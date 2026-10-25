//! Python binding for the native directory walk.

use std::ffi::OsString;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::sync::Mutex;

use pyo3::exceptions::PyException;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use wisp_search::gitignore::{GitIgnore, wtf8_from_raw};
use wisp_search::walk::{AncestorIgnore, Batch, Walk, WalkConfig, WalkError};

pyo3::create_exception!(
    _native,
    NativeWalkError,
    PyException,
    "A native walk stopped at a limit or an unreadable directory."
);

/// A directory's descriptor, its path relative to the root, and its files as
/// `(name, st_dev, st_ino, hidden_or_ignored)`.
type PythonBatch = (i32, OsString, Vec<(OsString, u64, u64, bool)>);

/// A walk below one directory, yielding one batch per run of files.
///
/// A batch's descriptor stays open until the next `__next__` or `close`.
#[pyclass(module = "wisp._native", frozen)]
pub(crate) struct NativeWalk {
    walk: Mutex<Option<Walk>>,
}

#[pymethods]
impl NativeWalk {
    #[new]
    #[pyo3(signature = (
        descriptor,
        *,
        ancestors,
        in_git_repository,
        include_ignored,
        max_directory_entries,
        max_ignore_file_bytes,
        max_ignore_file_patterns,
        max_git_marker_bytes
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        descriptor: i32,
        ancestors: Vec<(OsString, u8, Bound<'_, PyBytes>)>,
        in_git_repository: bool,
        include_ignored: bool,
        max_directory_entries: usize,
        max_ignore_file_bytes: usize,
        max_ignore_file_patterns: usize,
        max_git_marker_bytes: usize,
    ) -> PyResult<Self> {
        let ancestors = ancestors
            .into_iter()
            .map(|(prefix, priority, content)| AncestorIgnore {
                prefix: wtf8_from_raw(prefix.as_bytes()),
                priority,
                // Python already enforced the pattern limit on these files.
                rules: GitIgnore::parse(content.as_bytes(), usize::MAX)
                    .expect("no file has more than usize::MAX lines"),
            })
            .collect();
        let config = WalkConfig {
            ancestors,
            in_git_repository,
            include_ignored,
            max_directory_entries,
            max_ignore_file_bytes,
            max_ignore_file_patterns,
            max_git_marker_bytes,
        };
        let walk = Walk::new(descriptor, config)?;
        Ok(Self {
            walk: Mutex::new(Some(walk)),
        })
    }

    fn __iter__(slf: PyRef<'_, Self>) -> PyRef<'_, Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Option<PythonBatch>> {
        let batch = py.detach(|| match self.lock().as_mut() {
            Some(walk) => walk.next_batch(),
            None => Ok(None),
        });
        batch
            .map(|batch| batch.map(python_batch))
            .map_err(walk_error_to_python)
    }

    /// Closes every descriptor the walk holds; later iteration yields nothing.
    fn close(&self) {
        *self.lock() = None;
    }
}

impl NativeWalk {
    fn lock(&self) -> std::sync::MutexGuard<'_, Option<Walk>> {
        self.walk
            .lock()
            .expect("the walk lock is poisoned only by an earlier panic")
    }
}

fn python_batch(batch: Batch) -> PythonBatch {
    let files = batch
        .files
        .into_iter()
        .map(|file| {
            (
                OsString::from_vec(file.name),
                file.device,
                file.inode,
                file.hidden_or_ignored,
            )
        })
        .collect();
    (batch.descriptor, OsString::from_vec(batch.directory), files)
}

fn walk_error_to_python(error: WalkError) -> PyErr {
    match error {
        WalkError::DirectoryEntryLimit { directory } => {
            NativeWalkError::new_err(("directory_entries", OsString::from_vec(directory)))
        }
        WalkError::IgnoreFileTooLarge { file } => {
            NativeWalkError::new_err(("ignore_file_bytes", OsString::from_vec(file)))
        }
        WalkError::TooManyIgnorePatterns { file } => {
            NativeWalkError::new_err(("ignore_file_patterns", OsString::from_vec(file)))
        }
        WalkError::ListDirectory { directory, error } => NativeWalkError::new_err((
            "list_directory",
            OsString::from_vec(directory),
            error as i32,
        )),
        WalkError::Stat { name, error } => {
            NativeWalkError::new_err(("stat", OsString::from_vec(name), error as i32))
        }
    }
}
