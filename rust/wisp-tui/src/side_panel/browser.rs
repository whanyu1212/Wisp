//! Browser viewport view and panel controls rendered inside [`super::SidePanel`].

use super::*;
use crate::terminal_images::{
    CellSize, ImagePreviewMeta, PaintedImage, PlacementKey, fit_image_cells,
};
use chrono::Timelike;
use ratatui::layout::Position;
use ratatui::widgets::Paragraph;
use unicode_segmentation::UnicodeSegmentation;
use wisp_protocol::commands::LiveBrowserViewport;

use crate::prompt_editor::PromptEditor;

/// The focused address field, and on the start page the recent address that
/// Up/Down moved the selection to.
pub(super) struct AddressBar {
    pub(super) editor: PromptEditor,
    /// The highlighted row of the recent list; `None` while the field itself has focus.
    pub(super) recent: Option<usize>,
}

impl std::fmt::Debug for AddressBar {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("AddressBar([REDACTED])")
    }
}

/// The address field's text while it is empty, on the start page or a live page.
const ADDRESS_PLACEHOLDER: &str = "Search or enter address";

/// Lay out the page in CSS pixels while keeping the capture at terminal pixel density.
pub(super) fn viewport_for_cells(area: Rect, cell: CellSize) -> LiveBrowserViewport {
    // TIOCGWINSZ gives physical cell dimensions, but no monitor scale. Treat
    // cells approximately twice the default height as high-density.
    let scale = if cell.height_px >= 28 { 2 } else { 1 };
    let width = (u32::from(area.width) * u32::from(cell.width_px) / scale).max(1);
    let height = (u32::from(area.height) * u32::from(cell.height_px) / scale).max(1);
    let fit = (1600.0 / f64::from(width))
        .min(1200.0 / f64::from(height))
        .min(1.0);
    LiveBrowserViewport {
        width: (f64::from(width) * fit).round().clamp(1.0, 1600.0) as u32,
        height: (f64::from(height) * fit).round().clamp(1.0, 1200.0) as u32,
        device_scale_factor: scale,
    }
}

/// Draw the toolbar of a live page, or of the start page when `page` is `None`, and
/// return the address editor's cursor in pane cells. The start page has no history,
/// so its Back, Forward and Reload are drawn muted and take no clicks.
pub(super) fn push_toolbar(
    rows: &mut PanelRows,
    page: Option<&BrowserViewport>,
    address: Option<&AddressBar>,
    width: u16,
    palette: Palette,
) -> Option<(u16, u16)> {
    let mut nav = Vec::new();
    let mut actions = Vec::new();
    let muted = Style::default().fg(palette.muted);
    let nav_style = if page.is_some() {
        Style::default().fg(palette.primary)
    } else {
        muted
    };
    let mut col = 0;
    for (text, control) in [
        ("[‹]", BrowserControl::Back),
        ("[›]", BrowserControl::Forward),
        ("[↻]", BrowserControl::Reload),
    ] {
        if col + 3 > width {
            break;
        }
        nav.push(Span::styled(text, nav_style));
        if page.is_some() {
            actions.push((col, col + 3, control));
        }
        col += 3;
        if col < width {
            nav.push(Span::raw(" "));
            col += 1;
        }
    }
    let narrow = width < 40;
    let address_col = if narrow { 0 } else { col };
    let address_row = usize::from(narrow);
    let start = rows.lines.len() + 1;
    let available = width.saturating_sub(address_col).saturating_sub(2);
    // A recent row the start page highlights holds focus, so the field shows its
    // draft without a caret.
    let editing = address
        .filter(|address| address.recent.is_none())
        .map(|address| &address.editor);
    let idle = match (page, address) {
        (Some(page), _) => Some(page.url.as_deref().unwrap_or("Address")),
        (None, Some(address)) => Some(address.editor.text()).filter(|text| !text.is_empty()),
        (None, None) => None,
    };
    let (address, cursor) = address_text(idle, editing, available, palette);
    let mut location = vec![Span::styled("[", muted)];
    location.extend(address);
    location.push(Span::styled("]", muted));
    let lines = if narrow {
        let title = page
            .and_then(|page| page.title.as_deref())
            .map_or_else(|| "Browser".to_owned(), sanitize_for_terminal);
        nav.push(Span::styled(
            ui::clip_columns(&title, usize::from(width - col)),
            muted,
        ));
        vec![Line::from(nav), Line::from(location)]
    } else {
        nav.extend(location);
        vec![Line::from(nav)]
    };
    rows.section(Section::BrowserControls, lines);
    for (from, to, control) in actions {
        rows.action_range(0, from, to, PanelAction::BrowserControl(control));
    }
    rows.action_range(
        address_row,
        address_col,
        width,
        PanelAction::BrowserControl(BrowserControl::Address),
    );
    cursor.map(|cursor| (address_col + 1 + cursor, (start + address_row) as u16))
}

