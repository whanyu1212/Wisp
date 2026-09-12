use std::{
    future::Future,
    pin::Pin,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    task::{Context, Poll, Wake, Waker},
};

use proptest::{
    collection::vec,
    prelude::*,
    test_runner::{Config, RngAlgorithm, TestRng, TestRunner},
};
use tokio::io::{AsyncRead, ReadBuf};

use super::FrameReader;
use crate::Error;

const CASES: u32 = 256;
const MAX_INPUT: usize = 8 * 1024;
const MAX_LIMIT: usize = 4 * 1024;
const MAX_SHRINK_ITERS: u32 = 4_096;

#[derive(Debug, PartialEq)]
enum Outcome {
    Frame(Vec<u8>),
    End,
    TooLarge(usize),
    Incomplete,
    OtherError(String),
}

#[derive(Debug)]
struct ObservedRun {
    outcomes: Vec<Outcome>,
    largest_retained_buffer: usize,
    largest_request: usize,
    read_budget_respected: bool,
}

struct PlannedReader {
    input: Vec<u8>,
    offset: usize,
    chunk_sizes: Vec<usize>,
    next_chunk: usize,
    largest_request: usize,
    limit: usize,
    frame_start: Arc<AtomicUsize>,
    read_budget_respected: bool,
}

impl PlannedReader {
    fn new(
        input: Vec<u8>,
        chunk_sizes: Vec<usize>,
        limit: usize,
        frame_start: Arc<AtomicUsize>,
    ) -> Self {
        debug_assert!(!chunk_sizes.is_empty());
        Self {
            input,
            offset: 0,
            chunk_sizes,
            next_chunk: 0,
            largest_request: 0,
            limit,
            frame_start,
            read_budget_respected: true,
        }
    }
}

impl AsyncRead for PlannedReader {
    fn poll_read(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        let frame_start = self.frame_start.load(Ordering::Relaxed);
        assert!(frame_start <= self.offset);
        let buffered_for_frame = self.offset - frame_start;
        let remaining_allowance = self
            .limit
            .saturating_add(2)
            .saturating_sub(buffered_for_frame)
            .min(8192);
        self.read_budget_respected &= buffer.remaining() <= remaining_allowance;
        self.largest_request = self.largest_request.max(buffer.remaining());
        if self.offset == self.input.len() {
            return Poll::Ready(Ok(()));
        }

        let requested_chunk = self.chunk_sizes[self.next_chunk % self.chunk_sizes.len()];
        self.next_chunk += 1;
        let length = requested_chunk
            .min(buffer.remaining())
            .min(self.input.len() - self.offset);
        let end = self.offset + length;
        buffer.put_slice(&self.input[self.offset..end]);
        self.offset = end;
        Poll::Ready(Ok(()))
    }
}

struct PauseOnceReader {
    prefix: Vec<u8>,
    suffix: Vec<u8>,
    stage: u8,
    largest_request: usize,
}

impl AsyncRead for PauseOnceReader {
    fn poll_read(
        mut self: Pin<&mut Self>,
        _cx: &mut Context<'_>,
        buffer: &mut ReadBuf<'_>,
    ) -> Poll<std::io::Result<()>> {
        self.largest_request = self.largest_request.max(buffer.remaining());
        match self.stage {
            0 => {
                assert!(self.prefix.len() <= buffer.remaining());
                let prefix = std::mem::take(&mut self.prefix);
                buffer.put_slice(&prefix);
                self.stage = 1;
                Poll::Ready(Ok(()))
            }
            1 => {
                self.stage = 2;
                Poll::Pending
            }
            2 => {
                assert!(self.suffix.len() <= buffer.remaining());
                let suffix = std::mem::take(&mut self.suffix);
                buffer.put_slice(&suffix);
                self.stage = 3;
                Poll::Ready(Ok(()))
            }
            _ => Poll::Ready(Ok(())),
        }
    }
}

struct NoopWake;

impl Wake for NoopWake {
    fn wake(self: Arc<Self>) {}
}

fn runner(seed: u8) -> TestRunner {
    let config = Config {
        cases: CASES,
        max_shrink_iters: MAX_SHRINK_ITERS,
        failure_persistence: None,
        ..Config::default()
    };
    let rng = TestRng::from_seed(RngAlgorithm::ChaCha, &[seed; 32]);
    TestRunner::new_with_rng(config, rng)
}

fn non_newline_byte() -> impl Strategy<Value = u8> {
    prop_oneof![0_u8..=9, 11_u8..=u8::MAX]
}

fn reference_outcomes(input: &[u8], limit: usize) -> Vec<Outcome> {
    let mut outcomes = Vec::new();
    let mut remaining = input;

    loop {
        let Some(newline) = remaining.iter().position(|byte| *byte == b'\n') else {
            if remaining.is_empty() {
                outcomes.push(Outcome::End);
            } else if remaining.len() > limit {
                outcomes.push(Outcome::TooLarge(limit));
            } else {
                outcomes.push(Outcome::Incomplete);
            }
            break;
        };

        let mut frame = &remaining[..newline];
        remaining = &remaining[newline + 1..];
        if frame.last() == Some(&b'\r') {
            frame = &frame[..frame.len() - 1];
        }
        if frame.len() > limit {
            outcomes.push(Outcome::TooLarge(limit));
            break;
        }
        outcomes.push(Outcome::Frame(frame.to_vec()));
    }

    outcomes
}

