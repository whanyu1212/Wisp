//! FIFO transport admission and bounded command writes.

use bytes::Bytes;
use std::sync::Arc;
use tokio::{
    io::{AsyncRead, AsyncWrite, AsyncWriteExt},
    sync::{OwnedSemaphorePermit, Semaphore, mpsc, oneshot},
    time::{Duration, timeout},
};
use wisp_protocol::{
    EVENT_SCHEMA_VERSION, HANDSHAKE_FRAME_BYTES, LIVE_RPC_PROTOCOL_VERSION,
    commands::WispTypedClientRpcCommands, events::WispCurrentLiveEventOutput,
    handshake_response::RpcHandshakeResponse,
};

use crate::{
    Error, HANDSHAKE_TIMEOUT, SHUTDOWN_COMMAND_ID, framing::FrameReader, reducer::BackendEvent,
};

pub(crate) const TRANSPORT_STALL_TIMEOUT: Duration = Duration::from_secs(5);

pub(crate) enum WriterMessage {
    Frame {
        payload: Bytes,
        limit: usize,
        ack: Option<oneshot::Sender<Result<(), ()>>>,
    },
    Close,
}

#[derive(Debug)]
pub(crate) enum ReaderTermination {
    Eof,
}

pub(crate) struct QueuedEvent {
    pub event: BackendEvent,
    pub _wire_bytes: OwnedSemaphorePermit,
}

/// Close admission and account for every event, including a racing reserved send.
pub(crate) async fn abandon_events(
    events: &mut mpsc::Receiver<QueuedEvent>,
    ready: Option<QueuedEvent>,
) -> (usize, usize) {
    events.close();
    let mut count = 0;
    let mut wire_bytes = 0;
    if let Some(event) = ready {
        count += 1;
        wire_bytes += event._wire_bytes.num_permits();
    }
    // close() prevents new reservations, but existing permits can still send.
    // recv() waits for the reader's one outstanding reservation to settle.
    while let Some(event) = events.recv().await {
        count += 1;
        wire_bytes += event._wire_bytes.num_permits();
    }
    (count, wire_bytes)
}

pub(crate) async fn send_value<T: serde::Serialize>(
    writer: &mpsc::Sender<WriterMessage>,
    value: &T,
    limit: usize,
) -> Result<(), Error> {
    let payload = Bytes::from(serde_json::to_vec(value)?);
    send_payload(writer, payload, limit).await
}

pub(crate) async fn send_payload(
    writer: &mpsc::Sender<WriterMessage>,
    payload: Bytes,
    limit: usize,
) -> Result<(), Error> {
    if payload.len() > limit {
        return Err(Error::FrameTooLarge { limit });
    }
    timeout(
        TRANSPORT_STALL_TIMEOUT,
        writer.send(WriterMessage::Frame {
            payload,
            limit,
            ack: None,
        }),
    )
    .await
    .map_err(|_| Error::WriterAdmissionTimeout)?
    .map_err(|_| Error::WriterStopped)
}

pub(crate) async fn send_payload_confirmed(
    writer: &mpsc::Sender<WriterMessage>,
    payload: Bytes,
    limit: usize,
) -> Result<(), Error> {
    if payload.len() > limit {
        return Err(Error::FrameTooLarge { limit });
    }
    let (ack_tx, ack_rx) = oneshot::channel();
    timeout(HANDSHAKE_TIMEOUT, async {
        writer
            .send(WriterMessage::Frame {
                payload,
                limit,
                ack: Some(ack_tx),
            })
            .await
            .map_err(|_| Error::WriterStopped)?;
        ack_rx
            .await
            .map_err(|_| Error::WriterStopped)?
            .map_err(|_| Error::WriterStopped)
    })
    .await
    .map_err(|_| Error::QueueSubmissionTimeout)?
}

pub(crate) async fn queue_shutdown_and_close(
    writer: &mpsc::Sender<WriterMessage>,
    limit: usize,
) -> Result<(), Error> {
    let shutdown = WispTypedClientRpcCommands::shutdown(SHUTDOWN_COMMAND_ID)?;
    send_value(writer, &shutdown, limit).await?;
    timeout(TRANSPORT_STALL_TIMEOUT, writer.send(WriterMessage::Close))
        .await
        .map_err(|_| Error::WriterAdmissionTimeout)?
        .map_err(|_| Error::WriterStopped)
}