/// The address field's spans `width` columns wide, and the caret's column while
/// `editor` has focus. Without focus the field shows `idle`, or the muted
/// placeholder when there is nothing to show; so does an empty editor.
fn address_text(
    idle: Option<&str>,
    editor: Option<&PromptEditor>,
    width: u16,
    palette: Palette,
) -> (Vec<Span<'static>>, Option<u16>) {
    let style = Style::default().fg(palette.secondary);
    let placeholder = |cursor| {
        let text = ui::clip_columns(ADDRESS_PLACEHOLDER, usize::from(width));
        let pad = usize::from(width).saturating_sub(text.width());
        (
            vec![
                Span::styled(text, Style::default().fg(palette.muted)),
                Span::raw(" ".repeat(pad)),
            ],
            cursor,
        )
    };
    let Some(editor) = editor.filter(|_| width > 0) else {
        let Some(idle) = idle else {
            return placeholder(None);
        };
        let url = sanitize_for_terminal(idle);
        return (
            vec![Span::styled(
                ui::clip_columns(&url, usize::from(width)),
                style,
            )],
            None,
        );
    };
    let address = editor.text();
    if address.is_empty() {
        return placeholder(Some(0));
    }
    let mut start = editor.cursor_offset();
    let mut cursor = 0;
    for (offset, grapheme) in address[..start].grapheme_indices(true).rev() {
        let columns = grapheme.width();
        if cursor + columns >= usize::from(width) {
            break;
        }
        start = offset;
        cursor += columns;
    }
    let selection = editor.selection_range();
    let mut spans = Vec::new();
    let mut columns = 0;
    for (offset, grapheme) in address[start..].grapheme_indices(true) {
        let span_width = grapheme.width();
        if columns + span_width > usize::from(width) {
            break;
        }
        let style = if selection
            .as_ref()
            .is_some_and(|r| r.contains(&(start + offset)))
        {
            palette.text_selection()
        } else {
            style
        };
        spans.push(Span::styled(grapheme.to_owned(), style));
        columns += span_width;
    }
    spans.push(Span::raw(" ".repeat(usize::from(width) - columns)));
    (spans, Some(cursor as u16))
}

/// List the recent addresses under the start page's toolbar, newest first, with the
/// row Up/Down selected marked. Each row opens its address when clicked. Nothing is
/// drawn while there are none.
///
/// The list fills what is left of a body `height` rows tall. When the addresses do
/// not all fit, it shows a window from the top that slides down just far enough to
/// keep the selected row in view; only the rows drawn take clicks.
pub(super) fn push_recents(
    rows: &mut PanelRows,
    addresses: &[String],
    selected: Option<usize>,
    height: usize,
    width: usize,
    palette: Palette,
) {
    if addresses.is_empty() {
        return;
    }
    // The section's blank separator and its heading come first.
    let fits = height.saturating_sub(rows.lines.len() + 2).max(1);
    let first = selected
        .map_or(0, |index| (index + 1).saturating_sub(fits))
        .min(addresses.len().saturating_sub(fits));
    let shown = first..addresses.len().min(first + fits);
    let mut lines = vec![Line::styled(
        ui::clip_columns("Recent", width),
        Style::default().add_modifier(Modifier::BOLD),
    )];
    for (index, address) in shown.clone().zip(&addresses[shown.clone()]) {
        let text = ui::clip_columns(
            &sanitize_for_terminal(shown_address(address)),
            width.saturating_sub(2),
        );
        lines.push(if selected == Some(index) {
            let style = Style::default().fg(palette.primary);
            Line::from(vec![
                Span::styled("▸ ", style),
                Span::styled(text, style.add_modifier(Modifier::BOLD)),
            ])
        } else {
            Line::from(vec![Span::raw("  "), Span::raw(text)])
        });
    }
    rows.section(Section::BrowserRecents, lines);
    for (row, index) in shown.enumerate() {
        rows.action(
            row + 1,
            PanelAction::BrowserControl(BrowserControl::Recent(index)),
        );
    }
}

/// A recent address as the list shows it: without `https://`, which most addresses
/// share. Other schemes, `http://` included, stay visible.
fn shown_address(address: &str) -> &str {
    address
        .get(..8)
        .filter(|scheme| scheme.eq_ignore_ascii_case("https://"))
        .map_or(address, |_| &address[8..])
}

