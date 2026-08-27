use bytes::{Buf, Bytes, BytesMut};
use tokio::io::{AsyncRead, AsyncReadExt};

use crate::Error;

pub struct FrameReader<R> {
    reader: R,
    buffer: BytesMut,
}

impl<R: AsyncRead + Unpin> FrameReader<R> {
    pub fn new(reader: R) -> Self {
        Self {
            reader,
            buffer: BytesMut::with_capacity(8192),
        }
    }

    pub async fn read_frame(&mut self, limit: usize) -> Result<Option<Bytes>, Error> {
        loop {
            if let Some(newline) = self.buffer.iter().position(|byte| *byte == b'\n') {
                let mut frame = self.buffer.split_to(newline + 1);
                frame.truncate(newline);
                if frame.last() == Some(&b'\r') {
                    frame.truncate(frame.len() - 1);
                }
                if frame.len() > limit {
                    return Err(Error::FrameTooLarge { limit });
                }
                return Ok(Some(frame.freeze()));
            }
            if self.buffer.len() > limit + 1 {
                return Err(Error::FrameTooLarge { limit });
            }
            let read = self.reader.read_buf(&mut self.buffer).await?;
            if read == 0 {
                if self.buffer.is_empty() {
                    return Ok(None);
                }
                self.buffer.advance(self.buffer.len());
                return Err(Error::IncompleteFrame);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
    }
}
