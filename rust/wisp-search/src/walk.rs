//! A descriptor-relative directory walk that applies ignore files.
//!
//! Ports `_walk_directory` from `wisp.tools.search.tools`, which stays the
//! reference and the fallback. Every directory is opened relative to its parent
//! with `O_NOFOLLOW`, so a directory renamed or replaced by a link mid-walk is
//! skipped rather than reached by path, and every directory on the current branch
//! stays open so callers can open files relative to it. Protected paths, display
//! paths and glob matching stay in Python.
//!
//! The walk yields files in Python's order: entries sorted by name, each
//! subdirectory walked where it sorts. A [`Batch`] is a run of consecutive files
//! in one directory, so a directory whose files surround a subdirectory yields
//! two batches. A batch ends after at most [`ENTRIES_PER_BATCH`] entries, and may
//! then be empty, so a caller can stop between batches even in a tree whose files
//! are all ignored.

use std::fmt;
use std::io;
use std::os::fd::{AsRawFd, RawFd};

use nix::dir::Dir;
use nix::errno::Errno;
use nix::fcntl::{AtFlags, FcntlArg, OFlag, fcntl, openat};
use nix::sys::stat::{FileStat, Mode, SFlag, fstat, fstatat};
use nix::unistd::{close, read};

use crate::gitignore::{GitIgnore, PatternLimitExceeded, may_reinclude_descendant, wtf8_from_raw};

/// Limits and inherited state for one walk.
#[derive(Debug)]
pub struct WalkConfig {
    /// Ignore files read in directories above the root, in discovery order.
    pub ancestors: Vec<AncestorIgnore>,
    /// Whether a directory above the root is a Git repository root.
    pub in_git_repository: bool,
    /// Descend into ignored directories and report hidden or ignored files, for a
    /// caller that re-includes some of them by glob.
    pub include_ignored: bool,
    /// Entries one directory may hold before the walk fails.
    pub max_directory_entries: usize,
    /// Bytes one ignore file may hold before the walk fails.
    pub max_ignore_file_bytes: usize,
    /// Lines one ignore file may hold before the walk fails.
    pub max_ignore_file_patterns: usize,
    /// Bytes read from a `.git` file when checking for a worktree marker.
    pub max_git_marker_bytes: usize,
}

/// An ignore file from a directory above the walk root.
#[derive(Debug)]
pub struct AncestorIgnore {
    /// WTF-8 path from the file's directory to the root, ending in `/`.
    pub prefix: Vec<u8>,
    /// 0 for `.git/info/exclude`, then `.gitignore`, `.ignore` and `.rgignore`.
    pub priority: u8,
    /// The compiled rules.
    pub rules: GitIgnore,
}

/// A run of consecutive files in one directory.
#[derive(Debug)]
pub struct Batch {
    /// The directory's descriptor, open until the walk advances or is dropped.
    pub descriptor: RawFd,
    /// Raw path of the directory relative to the root; empty for the root.
    pub directory: Vec<u8>,
    /// Regular files, in walk order.
    pub files: Vec<WalkedFile>,
}

/// A regular file found by the walk.
#[derive(Debug)]
pub struct WalkedFile {
    /// Raw entry name.
    pub name: Vec<u8>,
    /// `st_dev` from the no-follow stat taken during the walk.
    pub device: u64,
    /// `st_ino` from the same stat.
    pub inode: u64,
    /// Whether the file is hidden or ignored. Such files are reported only when
    /// [`WalkConfig::include_ignored`] is set.
    pub hidden_or_ignored: bool,
}

/// Why a walk stopped. Paths are raw and relative to the root.
#[derive(Debug)]
pub enum WalkError {
    /// A directory holds more entries than allowed.
    DirectoryEntryLimit { directory: Vec<u8> },
    /// An ignore file holds more bytes than allowed.
    IgnoreFileTooLarge { file: Vec<u8> },
    /// An ignore file holds more lines than allowed.
    TooManyIgnorePatterns { file: Vec<u8> },
    /// Reading a directory's entries failed.
    ListDirectory { directory: Vec<u8>, error: Errno },
    /// A no-follow stat of an entry failed for a reason other than its removal.
    /// Holds the entry's name, as Python's `DirEntry.stat` reports it.
    Stat { name: Vec<u8>, error: Errno },
}