/// Explain why the start page offers no address field in an untrusted project.
pub(super) fn push_untrusted(rows: &mut PanelRows, width: usize, palette: Palette) {
    rows.section(
        Section::BrowserControls,
        vec![
            Line::styled(
                ui::clip_columns("Live browser unavailable", width),
                Style::default().add_modifier(Modifier::BOLD),
            ),
            Line::styled(
                ui::clip_columns("This project is not trusted.", width),
                Style::default().fg(palette.muted),
            ),
        ],
    );
}

/// Draw the panel's controls row: `INTERACT` while the page takes input, and the
/// `[Expand]` / `[Restore]` toggle at the right. A row with neither is left out.
pub(super) fn push_controls(
    rows: &mut PanelRows,
    interacting: bool,
    expanded: bool,
    width: usize,
    palette: Palette,
) {
    let mut spans = Vec::new();
    let mut col = 0usize;
    if interacting {
        let label = ui::clip_columns("INTERACT", width);
        col = label.width();
        spans.push(Span::styled(
            label,
            Style::default()
                .fg(palette.primary)
                .add_modifier(Modifier::BOLD),
        ));
    }
    let toggle_label = if expanded { "[Restore]" } else { "[Expand]" };
    let toggle_width = toggle_label.width();
    let toggle = (col + usize::from(col > 0) + toggle_width <= width).then(|| {
        spans.push(Span::raw(" ".repeat(width - col - toggle_width)));
        spans.push(Span::styled(
            toggle_label,
            Style::default().fg(palette.muted),
        ));
        let start = u16::try_from(width - toggle_width).unwrap_or(u16::MAX);
        let end = u16::try_from(width).unwrap_or(u16::MAX);
        (start, end)
    });
    if spans.is_empty() {
        return;
    }
    rows.section(Section::Controls, vec![Line::from(spans)]);
    if let Some((start, end)) = toggle {
        rows.action_range(0, start, end, PanelAction::ToggleExpand);
    }
}

/// Map a terminal cell inside `image_rect` to centered browser viewport coordinates.
pub(super) fn map_cell_to_viewport(
    image_rect: Rect,
    column: u16,
    row: u16,
    viewport_width: u32,
    viewport_height: u32,
) -> Option<(u32, u32)> {
    if image_rect.width == 0
        || image_rect.height == 0
        || viewport_width == 0
        || viewport_height == 0
        || column < image_rect.x
        || column >= image_rect.right()
        || row < image_rect.y
        || row >= image_rect.bottom()
    {
        return None;
    }
    let col_rel = u64::from(column - image_rect.x);
    let row_rel = u64::from(row - image_rect.y);
    let rect_w = u64::from(image_rect.width);
    let rect_h = u64::from(image_rect.height);
    let x_num = (2 * col_rel + 1).saturating_mul(u64::from(viewport_width));
    let y_num = (2 * row_rel + 1).saturating_mul(u64::from(viewport_height));
    let x = u32::try_from(x_num / (2 * rect_w)).unwrap_or(viewport_width - 1);
    let y = u32::try_from(y_num / (2 * rect_h)).unwrap_or(viewport_height - 1);
    Some((x.min(viewport_width - 1), y.min(viewport_height - 1)))
}