pub(crate) async fn writer_task<W: AsyncWrite + Unpin>(
    mut writer: W,
    mut messages: mpsc::Receiver<WriterMessage>,
) -> Result<(), Error> {
    while let Some(message) = messages.recv().await {
        match message {
            WriterMessage::Frame {
                payload,
                limit,
                ack,
            } => {
                let result = timeout(TRANSPORT_STALL_TIMEOUT, async {
                    if payload.len() > limit {
                        return Err(Error::FrameTooLarge { limit });
                    }
                    writer.write_all(&payload).await?;
                    writer.write_all(b"\n").await?;
                    writer.flush().await?;
                    Ok(())
                })
                .await
                .map_err(|_| Error::WriterStallTimeout)
                .and_then(|result| result);
                match result {
                    Ok(()) => {
                        if let Some(ack) = ack {
                            let _ = ack.send(Ok(()));
                        }
                    }
                    Err(error) => {
                        if let Some(ack) = ack {
                            let _ = ack.send(Err(()));
                        }
                        return Err(error);
                    }
                }
            }
            WriterMessage::Close => break,
        }
    }
    timeout(TRANSPORT_STALL_TIMEOUT, writer.shutdown())
        .await
        .map_err(|_| Error::WriterStallTimeout)??;
    Ok(())
}

pub(crate) async fn stdout_reader_task<R: AsyncRead + Unpin>(
    reader: R,
    handshake: oneshot::Sender<Result<RpcHandshakeResponse, Error>>,
    events: mpsc::Sender<QueuedEvent>,
    event_wire_budget: Arc<Semaphore>,
    outcome: oneshot::Sender<Result<ReaderTermination, Error>>,
) {
    let mut frames = FrameReader::new(reader);
    let handshake_frame = match frames.read_frame(HANDSHAKE_FRAME_BYTES).await {
        Ok(Some(frame)) => frame,
        Ok(None) => {
            let _ = handshake.send(Err(Error::HandshakeEof));
            return;
        }
        Err(error) => {
            let _ = handshake.send(Err(error));
            return;
        }
    };
    let response = match serde_json::from_slice::<RpcHandshakeResponse>(&handshake_frame) {
        Ok(response) => response,
        Err(error) => {
            let _ = handshake.send(Err(Error::InvalidProtocolFrame(error)));
            return;
        }
    };
    let server_limit = response
        .accepted_contract()
        .map_or(HANDSHAKE_FRAME_BYTES, |contract| contract.3);
    if handshake.send(Ok(response)).is_err() {
        return;
    }
    let result = loop {
        let frame = match frames.read_frame(server_limit).await {
            Ok(Some(frame)) => frame,
            Ok(None) => break Ok(ReaderTermination::Eof),
            Err(error) => break Err(error),
        };
        // One deadline covers both resources. Reserve before decoding so the
        // waiting reader retains only one bounded wire frame.
        let admission = timeout(TRANSPORT_STALL_TIMEOUT, async {
            let slot = events.reserve().await.map_err(|_| Error::ReaderStopped)?;
            let bytes = tokio::select! {
                biased;
                _ = events.closed() => return Err(Error::ReaderStopped),
                permits = Arc::clone(&event_wire_budget)
                    .acquire_many_owned(u32::try_from(frame.len()).expect("frame limit fits u32")) => {
                    permits.map_err(|_| Error::ReaderStopped)?
                }
            };
            Ok::<_, Error>((slot, bytes))
        })
        .await;
        let (slot, permit) = match admission {
            Ok(Ok(admission)) => admission,
            Ok(Err(error)) => break Err(error),
            Err(_) => break Err(Error::InboundOverloaded),
        };
        if events.is_closed() {
            break Err(Error::ReaderStopped);
        }
        let event = match serde_json::from_slice::<WispCurrentLiveEventOutput>(&frame) {
            Ok(event) => event,
            Err(error) => break Err(Error::InvalidProtocolFrame(error)),
        };
        if event.schema_version() != EVENT_SCHEMA_VERSION {
            break Err(Error::ContractMismatch {
                protocol: LIVE_RPC_PROTOCOL_VERSION,
                events: event.schema_version(),
            });
        }
        let event = match BackendEvent::from_live(&event) {
            Ok(event) => event,
            Err(error) => break Err(Error::EventProjection(error)),
        };
        if events.is_closed() {
            break Err(Error::ReaderStopped);
        }
        slot.send(QueuedEvent {
            event,
            _wire_bytes: permit,
        });
    };
    let _ = outcome.send(result);
}