impl fmt::Display for WalkError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DirectoryEntryLimit { directory } => write!(
                formatter,
                "directory {} exceeds the entry limit",
                String::from_utf8_lossy(directory)
            ),
            Self::IgnoreFileTooLarge { file } => write!(
                formatter,
                "ignore file {} exceeds the byte limit",
                String::from_utf8_lossy(file)
            ),
            Self::TooManyIgnorePatterns { file } => write!(
                formatter,
                "ignore file {} exceeds the pattern limit",
                String::from_utf8_lossy(file)
            ),
            Self::ListDirectory { directory, error } => write!(
                formatter,
                "could not list directory {}: {error}",
                String::from_utf8_lossy(directory)
            ),
            Self::Stat { name, error } => write!(
                formatter,
                "could not stat {}: {error}",
                String::from_utf8_lossy(name)
            ),
        }
    }
}

impl std::error::Error for WalkError {}

/// A lazy walk below one already-authorized directory.
#[derive(Debug)]
pub struct Walk {
    root: Option<Dir>,
    in_git_repository: bool,
    include_ignored: bool,
    limits: Limits,
    rules: Vec<ActiveRules>,
    frames: Vec<Frame>,
}

#[derive(Clone, Copy, Debug)]
struct Limits {
    directory_entries: usize,
    ignore_file_bytes: usize,
    ignore_file_patterns: usize,
    git_marker_bytes: usize,
}

#[derive(Debug)]
struct ActiveRules {
    rules: GitIgnore,
    priority: u8,
    base: RulesBase,
}

#[derive(Debug)]
enum RulesBase {
    /// Prepend this WTF-8 prefix to a root-relative path.
    Ancestor(Vec<u8>),
    /// Strip this many bytes of root-relative WTF-8 path, plus the slash after them.
    Walked(usize),
}

#[derive(Debug)]
struct Frame {
    directory: Dir,
    raw_path: Vec<u8>,
    wtf8_path: Vec<u8>,
    entries: Vec<Entry>,
    next: usize,
    /// The stat of `entries[next]`, kept when a batch is returned before it.
    next_stat: Option<FileStat>,
    rules_len: usize,
    in_git_repository: bool,
}

#[derive(Debug)]
struct Entry {
    raw: Vec<u8>,
    wtf8: Vec<u8>,
}

/// Entries examined before a batch is returned even if it holds no files.
pub const ENTRIES_PER_BATCH: usize = 1024;
const IGNORE_FILES: [(&[u8], u8); 3] = [(b".gitignore", 1), (b".ignore", 2), (b".rgignore", 3)];
const HIGHEST_PRIORITY: u8 = 3;

impl Walk {
    /// Starts a walk at the directory open as `root`. The walk duplicates the
    /// descriptor, so the caller still owns and closes `root`.
    ///
    /// # Errors
    ///
    /// Returns the error from duplicating `root` or reading it as a directory.
    pub fn new(root: RawFd, config: WalkConfig) -> io::Result<Self> {
        // `Dir::from_fd` closes the duplicate if it fails.
        let duplicate = fcntl(root, FcntlArg::F_DUPFD_CLOEXEC(0))?;
        let root = Dir::from_fd(duplicate)?;
        let rules = config
            .ancestors
            .into_iter()
            .map(|ancestor| ActiveRules {
                rules: ancestor.rules,
                priority: ancestor.priority,
                base: RulesBase::Ancestor(ancestor.prefix),
            })
            .collect();
        Ok(Self {
            root: Some(root),
            in_git_repository: config.in_git_repository,
            include_ignored: config.include_ignored,
            limits: Limits {
                directory_entries: config.max_directory_entries,
                ignore_file_bytes: config.max_ignore_file_bytes,
                ignore_file_patterns: config.max_ignore_file_patterns,
                git_marker_bytes: config.max_git_marker_bytes,
            },
            rules,
            frames: Vec::new(),
        })
    }