/// Render header metadata (sanitized title, URL, dimensions, updated clock) and the
/// centered browser screenshot inside `body`. Returns the painted image rectangle when
/// a screenshot frame is laid out.
#[allow(clippy::too_many_arguments)]
pub(super) fn render_browser_body(
    frame: &mut Frame<'_>,
    body: Rect,
    vp: &BrowserViewport,
    surface: Style,
    palette: Palette,
    preview: Option<&ImagePreviewMeta>,
    cell_size: CellSize,
    painted_images: Option<&mut Vec<PaintedImage>>,
    ignore_overlay_rect: bool,
) -> Option<Rect> {
    let width = usize::from(body.width);
    if width == 0 || body.height == 0 {
        return None;
    }
    let muted = Style::default().fg(palette.muted);

    let mut meta: Vec<Line<'static>> = Vec::new();
    if !vp.live {
        let title = vp
            .title
            .as_deref()
            .map(sanitize_for_terminal)
            .filter(|text| !text.is_empty())
            .unwrap_or_else(|| "Browser".to_owned());
        let right = vp
            .updated_at
            .as_deref()
            .and_then(format_clock_utc)
            .map(|clock| vec![Span::styled(clock, muted)])
            .unwrap_or_default();
        meta.push(spread(
            vec![Span::styled(
                title,
                Style::default().add_modifier(Modifier::BOLD),
            )],
            right,
            width,
        ));
        if let Some(url) = vp
            .url
            .as_deref()
            .map(sanitize_for_terminal)
            .filter(|text| !text.is_empty())
        {
            meta.push(Line::styled(
                ui::clip_columns(&url, width),
                Style::default().fg(palette.secondary),
            ));
        }
        let mut details = Vec::new();
        if let (Some(w), Some(h)) = (vp.viewport_width, vp.viewport_height) {
            details.push(format!("{w}×{h}"));
        }
        if let Some(tool) = vp
            .source_tool
            .as_deref()
            .map(sanitize_for_terminal)
            .filter(|text| !text.is_empty())
        {
            details.push(short_tool_label(&tool).to_owned());
        }
        if !details.is_empty() {
            meta.push(Line::styled(
                ui::clip_columns(&details.join(" · "), width),
                muted,
            ));
        }
    }

    let meta_rows = meta.len().min(usize::from(body.height));
    let meta_rect = Rect {
        height: meta_rows as u16,
        ..body
    };
    frame.render_widget(
        Paragraph::new(meta.into_iter().take(meta_rows).collect::<Vec<_>>()),
        meta_rect,
    );

    let after_meta_y = meta_rect.bottom();
    let remaining_height = body.bottom().saturating_sub(after_meta_y);
    if remaining_height == 0 {
        return None;
    }
    // Leave one blank spacer row between metadata and the image frame when space permits.
    let spacer = u16::from(!meta_rect.is_empty() && remaining_height > 2);
    let image_area = Rect {
        x: body.x,
        y: after_meta_y + spacer,
        width: body.width,
        height: remaining_height - spacer,
    };
    if image_area.is_empty() {
        return None;
    }

    let Some(_image_ref) = &vp.image else {
        if vp.window_mode {
            let mut lines = vec![Line::styled(
                ui::clip_columns("Live browser open in Chrome window", width),
                Style::default().add_modifier(Modifier::BOLD),
            )];
            if let Some(url) = vp
                .url
                .as_deref()
                .map(sanitize_for_terminal)
                .filter(|text| !text.is_empty())
            {
                lines.push(Line::styled(
                    ui::clip_columns(&url, width),
                    Style::default().fg(palette.secondary),
                ));
            }
            lines.push(Line::styled(
                ui::clip_columns("Run /browser live to dock it back in the terminal.", width),
                muted,
            ));
            frame.render_widget(Paragraph::new(lines), image_area);
            return None;
        }
        render_placeholder(
            frame,
            image_area,
            "Metadata updated (no screenshot returned).",
            muted,
        );
        return None;
    };
    let Some(preview) = preview else {
        render_placeholder(frame, image_area, "Preview unavailable.", muted);
        return None;
    };

    let (cols, rows) = fit_image_cells(
        preview.pixel_width,
        preview.pixel_height,
        cell_size,
        image_area.width,
        image_area.height,
        vp.live,
    );
    if cols == 0 || rows == 0 {
        render_placeholder(frame, image_area, "Viewport too small for frame.", muted);
        return None;
    }

    let x = image_area.x + (image_area.width.saturating_sub(cols)) / 2;
    let y = image_area.y + (image_area.height.saturating_sub(rows)) / 2;
    let img_rect = Rect::new(x, y, cols, rows);

    if let Some(painted_images) = painted_images {
        let buffer = frame.buffer_mut();
        let mut snapshot = Vec::with_capacity(usize::from(cols) * usize::from(rows));
        let bg = surface.bg.unwrap_or(ratatui::style::Color::Reset);
        for dy in 0..rows {
            for dx in 0..cols {
                let cell = &mut buffer[Position::new(x + dx, y + dy)];
                cell.set_symbol(" ");
                cell.set_style(surface);
                snapshot.push(cell.clone());
            }
        }

        painted_images.push(PaintedImage {
            placement: PlacementKey::BrowserViewport,
            key: preview.key.clone(),
            rect: img_rect,
            total_rows: rows,
            first_image_row: 0,
            bg,
            snapshot,
            ignore_overlay_rect,
        });
    }

    Some(img_rect)
}

fn render_placeholder(frame: &mut Frame<'_>, area: Rect, message: &str, style: Style) {
    let clipped = ui::clip_columns(message, usize::from(area.width));
    frame.render_widget(Paragraph::new(Line::styled(clipped, style)), area);
}

fn short_tool_label(tool: &str) -> &str {
    tool.strip_prefix("mcp__")
        .and_then(|rest| rest.split_once("__"))
        .map_or(tool, |(server, _)| server)
}

fn format_clock_utc(timestamp: &str) -> Option<String> {
    let dt = chrono::DateTime::parse_from_rfc3339(timestamp).ok()?;
    let utc = dt.with_timezone(&chrono::Utc);
    Some(format!(
        "{:02}:{:02}:{:02}Z",
        utc.hour(),
        utc.minute(),
        utc.second()
    ))
}