#[cfg(test)]
mod tests {
    use std::{
        io,
        pin::Pin,
        task::{Context, Poll},
    };

    use serde_json::json;
    use tokio::{
        io::{AsyncReadExt, AsyncWrite, AsyncWriteExt, ReadBuf, duplex},
        task::yield_now,
        time::advance,
    };

    use super::*;

    fn handshake() -> serde_json::Value {
        json!({
            "type": "rpc.handshake.accepted",
            "backend_package_version": "0.1.0",
            "protocol_version": LIVE_RPC_PROTOCOL_VERSION,
            "event_schema_version": EVENT_SCHEMA_VERSION,
            "min_protocol_version": LIVE_RPC_PROTOCOL_VERSION,
            "max_protocol_version": LIVE_RPC_PROTOCOL_VERSION,
            "capabilities": [],
            "limits": {
                "max_client_frame_bytes": 1024,
                "max_server_frame_bytes": 2048
            }
        })
    }

    fn event(command_id: &str) -> String {
        json!({
            "type": "rpc.command.finished",
            "schema_version": EVENT_SCHEMA_VERSION,
            "timestamp": "2026-01-02T03:04:05Z",
            "command_id": command_id,
            "command_type": "shutdown",
            "ok": true,
            "error": null
        })
        .to_string()
    }

    struct ChunkedInput {
        input: Vec<u8>,
        offset: usize,
        chunk_size: usize,
    }

    impl ChunkedInput {
        fn new(input: Vec<u8>, chunk_size: usize) -> Self {
            Self {
                input,
                offset: 0,
                chunk_size,
            }
        }
    }

    impl AsyncRead for ChunkedInput {
        fn poll_read(
            mut self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
            buffer: &mut ReadBuf<'_>,
        ) -> Poll<io::Result<()>> {
            let available = &self.input[self.offset..];
            let length = available.len().min(self.chunk_size).min(buffer.remaining());
            buffer.put_slice(&available[..length]);
            self.offset += length;
            Poll::Ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn generated_chunk_and_event_sizes_preserve_wire_permits() {
        for (padding_size, chunk_size) in
            [(1, 1), (7, 2), (31, 7), (127, 31), (511, 127), (1400, 8192)]
        {
            let command_id = format!("case-{padding_size}");
            let frame = format!("{}{}", event(&command_id), " ".repeat(padding_size));
            let input = format!("{}\n{frame}\n", handshake()).into_bytes();
            let budget = Arc::new(Semaphore::new(frame.len()));
            let (handshake_tx, handshake_rx) = oneshot::channel();
            let (event_tx, mut event_rx) = mpsc::channel(1);
            let (outcome_tx, outcome_rx) = oneshot::channel();

            stdout_reader_task(
                ChunkedInput::new(input, chunk_size),
                handshake_tx,
                event_tx,
                Arc::clone(&budget),
                outcome_tx,
            )
            .await;

            handshake_rx.await.unwrap().unwrap();
            let outcome = outcome_rx.await.unwrap();
            assert!(
                matches!(outcome, Ok(ReaderTermination::Eof)),
                "event padding {padding_size} in chunks of {chunk_size} failed: {outcome:?}"
            );
            assert_eq!(budget.available_permits(), 0);
            let queued = event_rx.recv().await.unwrap();
            assert!(matches!(
                &queued.event,
                BackendEvent::CommandFinished { command_id: actual, .. } if actual == &command_id
            ));
            drop(queued);
            assert_eq!(budget.available_permits(), frame.len());
        }
    }

    #[tokio::test]
    async fn generated_invalid_protocol_cases_release_wire_permits() {
        let mut wrong_schema: serde_json::Value = serde_json::from_str(&event("schema")).unwrap();
        wrong_schema["schema_version"] = json!(EVENT_SCHEMA_VERSION + 1);
        let cases = [
            ("{".to_string(), 1),
            ("[]".to_string(), 2),
            (wrong_schema.to_string(), 3),
        ];

        for (frame, chunk_size) in cases {
            let input = format!("{}\n{frame}\n", handshake()).into_bytes();
            let budget = Arc::new(Semaphore::new(frame.len()));
            let (handshake_tx, handshake_rx) = oneshot::channel();
            let (event_tx, mut event_rx) = mpsc::channel(1);
            let (outcome_tx, outcome_rx) = oneshot::channel();

            stdout_reader_task(
                ChunkedInput::new(input, chunk_size),
                handshake_tx,
                event_tx,
                Arc::clone(&budget),
                outcome_tx,
            )
            .await;

            handshake_rx.await.unwrap().unwrap();
            let outcome = outcome_rx.await.unwrap();
            assert!(matches!(outcome, Err(Error::InvalidProtocolFrame(_))));
            assert!(event_rx.try_recv().is_err());
            assert_eq!(budget.available_permits(), frame.len());
        }
    }

    #[tokio::test(start_paused = true)]
    async fn count_pressure_recovers_before_the_admission_deadline() {
        let first_frame = event("shutdown-a");
        let second_frame = event("shutdown-b");
        let input = format!("{}\n{first_frame}\n{second_frame}\n", handshake());
        let (mut server, client) = duplex(64 * 1024);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, mut event_rx) = mpsc::channel(1);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::new(Semaphore::new(4096)),
            outcome_tx,
        ));

