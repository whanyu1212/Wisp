//! OSC 8 hyperlinks for links the TUI draws itself.
//!
//! Untrusted text never carries a link: the sanitizer replaces every control character
//! before text reaches the buffer. Links come only from a Markdown link's destination or
//! a file a tool card names, and both are built here into a URI made of printable ASCII,
//! so no byte of it can end the escape sequence early.
//!
//! The transcript records where it painted each link ([`record`]); the links go into the
//! buffer only once the whole frame is drawn ([`apply`]). Overlays drawn later, and the
//! copy of the screen that mouse selection reads text from, so see plain cells, and a
//! cell an overlay has since covered keeps no link.
//!
//! Each linked cell carries its own complete sequence, opening the link, then the
//! cell's text, then closing it. A link is terminal state, like a colour, and ratatui
//! writes only the cells that changed, so a link opened in one cell and closed in
//! another could stay open across everything written after it. Cells of one link share
//! an `id`, so a terminal highlights them together, even across wrapped rows.

use std::hash::{Hash, Hasher};
use std::num::NonZeroU16;
use std::path::Path;
use std::sync::{Arc, OnceLock};

use ratatui::buffer::{Buffer, Cell, CellDiffOption};
use ratatui::layout::Rect;
use unicode_width::UnicodeWidthStr;

/// Whether a terminal named `term` (`$TERM`) is drawn with links. Terminals without
/// OSC 8 ignore it, except the Linux console and `dumb`, which may show the bytes.
pub(crate) fn supported(term: Option<&str>) -> bool {
    !matches!(term, Some("dumb" | "linux"))
}

/// The longest URI a link carries; a longer one is shown as plain text.
const MAX_URI_BYTES: usize = 2048;

/// The URI for a Markdown link destination, or `None` unless it is `http` or `https`.
///
/// Any byte that is not printable ASCII, a space or a control character among them, is
/// percent-encoded.
pub(crate) fn web_uri(destination: &str) -> Option<Arc<str>> {
    let scheme_end = destination.find("://")?;
    let scheme = &destination[..scheme_end];
    if !(scheme.eq_ignore_ascii_case("http") || scheme.eq_ignore_ascii_case("https")) {
        return None;
    }
    let mut uri = String::with_capacity(destination.len());
    for &byte in destination.as_bytes() {
        if byte.is_ascii_graphic() {
            uri.push(char::from(byte));
        } else {
            push_escaped(&mut uri, byte);
        }
    }
    bounded(uri)
}

/// The `file://` URI for an absolute path on this machine, naming the host, so a
/// terminal reached over SSH does not open a local file of the same name.
pub(crate) fn file_uri(path: &Path) -> Option<Arc<str>> {
    use std::os::unix::ffi::OsStrExt;
    if !path.is_absolute() {
        return None;
    }
    let mut uri = format!("file://{}", host());
    for &byte in path.as_os_str().as_bytes() {
        // A path is not a URI yet, so `%`, `?` and `#` in a file name are escaped too.
        if byte.is_ascii_alphanumeric() || b"/-._~!$&'()*+,;=:@".contains(&byte) {
            uri.push(char::from(byte));
        } else {
            push_escaped(&mut uri, byte);
        }
    }
    bounded(uri)
}

fn push_escaped(uri: &mut String, byte: u8) {
    use std::fmt::Write as _;
    let _ = write!(uri, "%{byte:02X}");
}

fn bounded(uri: String) -> Option<Arc<str>> {
    (uri.len() <= MAX_URI_BYTES).then(|| Arc::from(uri))
}

/// This machine's host name for `file://` URIs, read once; empty if it has none that
/// fits a URI, which the URI then leaves out.
fn host() -> &'static str {
    static HOST: OnceLock<String> = OnceLock::new();
    HOST.get_or_init(|| {
        nix::unistd::gethostname()
            .ok()
            .and_then(|name| name.into_string().ok())
            .filter(|name| {
                name.bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"-._".contains(&byte))
            })
            .unwrap_or_default()
    })
}

/// One row of cells painted as a link, with the cells as they were painted.
#[derive(Debug)]
pub(crate) struct PaintedLink {
    x: u16,
    y: u16,
    cells: Vec<Cell>,
    uri: Arc<str>,
}

/// Notes that the cells of one row in `area` were painted as a link to `uri`.
pub(crate) fn record(buffer: &Buffer, area: Rect, uri: Arc<str>) -> PaintedLink {
    let cells = (area.left()..area.right())
        .filter_map(|x| buffer.cell((x, area.y)).cloned())
        .collect();
    PaintedLink {
        x: area.x,
        y: area.y,
        cells,
        uri,
    }
}

