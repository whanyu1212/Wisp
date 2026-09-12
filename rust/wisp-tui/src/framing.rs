use bytes::{Buf, Bytes, BytesMut};
use tokio::io::{AsyncRead, AsyncReadExt};

use crate::Error;

pub struct FrameReader<R> {
    reader: R,
    buffer: BytesMut,
    scanned: usize,
}

impl<R: AsyncRead + Unpin> FrameReader<R> {
    pub fn new(reader: R) -> Self {
        Self {
            reader,
            buffer: BytesMut::with_capacity(8192),
            scanned: 0,
        }
    }

    pub async fn read_frame(&mut self, limit: usize) -> Result<Option<Bytes>, Error> {
        loop {
            if let Some(offset) = self.buffer[self.scanned..]
                .iter()
                .position(|byte| *byte == b'\n')
            {
                let newline = self.scanned + offset;
                let mut frame = self.buffer.split_to(newline + 1);
                self.scanned = 0;
                frame.truncate(newline);
                if frame.last() == Some(&b'\r') {
                    frame.truncate(frame.len() - 1);
                }
                if frame.len() > limit {
                    return Err(Error::FrameTooLarge { limit });
                }
                return Ok(Some(frame.freeze()));
            }
            if self.buffer.len() > limit.saturating_add(1) {
                return Err(Error::FrameTooLarge { limit });
            }
            // Bytes already searched cannot become a delimiter. Persist the
            // cursor before awaiting so cancellation and a later call do not
            // rescan the retained prefix.
            self.scanned = self.buffer.len();
            // Allow the content limit plus a possible CRLF, never a growing
            // read_buf allocation or unbounded read-ahead behind this frame.
            let allowance = limit.saturating_add(2).saturating_sub(self.buffer.len());
            if allowance == 0 {
                return Err(Error::FrameTooLarge { limit });
            }
            let mut chunk = [0_u8; 8192];
            let read = self.reader.read(&mut chunk[..allowance.min(8192)]).await?;
            self.buffer.extend_from_slice(&chunk[..read]);
            if read == 0 {
                if self.buffer.is_empty() {
                    return Ok(None);
                }
                if self.buffer.len() > limit {
                    self.buffer.advance(self.buffer.len());
                    self.scanned = 0;
                    return Err(Error::FrameTooLarge { limit });
                }
                self.buffer.advance(self.buffer.len());
                self.scanned = 0;
                return Err(Error::IncompleteFrame);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{
        collections::VecDeque,
        pin::Pin,
        sync::{
            Arc, Mutex,
            atomic::{AtomicUsize, Ordering},
        },
        task::{Context, Poll},
    };

    use super::*;
    use tokio::io::ReadBuf;

    struct ChunkedReader {
        input: &'static [u8],
        offset: usize,
        chunk_size: usize,
        largest_request: Arc<AtomicUsize>,
    }

    struct PendingChunkReader {
        chunks: Arc<Mutex<VecDeque<Vec<u8>>>>,
    }

    impl AsyncRead for PendingChunkReader {
        fn poll_read(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
            buffer: &mut ReadBuf<'_>,
        ) -> Poll<std::io::Result<()>> {
            let Some(chunk) = self.chunks.lock().unwrap().pop_front() else {
                return Poll::Pending;
            };
            assert!(chunk.len() <= buffer.remaining());
            buffer.put_slice(&chunk);
            Poll::Ready(Ok(()))
        }
    }

    impl ChunkedReader {
        fn new(input: &'static [u8], chunk_size: usize, largest_request: Arc<AtomicUsize>) -> Self {
            Self {
                input,
                offset: 0,
                chunk_size,
                largest_request,
            }
        }
    }

    impl AsyncRead for ChunkedReader {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
            buffer: &mut ReadBuf<'_>,
        ) -> Poll<std::io::Result<()>> {
            self.largest_request
                .fetch_max(buffer.remaining(), Ordering::Relaxed);
            let available = &self.input[self.offset..];
            let length = available.len().min(self.chunk_size).min(buffer.remaining());
            buffer.put_slice(&available[..length]);
            self.offset += length;
            Poll::Ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn reads_crlf_and_preserves_buffered_frames() {
        let input = &b"{\"one\":1}\r\n{\"two\":2}\n"[..];
        let mut reader = FrameReader::new(input);
        assert_eq!(
            reader.read_frame(32).await.unwrap().unwrap(),
            &b"{\"one\":1}"[..]
        );
        assert_eq!(
            reader.read_frame(32).await.unwrap().unwrap(),
            &b"{\"two\":2}"[..]
        );
        assert!(reader.read_frame(32).await.unwrap().is_none());
    }

    #[tokio::test]
    async fn rejects_oversized_and_incomplete_frames() {
        let mut oversized = FrameReader::new(&b"12345\n"[..]);
        assert!(matches!(
            oversized.read_frame(4).await,
            Err(Error::FrameTooLarge { limit: 4 })
        ));
        let mut incomplete = FrameReader::new(&b"{}"[..]);
        assert!(matches!(
            incomplete.read_frame(4).await,
            Err(Error::IncompleteFrame)
        ));

        let mut oversized_at_eof = FrameReader::new(&b"12345"[..]);
        assert!(matches!(
            oversized_at_eof.read_frame(4).await,
            Err(Error::FrameTooLarge { limit: 4 })
        ));
    }

    #[tokio::test]
    async fn reads_fragmented_crlf_without_requesting_more_than_eight_kibibytes() {
        static INPUT: &[u8] = b"fragmented\r\nnext\n";
        let largest_request = Arc::new(AtomicUsize::new(0));
        let reader = ChunkedReader::new(INPUT, 1, Arc::clone(&largest_request));
        let mut frames = FrameReader::new(reader);

        assert_eq!(
            frames.read_frame(16 * 1024).await.unwrap().unwrap(),
            &b"fragmented"[..]
        );
        assert_eq!(
            frames.read_frame(16 * 1024).await.unwrap().unwrap(),
            &b"next"[..]
        );
        assert!(frames.read_frame(16 * 1024).await.unwrap().is_none());
        assert!(largest_request.load(Ordering::Relaxed) <= 8 * 1024);
    }

    #[tokio::test]
    async fn read_request_shrinks_to_the_remaining_crlf_allowance() {
        static INPUT: &[u8] = b"1234\r\n";
        let largest_request = Arc::new(AtomicUsize::new(0));
        let reader = ChunkedReader::new(INPUT, 4, Arc::clone(&largest_request));
        let mut frames = FrameReader::new(reader);

        assert_eq!(frames.read_frame(4).await.unwrap().unwrap(), &b"1234"[..]);
        // The initial request is exactly the content limit plus the CRLF
        // allowance, and no subsequent request can exceed that bound.
        assert_eq!(largest_request.load(Ordering::Relaxed), 6);
    }

    #[tokio::test]
    async fn cancelled_read_retains_its_incremental_scan_cursor() {
        let chunks = Arc::new(Mutex::new(VecDeque::from([b"prefix".to_vec()])));
        let mut frames = FrameReader::new(PendingChunkReader {
            chunks: Arc::clone(&chunks),
        });

        tokio::select! {
            biased;
            result = frames.read_frame(32) => panic!("reader unexpectedly completed: {result:?}"),
            () = tokio::task::yield_now() => {}
        }
        assert_eq!(frames.scanned, b"prefix".len());

        chunks.lock().unwrap().push_back(b"-tail\n".to_vec());
        assert_eq!(
            frames.read_frame(32).await.unwrap().unwrap(),
            &b"prefix-tail"[..]
        );
        assert_eq!(frames.scanned, 0);
    }
}