        server.write_all(input.as_bytes()).await.unwrap();
        server.shutdown().await.unwrap();
        handshake_rx.await.unwrap().unwrap();
        yield_now().await;
        advance(Duration::from_secs(4)).await;
        let first = event_rx.recv().await.unwrap();
        let second = event_rx.recv().await.unwrap();
        assert!(matches!(
            &second.event,
            BackendEvent::CommandFinished { command_id, .. } if command_id == "shutdown-b"
        ));
        drop(first);
        assert!(matches!(
            outcome_rx.await.unwrap(),
            Ok(ReaderTermination::Eof)
        ));
        task.await.unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn byte_pressure_recovers_before_the_admission_deadline() {
        let first_frame = event("shutdown-a");
        let second_frame = event("shutdown-b");
        assert_eq!(first_frame.len(), second_frame.len());
        let input = format!("{}\n{first_frame}\n{second_frame}\n", handshake());
        let budget = Arc::new(Semaphore::new(first_frame.len()));
        let (mut server, client) = duplex(64 * 1024);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, mut event_rx) = mpsc::channel(2);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::clone(&budget),
            outcome_tx,
        ));

        server.write_all(input.as_bytes()).await.unwrap();
        server.shutdown().await.unwrap();
        handshake_rx.await.unwrap().unwrap();
        yield_now().await;
        advance(Duration::from_secs(4)).await;
        drop(event_rx.recv().await.unwrap());
        let second = event_rx.recv().await.unwrap();
        assert!(matches!(
            &second.event,
            BackendEvent::CommandFinished { command_id, .. } if command_id == "shutdown-b"
        ));
        drop(second);
        assert_eq!(budget.available_permits(), first_frame.len());
        assert!(matches!(
            outcome_rx.await.unwrap(),
            Ok(ReaderTermination::Eof)
        ));
        task.await.unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn combined_count_and_byte_pressure_recovers_fifo() {
        let first_frame = event("shutdown-a");
        let second_frame = event("shutdown-b");
        let input = format!("{}\n{first_frame}\n{second_frame}\n", handshake());
        let (mut server, client) = duplex(64 * 1024);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, mut event_rx) = mpsc::channel(1);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::new(Semaphore::new(first_frame.len())),
            outcome_tx,
        ));

        server.write_all(input.as_bytes()).await.unwrap();
        server.shutdown().await.unwrap();
        handshake_rx.await.unwrap().unwrap();
        yield_now().await;
        advance(Duration::from_secs(4)).await;
        drop(event_rx.recv().await.unwrap());
        let second = event_rx.recv().await.unwrap();
        assert!(matches!(
            &second.event,
            BackendEvent::CommandFinished { command_id, .. } if command_id == "shutdown-b"
        ));
        drop(second);
        assert!(matches!(
            outcome_rx.await.unwrap(),
            Ok(ReaderTermination::Eof)
        ));
        task.await.unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn count_and_bytes_share_one_absolute_deadline() {
        let first_frame = event("shutdown-a");
        let second_frame = event("shutdown-b");
        let input = format!("{}\n{first_frame}\n{second_frame}\n", handshake());
        let budget = Arc::new(Semaphore::new(first_frame.len()));
        let (mut server, client) = duplex(64 * 1024);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, mut event_rx) = mpsc::channel(1);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::clone(&budget),
            outcome_tx,
        ));

        server.write_all(input.as_bytes()).await.unwrap();
        server.shutdown().await.unwrap();
        handshake_rx.await.unwrap().unwrap();
        yield_now().await;
        advance(Duration::from_secs(3)).await;
        let retained_first = event_rx.recv().await.unwrap();
        yield_now().await;
        advance(Duration::from_secs(2)).await;

        assert!(matches!(
            outcome_rx.await.unwrap(),
            Err(Error::InboundOverloaded)
        ));
        assert!(event_rx.try_recv().is_err());
        drop(retained_first);
        assert_eq!(budget.available_permits(), first_frame.len());
        task.await.unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn receiver_closure_interrupts_a_byte_wait() {
        let frame = event("shutdown-a");
        let input = format!("{}\n{frame}\n", handshake());
        let (mut server, client) = duplex(4096);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, event_rx) = mpsc::channel(1);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::new(Semaphore::new(0)),
            outcome_tx,
        ));

        server.write_all(input.as_bytes()).await.unwrap();
        handshake_rx.await.unwrap().unwrap();
        yield_now().await;
        drop(event_rx);

        assert!(matches!(
            outcome_rx.await.unwrap(),
            Err(Error::ReaderStopped)
        ));
        task.await.unwrap();
    }

    #[tokio::test]
    async fn malformed_and_oversized_frames_release_or_preserve_permits() {
        let malformed = "not-json";
        let budget = Arc::new(Semaphore::new(malformed.len()));
        let (mut server, client) = duplex(4096);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, mut event_rx) = mpsc::channel(1);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::clone(&budget),
            outcome_tx,
        ));
        server
            .write_all(format!("{}\n{malformed}\n", handshake()).as_bytes())
            .await
            .unwrap();
        handshake_rx.await.unwrap().unwrap();
        assert!(matches!(
            outcome_rx.await.unwrap(),
            Err(Error::InvalidProtocolFrame(_))
        ));
        assert!(event_rx.try_recv().is_err());
        assert_eq!(budget.available_permits(), malformed.len());
        task.await.unwrap();

        let mut accepted = handshake();
        accepted["limits"]["max_server_frame_bytes"] = json!(4);
        let budget = Arc::new(Semaphore::new(16));
        let (mut server, client) = duplex(4096);
        let (handshake_tx, handshake_rx) = oneshot::channel();
        let (event_tx, _event_rx) = mpsc::channel(1);
        let (outcome_tx, outcome_rx) = oneshot::channel();
        let task = tokio::spawn(stdout_reader_task(
            client,
            handshake_tx,
            event_tx,
            Arc::clone(&budget),
            outcome_tx,
        ));
        server
            .write_all(format!("{accepted}\n12345\n").as_bytes())
            .await
            .unwrap();
        handshake_rx.await.unwrap().unwrap();
        assert!(matches!(
            outcome_rx.await.unwrap(),
            Err(Error::FrameTooLarge { limit: 4 })
        ));
        assert_eq!(budget.available_permits(), 16);
        task.await.unwrap();
    }

    #[tokio::test(start_paused = true)]
    async fn payload_admission_has_a_five_second_deadline() {
        let (tx, mut rx) = mpsc::channel(1);
        tx.send(WriterMessage::Close).await.unwrap();
        let send = tokio::spawn({
            let tx = tx.clone();
            async move { send_payload(&tx, Bytes::from_static(b"{}"), 16).await }
        });
        yield_now().await;
        advance(TRANSPORT_STALL_TIMEOUT).await;

        assert!(matches!(
            send.await.unwrap(),
            Err(Error::WriterAdmissionTimeout)
        ));
        assert!(matches!(rx.recv().await, Some(WriterMessage::Close)));
    }

    #[tokio::test(start_paused = true)]
    async fn stalled_partial_frame_is_terminal_and_not_retried() {
        let (client, mut server) = duplex(4);
        let (tx, rx) = mpsc::channel(1);
        let task = tokio::spawn(writer_task(client, rx));
        tx.send(WriterMessage::Frame {
            payload: Bytes::from_static(b"abcdefgh"),
            limit: 8,
            ack: None,
        })
        .await
        .unwrap();
        yield_now().await;
        advance(TRANSPORT_STALL_TIMEOUT).await;

        assert!(matches!(
            task.await.unwrap(),
            Err(Error::WriterStallTimeout)
        ));
        let mut partial = Vec::new();
        server.read_to_end(&mut partial).await.unwrap();
        assert!(!partial.is_empty());
        assert!(partial.len() < b"abcdefgh\n".len());
        assert!(b"abcdefgh\n".starts_with(&partial));
    }

    struct FailingWriter;

    impl AsyncWrite for FailingWriter {
        fn poll_write(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
            _buffer: &[u8],
        ) -> Poll<io::Result<usize>> {
            Poll::Ready(Err(io::Error::new(io::ErrorKind::BrokenPipe, "closed")))
        }

        fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }

        fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }
    }

    #[tokio::test]
    async fn writer_failure_rejects_the_confirmation_and_stops() {
        let (tx, rx) = mpsc::channel(1);
        let task = tokio::spawn(writer_task(FailingWriter, rx));
        let (ack_tx, ack_rx) = oneshot::channel();
        tx.send(WriterMessage::Frame {
            payload: Bytes::from_static(b"{}"),
            limit: 16,
            ack: Some(ack_tx),
        })
        .await
        .unwrap();

        assert_eq!(ack_rx.await.unwrap(), Err(()));
        assert!(matches!(task.await.unwrap(), Err(Error::Io(_))));
    }

    struct StalledShutdownWriter;

    impl AsyncWrite for StalledShutdownWriter {
        fn poll_write(
            self: Pin<&mut Self>,
            _cx: &mut Context<'_>,
            buffer: &[u8],
        ) -> Poll<io::Result<usize>> {
            Poll::Ready(Ok(buffer.len()))
        }

        fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Ready(Ok(()))
        }

        fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
            Poll::Pending
        }
    }

    #[tokio::test(start_paused = true)]
    async fn writer_shutdown_has_a_five_second_deadline() {
        let (tx, rx) = mpsc::channel(1);
        let task = tokio::spawn(writer_task(StalledShutdownWriter, rx));
        tx.send(WriterMessage::Close).await.unwrap();
        yield_now().await;
        advance(TRANSPORT_STALL_TIMEOUT).await;

        assert!(matches!(
            task.await.unwrap(),
            Err(Error::WriterStallTimeout)
        ));
    }
    #[tokio::test(start_paused = true)]
    async fn fatal_abandonment_includes_a_send_reserved_before_receiver_closure() {
        let budget = Arc::new(Semaphore::new(64));
        let (sender, mut events) = mpsc::channel(1);
        let slot = sender.reserve_owned().await.unwrap();
        let wire_bytes = budget.clone().acquire_many_owned(17).await.unwrap();
        let late_send = tokio::spawn(async move {
            tokio::time::sleep(Duration::from_secs(1)).await;
            slot.send(QueuedEvent {
                event: BackendEvent::Diagnostic("not dispatched".into()),
                _wire_bytes: wire_bytes,
            });
        });
        let ready = QueuedEvent {
            event: BackendEvent::Diagnostic("held".into()),
            _wire_bytes: budget.clone().acquire_many_owned(13).await.unwrap(),
        };
        assert_eq!(abandon_events(&mut events, Some(ready)).await, (2, 30));
        assert_eq!(budget.available_permits(), 64);
        late_send.await.unwrap();
    }
}
