//! Native Python accelerators for Wisp.

#![forbid(unsafe_code)]

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::exceptions::{PyOSError, PyOverflowError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyTuple};
use wisp_process_text::PendingText as ProcessPendingText;
use wisp_search::{ScanConfig, ScanError, ScanResult};

/// Incrementally decoded process output with bounded tail retention.
#[pyclass(module = "wisp._native")]
struct PendingText {
    inner: ProcessPendingText,
    max_bytes: Py<PyAny>,
    max_lines: Py<PyAny>,
}

#[pymethods]
impl PendingText {
    #[new]
    #[pyo3(signature = (max_bytes, max_lines))]
    fn new(max_bytes: Bound<'_, PyAny>, max_lines: Bound<'_, PyAny>) -> PyResult<Self> {
        let effective_max_bytes = effective_limit(&max_bytes)?;
        let effective_max_lines = effective_limit(&max_lines)?;
        Ok(Self {
            inner: ProcessPendingText::new(effective_max_bytes, effective_max_lines),
            max_bytes: max_bytes.unbind(),
            max_lines: max_lines.unbind(),
        })
    }

    #[getter]
    fn max_bytes(&self, py: Python<'_>) -> Py<PyAny> {
        self.max_bytes.clone_ref(py)
    }

    #[getter]
    fn max_lines(&self, py: Python<'_>) -> Py<PyAny> {
        self.max_lines.clone_ref(py)
    }

    #[getter]
    fn dropped_bytes(&self) -> usize {
        self.inner.dropped_bytes()
    }

    #[getter]
    fn retained_source_bytes(&self) -> usize {
        self.inner.retained_source_bytes()
    }

    #[getter]
    fn has_text(&self) -> bool {
        self.inner.has_text()
    }

    #[getter]
    fn text(&self) -> String {
        self.inner.text()
    }

    fn append(&mut self, value: &str) {
        self.inner.append(value);
    }

    #[pyo3(signature = (value, *, r#final=false))]
    fn append_bytes(&mut self, value: &[u8], r#final: bool) {
        self.inner.append_bytes(value, r#final);
    }

    fn drain(&mut self, py: Python<'_>) -> PyResult<(String, usize, usize, Py<PyTuple>)> {
        let drained = self.inner.drain();
        let source_byte_lengths = PyTuple::new(py, drained.source_byte_lengths)?.unbind();
        Ok((
            drained.text,
            drained.dropped_bytes,
            drained.retained_source_bytes,
            source_byte_lengths,
        ))
    }
}

/// Thread-safe cancellation token for a native grep scan.
#[pyclass(module = "wisp._native")]
struct GrepCancellation {
    inner: Arc<AtomicBool>,
}

#[pymethods]
impl GrepCancellation {
    #[new]
    fn new() -> Self {
        Self {
            inner: Arc::new(AtomicBool::new(false)),
        }
    }

    /// Requests cancellation of an in-flight scan.
    fn cancel(&self) {
        self.inner.store(true, Ordering::Relaxed);
    }

    /// Returns whether cancellation has been requested.
    #[getter]
    fn cancelled(&self) -> bool {
        self.inner.load(Ordering::Relaxed)
    }
}

/// One bounded native grep result.
#[pyclass(module = "wisp._native", frozen, get_all)]
struct GrepScanResult {
    lines: Vec<String>,
    byte_count: usize,
    match_count: usize,
    had_extra_match: bool,
    exhausted: bool,
    status: String,
}

impl From<ScanResult> for GrepScanResult {
    fn from(result: ScanResult) -> Self {
        Self {
            lines: result.lines,
            byte_count: result.byte_count,
            match_count: result.match_count,
            had_extra_match: result.had_extra_match,
            exhausted: result.exhausted,
            status: result.status.as_str().to_owned(),
        }
    }
}

/// Scans one already-opened file descriptor for a case-sensitive literal.
#[pyfunction]
#[pyo3(signature = (
    fd,
    pattern,
    display_path,
    *,
    context_lines,
    remaining_matches,
    prior_lines,
    prior_bytes,
    prefix_separator,
    max_output_lines,
    max_output_bytes,
    max_line_chars,
    cancellation
))]
#[allow(clippy::too_many_arguments)]
fn scan_literal_fd(
    py: Python<'_>,
    fd: i64,
    pattern: String,
    display_path: String,
    context_lines: usize,
    remaining_matches: usize,
    prior_lines: usize,
    prior_bytes: usize,
    prefix_separator: bool,
    max_output_lines: usize,
    max_output_bytes: usize,
    max_line_chars: usize,
    cancellation: PyRef<'_, GrepCancellation>,
) -> PyResult<GrepScanResult> {
    let cancellation = Arc::clone(&cancellation.inner);
    let config = ScanConfig {
        pattern,
        display_path,
        context_lines,
        remaining_matches,
        prior_lines,
        prior_bytes,
        prefix_separator,
        max_output_lines,
        max_output_bytes,
        max_line_chars,
    };
    py.detach(move || wisp_search::scan_literal_fd(fd, &config, &cancellation))
        .map(GrepScanResult::from)
        .map_err(scan_error_to_python)
}

fn scan_error_to_python(error: ScanError) -> PyErr {
    match error {
        ScanError::InvalidConfig(_) | ScanError::LineTooLong { .. } => {
            PyValueError::new_err(error.to_string())
        }
        ScanError::Io(io_error) => match io_error.raw_os_error() {
            Some(code) => PyOSError::new_err((code, io_error.to_string())),
            None => PyOSError::new_err(io_error.to_string()),
        },
        ScanError::UnsupportedTarget => PyRuntimeError::new_err(error.to_string()),
    }
}

fn effective_limit(value: &Bound<'_, PyAny>) -> PyResult<usize> {
    if value.lt(0)? {
        return Ok(0);
    }
    match value.extract::<usize>() {
        Ok(limit) => Ok(limit),
        Err(error) if error.is_instance_of::<PyOverflowError>(value.py()) => Ok(usize::MAX),
        Err(error) => Err(error),
    }
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<PendingText>()?;
    module.add_class::<GrepCancellation>()?;
    module.add_class::<GrepScanResult>()?;
    module.add_function(wrap_pyfunction!(scan_literal_fd, module)?)
}