    /// Returns the next run of files, or `None` when the walk is complete.
    ///
    /// The previous batch's descriptor may be closed by this call.
    ///
    /// # Errors
    ///
    /// Returns a [`WalkError`] for a limit, a directory that cannot be listed, or
    /// an entry that cannot be inspected. The walk should not be resumed after one.
    pub fn next_batch(&mut self) -> Result<Option<Batch>, WalkError> {
        if let Some(root) = self.root.take() {
            self.enter(root, Vec::new(), Vec::new(), self.in_git_repository)?;
        }
        let mut files = Vec::new();
        let mut examined = 0;
        loop {
            let Some(frame) = self.frames.last_mut() else {
                return Ok(None);
            };
            if examined == ENTRIES_PER_BATCH {
                return Ok(Some(frame.batch(files)));
            }
            examined += 1;
            if frame.next == frame.entries.len() {
                if !files.is_empty() {
                    return Ok(Some(frame.batch(files)));
                }
                let finished = self.frames.pop().expect("a frame was just inspected");
                self.rules.truncate(finished.rules_len);
                continue;
            }
            let entry = &frame.entries[frame.next];
            let stat = match frame.next_stat.take() {
                Some(stat) => stat,
                None => match fstatat(
                    Some(frame.directory.as_raw_fd()),
                    entry.raw.as_slice(),
                    AtFlags::AT_SYMLINK_NOFOLLOW,
                ) {
                    Ok(stat) => stat,
                    Err(Errno::ENOENT) => {
                        frame.next += 1;
                        continue;
                    }
                    Err(error) => {
                        return Err(WalkError::Stat {
                            name: entry.raw.clone(),
                            error,
                        });
                    }
                },
            };
            let kind = SFlag::from_bits_truncate(stat.st_mode) & SFlag::S_IFMT;
            if kind == SFlag::S_IFDIR && !files.is_empty() {
                // Files before this directory form their own batch.
                frame.next_stat = Some(stat);
                return Ok(Some(frame.batch(files)));
            }
            frame.next += 1;
            let wtf8_path = join(&frame.wtf8_path, &entry.wtf8);
            if kind == SFlag::S_IFDIR {
                if is_hidden(&entry.raw) || self.prunes_directory(&wtf8_path) {
                    continue;
                }
                let frame = self.frames.last().expect("the current frame is live");
                let entry = &frame.entries[frame.next - 1];
                let Some(child) = open_child_directory(frame.directory.as_raw_fd(), &entry.raw)
                else {
                    // A rename, replacement or new link invalidates the entry; skip it
                    // rather than fall back to path traversal.
                    continue;
                };
                let raw_path = join(&frame.raw_path, &entry.raw);
                let in_git_repository = frame.in_git_repository;
                self.enter(child, raw_path, wtf8_path, in_git_repository)?;
                continue;
            }
            if kind != SFlag::S_IFREG {
                continue;
            }
            let hidden_or_ignored = is_hidden(&entry.raw) || self.is_ignored(&wtf8_path, false);
            if hidden_or_ignored && !self.include_ignored {
                continue;
            }
            let frame = self.frames.last().expect("the current frame is live");
            let (device, inode) = file_identity(&stat);
            files.push(WalkedFile {
                name: frame.entries[frame.next - 1].raw.clone(),
                device,
                inode,
                hidden_or_ignored,
            });
        }
    }

    fn enter(
        &mut self,
        mut directory: Dir,
        raw_path: Vec<u8>,
        wtf8_path: Vec<u8>,
        parent_in_git_repository: bool,
    ) -> Result<(), WalkError> {
        let descriptor = directory.as_raw_fd();
        let in_git_repository = parent_in_git_repository
            || is_git_repository_root(descriptor, self.limits.git_marker_bytes);
        let rules_len = self.rules.len();
        let found = read_ignore_files(descriptor, &raw_path, in_git_repository, self.limits)?;
        for (rules, priority) in found {
            self.rules.push(ActiveRules {
                rules,
                priority,
                base: RulesBase::Walked(wtf8_path.len()),
            });
        }
        let entries = list_entries(&mut directory, &raw_path, self.limits.directory_entries)?;
        self.frames.push(Frame {
            directory,
            raw_path,
            wtf8_path,
            entries,
            next: 0,
            next_stat: None,
            rules_len,
            in_git_repository,
        });
        Ok(())
    }

    /// Ports `_is_ignored`: the last deciding rule by priority, then discovery order.
    fn is_ignored(&self, wtf8_path: &[u8], is_directory: bool) -> bool {
        let mut ignored = false;
        for active in self.ordered_rules() {
            let relative = active.relative(wtf8_path, is_directory);
            if let Some(found) = active.rules.check(&relative) {
                ignored = found.ignored;
            }
        }
        ignored
    }

