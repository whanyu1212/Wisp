//! Native Python accelerators for Wisp.

#![forbid(unsafe_code)]

use pyo3::exceptions::PyOverflowError;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyTuple};
use wisp_process_text::PendingText as ProcessPendingText;

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
    module.add_class::<PendingText>()
}