async fn observe(input: Vec<u8>, chunk_sizes: Vec<usize>, limit: usize) -> ObservedRun {
    let source = input.clone();
    let frame_start = Arc::new(AtomicUsize::new(0));
    let mut reader = FrameReader::new(PlannedReader::new(
        input,
        chunk_sizes,
        limit,
        Arc::clone(&frame_start),
    ));
    let mut outcomes = Vec::new();
    let mut largest_retained_buffer = 0;

    loop {
        let terminal = match reader.read_frame(limit).await {
            Ok(Some(frame)) => {
                outcomes.push(Outcome::Frame(frame.to_vec()));
                let current_start = frame_start.load(Ordering::Relaxed);
                let newline = source[current_start..]
                    .iter()
                    .position(|byte| *byte == b'\n')
                    .expect("a returned frame must have a source delimiter");
                frame_start.store(current_start + newline + 1, Ordering::Relaxed);
                false
            }
            Ok(None) => {
                outcomes.push(Outcome::End);
                true
            }
            Err(Error::FrameTooLarge { limit }) => {
                outcomes.push(Outcome::TooLarge(limit));
                true
            }
            Err(Error::IncompleteFrame) => {
                outcomes.push(Outcome::Incomplete);
                true
            }
            Err(error) => {
                outcomes.push(Outcome::OtherError(error.to_string()));
                true
            }
        };
        largest_retained_buffer = largest_retained_buffer.max(reader.buffer.len());
        if terminal {
            break;
        }
    }

    ObservedRun {
        outcomes,
        largest_retained_buffer,
        largest_request: reader.reader.largest_request,
        read_budget_respected: reader.reader.read_budget_respected,
    }
}

#[test]
fn arbitrary_chunks_match_the_reference_splitter() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let strategy = (
        vec(any::<u8>(), 0..=MAX_INPUT),
        vec(1_usize..=1024, 1..=32),
        0_usize..=MAX_LIMIT,
    );

    runner(0x46)
        .run(&strategy, |(input, chunk_sizes, limit)| {
            let expected = reference_outcomes(&input, limit);
            let observed = runtime.block_on(observe(input.clone(), chunk_sizes, limit));

            prop_assert_eq!(observed.outcomes, expected);
            prop_assert!(observed.largest_retained_buffer <= limit.saturating_add(2));
            prop_assert!(observed.largest_request <= limit.saturating_add(2).min(8192));
            prop_assert!(observed.read_budget_respected);
            Ok(())
        })
        .unwrap();
}

#[test]
fn exact_and_oversized_lf_and_crlf_frames_match_the_reference() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let strategy = (
        0_usize..=MAX_LIMIT,
        0_usize..=2,
        any::<bool>(),
        vec(1_usize..=1024, 1..=16),
    )
        .prop_flat_map(|(limit, extra, crlf, chunk_sizes)| {
            (
                Just(limit),
                Just(crlf),
                vec(non_newline_byte(), limit + extra),
                Just(chunk_sizes),
            )
        });

    runner(0x47)
        .run(&strategy, |(limit, crlf, payload, chunk_sizes)| {
            let mut input = payload.clone();
            if crlf {
                input.push(b'\r');
            }
            input.push(b'\n');

            let expected = reference_outcomes(&input, limit);
            let observed = runtime.block_on(observe(input.clone(), chunk_sizes, limit));
            prop_assert_eq!(observed.outcomes, expected);
            prop_assert!(observed.largest_retained_buffer <= limit.saturating_add(2));
            prop_assert!(observed.largest_request <= limit.saturating_add(2).min(8192));
            prop_assert!(observed.read_budget_respected);

            Ok(())
        })
        .unwrap();
}

#[test]
fn cancelled_reads_resume_without_losing_or_rescanning_bytes() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();
    let strategy = (1_usize..=2048, 0_usize..=2048, any::<bool>()).prop_flat_map(
        |(prefix_len, suffix_len, crlf)| {
            (
                vec(non_newline_byte(), prefix_len),
                vec(non_newline_byte(), suffix_len),
                Just(crlf),
            )
        },
    );

    runner(0x48)
        .run(&strategy, |(prefix, tail, crlf)| {
            let limit = prefix.len() + tail.len();
            let mut suffix = tail.clone();
            if crlf {
                suffix.push(b'\r');
            }
            suffix.push(b'\n');

            let mut frames = FrameReader::new(PauseOnceReader {
                prefix: prefix.clone(),
                suffix,
                stage: 0,
                largest_request: 0,
            });
            let mut pending_read = Box::pin(frames.read_frame(limit));
            let waker = Waker::from(Arc::new(NoopWake));
            let mut context = Context::from_waker(&waker);
            prop_assert!(matches!(
                pending_read.as_mut().poll(&mut context),
                Poll::Pending
            ));
            drop(pending_read);

            prop_assert_eq!(frames.scanned, prefix.len());
            let frame = runtime
                .block_on(frames.read_frame(limit))
                .map_err(|error| TestCaseError::fail(error.to_string()))?
                .ok_or_else(|| TestCaseError::fail("resumed read returned EOF"))?;
            let mut expected = prefix;
            expected.extend_from_slice(&tail);
            if !crlf && expected.last() == Some(&b'\r') {
                expected.pop();
            }
            prop_assert_eq!(frame.as_ref(), expected.as_slice());
            prop_assert_eq!(frames.scanned, 0);
            prop_assert!(frames.buffer.len() <= limit.saturating_add(2));
            prop_assert!(frames.reader.largest_request <= limit.saturating_add(2).min(8192));
            Ok(())
        })
        .unwrap();
}

#[tokio::test]
async fn framing_preserves_non_utf8_payload_bytes() {
    let input = vec![0xff, 0xfe, b'\r', b'\n'];
    let mut reader = FrameReader::new(input.as_slice());

    assert_eq!(
        reader.read_frame(2).await.unwrap().unwrap().as_ref(),
        &[0xff, 0xfe]
    );
    assert!(reader.read_frame(2).await.unwrap().is_none());
}