    fn prunes_directory(&self, wtf8_path: &[u8]) -> bool {
        if self.include_ignored || !self.is_ignored(wtf8_path, true) {
            return false;
        }
        let relative: Vec<(&GitIgnore, Vec<u8>)> = self
            .ordered_rules()
            .map(|active| (&active.rules, active.relative(wtf8_path, true)))
            .collect();
        let borrowed: Vec<(&GitIgnore, &[u8])> = relative
            .iter()
            .map(|(rules, path)| (*rules, path.as_slice()))
            .collect();
        !may_reinclude_descendant(&borrowed)
    }

    fn ordered_rules(&self) -> impl Iterator<Item = &ActiveRules> {
        (0..=HIGHEST_PRIORITY).flat_map(move |priority| {
            self.rules
                .iter()
                .filter(move |active| active.priority == priority)
        })
    }
}

impl ActiveRules {
    fn relative(&self, wtf8_path: &[u8], is_directory: bool) -> Vec<u8> {
        let mut relative = match &self.base {
            RulesBase::Ancestor(prefix) => [prefix.as_slice(), wtf8_path].concat(),
            RulesBase::Walked(0) => wtf8_path.to_vec(),
            RulesBase::Walked(length) => wtf8_path[length + 1..].to_vec(),
        };
        if is_directory {
            relative.push(b'/');
        }
        relative
    }
}

impl Frame {
    fn batch(&self, files: Vec<WalkedFile>) -> Batch {
        Batch {
            descriptor: self.directory.as_raw_fd(),
            directory: self.raw_path.clone(),
            files,
        }
    }
}

/// Closes a descriptor that is not a directory stream.
struct Descriptor(RawFd);

impl Drop for Descriptor {
    fn drop(&mut self) {
        // Nothing useful can be done about a failed close of a read-only descriptor.
        let _ = close(self.0);
    }
}

fn directory_flags() -> OFlag {
    OFlag::O_RDONLY | OFlag::O_DIRECTORY | OFlag::O_NOFOLLOW | OFlag::O_CLOEXEC
}

/// `O_NONBLOCK` keeps a FIFO with an ignore file's name from blocking the walk.
fn file_flags() -> OFlag {
    OFlag::O_RDONLY | OFlag::O_NOFOLLOW | OFlag::O_NONBLOCK | OFlag::O_CLOEXEC
}

fn open_child_directory(parent: RawFd, name: &[u8]) -> Option<Dir> {
    Dir::openat(Some(parent), name, directory_flags(), Mode::empty()).ok()
}

fn open_at(parent: RawFd, name: &[u8], flags: OFlag) -> Option<Descriptor> {
    openat(Some(parent), name, flags, Mode::empty())
        .ok()
        .map(Descriptor)
}

fn list_entries(
    directory: &mut Dir,
    raw_path: &[u8],
    max_entries: usize,
) -> Result<Vec<Entry>, WalkError> {
    let mut entries = Vec::new();
    for entry in directory.iter() {
        let entry = entry.map_err(|error| WalkError::ListDirectory {
            directory: raw_path.to_vec(),
            error,
        })?;
        let raw = entry.file_name().to_bytes();
        if raw == b"." || raw == b".." {
            continue;
        }
        entries.push(Entry {
            raw: raw.to_vec(),
            wtf8: wtf8_from_raw(raw),
        });
        if entries.len() > max_entries {
            return Err(WalkError::DirectoryEntryLimit {
                directory: raw_path.to_vec(),
            });
        }
    }
    entries.sort_unstable_by(|left, right| left.wtf8.cmp(&right.wtf8));
    Ok(entries)
}

/// Ports `_read_ignore_specs` and `_read_repository_exclude`.
fn read_ignore_files(
    directory: RawFd,
    raw_path: &[u8],
    in_git_repository: bool,
    limits: Limits,
) -> Result<Vec<(GitIgnore, u8)>, WalkError> {
    let mut found = Vec::new();
    let exclude = in_git_repository
        .then(|| open_repository_exclude(directory))
        .flatten();
    if let Some(exclude) = exclude {
        let file = join(raw_path, b".git/info/exclude");
        if let Some(rules) = read_ignore_file(exclude, file, limits)? {
            found.push((rules, 0));
        }
    }
    for (name, priority) in IGNORE_FILES {
        if name == b".gitignore" && !in_git_repository {
            continue;
        }
        let Some(ignore_file) = open_at(directory, name, file_flags()) else {
            continue;
        };
        if let Some(rules) = read_ignore_file(ignore_file, join(raw_path, name), limits)? {
            found.push((rules, priority));
        }
    }
    Ok(found)
}