/// Makes the recorded links' cells links, in a buffer whose frame is complete.
///
/// A cell keeps its text and style. One changed since it was recorded, such as by an
/// overlay drawn over it, is left alone. A wide character's cell takes the link; the
/// blank cell it covers is left alone too, as the terminal never draws it.
pub(crate) fn apply(buffer: &mut Buffer, links: &[PaintedLink]) {
    for link in links {
        let id = link_id(&link.uri);
        let mut covered = 0;
        for (x, painted) in (link.x..).zip(&link.cells) {
            if covered > 0 {
                covered -= 1;
                continue;
            }
            let Some(cell) = buffer.cell_mut((x, link.y)).filter(|cell| *cell == painted) else {
                continue;
            };
            let symbol = cell.symbol();
            let width = u16::try_from(symbol.width()).unwrap_or(1).max(1);
            covered = width - 1;
            let linked = format!("\x1b]8;id={id};{}\x1b\\{symbol}\x1b]8;;\x1b\\", link.uri);
            cell.set_symbol(&linked);
            // The escapes take no columns; without this, ratatui would count them.
            cell.set_diff_option(CellDiffOption::ForcedWidth(
                NonZeroU16::new(width).expect("width is at least 1"),
            ));
        }
    }
}

/// A short `id` that the cells of one link share.
fn link_id(uri: &str) -> String {
    let mut hasher = std::collections::hash_map::DefaultHasher::new();
    uri.hash(&mut hasher);
    format!("{:x}", hasher.finish() & 0xffff_ffff)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn web_uri_accepts_only_web_schemes() {
        assert_eq!(
            web_uri("https://example.test/a?b=1#c").as_deref(),
            Some("https://example.test/a?b=1#c")
        );
        assert_eq!(
            web_uri("HTTP://example.test").as_deref(),
            Some("HTTP://example.test")
        );
        for rejected in [
            "javascript:alert(1)",
            "file:///etc/passwd",
            "mailto:a@example.test",
            "example.test/path",
            "ftp://example.test",
        ] {
            assert_eq!(web_uri(rejected), None, "{rejected}");
        }
    }

    #[test]
    fn uris_cannot_break_out() {
        // ESC, BEL, a space and non-ASCII are all encoded, so nothing ends the
        // sequence early or reaches the terminal raw.
        let uri = web_uri("https://example.test/\x1b]8;;\x07x y\u{202e}é").unwrap();
        assert_eq!(&*uri, "https://example.test/%1B]8;;%07x%20y%E2%80%AE%C3%A9");
        assert!(uri.bytes().all(|byte| byte.is_ascii_graphic()));
        let long = format!("https://example.test/{}", "a".repeat(MAX_URI_BYTES));
        assert_eq!(web_uri(&long), None);
    }

    #[test]
    fn file_uri_names_host_and_escapes_path() {
        let uri = file_uri(Path::new("/work/my notes/50%#1.md")).unwrap();
        assert_eq!(
            &*uri,
            format!("file://{}/work/my%20notes/50%25%231.md", host())
        );
        assert_eq!(file_uri(Path::new("relative.md")), None);
    }

    #[test]
    fn linked_cells_are_self_contained() {
        let area = Rect::new(0, 0, 6, 1);
        let mut buffer = Buffer::empty(area);
        buffer.set_string(0, 0, "a界b", ratatui::style::Style::default());
        let link = record(&buffer, Rect::new(0, 0, 4, 1), Arc::from("https://x.test"));
        apply(&mut buffer, &[link]);
        let id = link_id("https://x.test");
        let open = format!("\x1b]8;id={id};https://x.test\x1b\\");
        let close = "\x1b]8;;\x1b\\";
        assert_eq!(buffer[(0, 0)].symbol(), format!("{open}a{close}"));
        assert_eq!(buffer[(1, 0)].symbol(), format!("{open}界{close}"));
        assert_eq!(
            buffer[(1, 0)].diff_option,
            CellDiffOption::ForcedWidth(NonZeroU16::new(2).unwrap())
        );
        // The cell the wide character covers stays blank and unlinked.
        assert_eq!(buffer[(2, 0)].symbol(), " ");
        assert_eq!(buffer[(3, 0)].symbol(), format!("{open}b{close}"));
        assert_eq!(buffer[(4, 0)].symbol(), " ");
    }

    #[test]
    fn diff_writes_each_linked_cell_whole() {
        // Linking an unchanged character still changes its cell, so the next frame
        // rewrites it; and the forced width keeps later cells where they were.
        let area = Rect::new(0, 0, 4, 1);
        let mut previous = Buffer::empty(area);
        previous.set_string(0, 0, "ab", ratatui::style::Style::default());
        let mut next = previous.clone();
        let link = record(&next, Rect::new(0, 0, 2, 1), Arc::from("https://x.test"));
        apply(&mut next, &[link]);
        next.set_string(2, 0, "c", ratatui::style::Style::default());
        let updates = previous.diff(&next);
        let columns = updates.iter().map(|(x, _, _)| *x).collect::<Vec<_>>();
        assert_eq!(columns, [0, 1, 2]);
    }

    #[test]
    fn covered_cells_stay_plain() {
        // A cell drawn over after the link was recorded, as an overlay does, keeps no link.
        let area = Rect::new(0, 0, 3, 1);
        let mut buffer = Buffer::empty(area);
        buffer.set_string(0, 0, "abc", ratatui::style::Style::default());
        let link = record(&buffer, area, Arc::from("https://x.test"));
        buffer.set_string(1, 0, "│", ratatui::style::Style::default());
        apply(&mut buffer, &[link]);
        assert!(buffer[(0, 0)].symbol().starts_with("\x1b]8;"));
        assert_eq!(buffer[(1, 0)].symbol(), "│");
        assert!(buffer[(2, 0)].symbol().starts_with("\x1b]8;"));
    }
}