fn open_repository_exclude(directory: RawFd) -> Option<Descriptor> {
    let git = open_at(directory, b".git", directory_flags())?;
    let info = open_at(git.0, b"info", directory_flags())?;
    open_at(info.0, b"exclude", file_flags())
}

/// Reads one ignore file, or `None` when it is not a readable regular file.
fn read_ignore_file(
    file: Descriptor,
    path: Vec<u8>,
    limits: Limits,
) -> Result<Option<GitIgnore>, WalkError> {
    if !fstat(file.0).is_ok_and(|stat| is_regular(&stat)) {
        return Ok(None);
    }
    let Some(content) = read_bounded(&file, limits.ignore_file_bytes + 1) else {
        return Ok(None);
    };
    if content.len() > limits.ignore_file_bytes {
        return Err(WalkError::IgnoreFileTooLarge { file: path });
    }
    GitIgnore::parse(&content, limits.ignore_file_patterns)
        .map(Some)
        .map_err(|PatternLimitExceeded| WalkError::TooManyIgnorePatterns { file: path })
}

/// Ports `_is_git_repository_root`: a `.git` directory, or a `.git` file whose
/// first line is a non-empty `gitdir: ` marker.
fn is_git_repository_root(directory: RawFd, max_marker_bytes: usize) -> bool {
    let Some(marker) = open_at(directory, b".git", file_flags()) else {
        return false;
    };
    let Ok(stat) = fstat(marker.0) else {
        return false;
    };
    let kind = SFlag::from_bits_truncate(stat.st_mode) & SFlag::S_IFMT;
    if kind == SFlag::S_IFDIR {
        return true;
    }
    if kind != SFlag::S_IFREG {
        return false;
    }
    let Some(content) = read_bounded(&marker, max_marker_bytes + 1) else {
        return false;
    };
    if content.len() > max_marker_bytes {
        return false;
    }
    let first_line = content
        .split(|&byte| byte == b'\n' || byte == b'\r')
        .next()
        .unwrap_or_default();
    first_line.strip_prefix(b"gitdir: ").is_some_and(|target| {
        // Python's `bytes.strip`; the line holds no newline or carriage return.
        target
            .iter()
            .any(|byte| !matches!(byte, b' ' | b'\t' | b'\x0b' | b'\x0c'))
    })
}

/// Reads up to `limit` bytes, or `None` if a read fails.
fn read_bounded(file: &Descriptor, limit: usize) -> Option<Vec<u8>> {
    let mut content = Vec::new();
    let mut buffer = [0_u8; 64 * 1024];
    while content.len() < limit {
        let wanted = buffer.len().min(limit - content.len());
        match read(file.0, &mut buffer[..wanted]) {
            Ok(0) => break,
            Ok(count) => content.extend_from_slice(&buffer[..count]),
            Err(Errno::EINTR) => {}
            Err(_) => return None,
        }
    }
    Some(content)
}

fn is_regular(stat: &FileStat) -> bool {
    SFlag::from_bits_truncate(stat.st_mode) & SFlag::S_IFMT == SFlag::S_IFREG
}

fn is_hidden(name: &[u8]) -> bool {
    name.first() == Some(&b'.')
}

/// `st_dev` and `st_ino` as Python's `os.stat_result` reports them.
#[allow(clippy::unnecessary_cast)]
fn file_identity(stat: &FileStat) -> (u64, u64) {
    // `dev_t` is `i32` on macOS and `u64` on Linux. CPython widens it as unsigned,
    // except `NODEV`, which is never a regular file's device.
    (stat.st_dev as u64, stat.st_ino as u64)
}

fn join(directory: &[u8], name: &[u8]) -> Vec<u8> {
    if directory.is_empty() {
        return name.to_vec();
    }
    [directory, b"/", name].concat()
}
