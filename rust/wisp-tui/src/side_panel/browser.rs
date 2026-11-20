//! Browser viewport view and mode switcher rendered inside [`super::SidePanel`].

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

pub(super) struct AddressBar(pub(super) PromptEditor);

impl std::fmt::Debug for AddressBar {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("AddressBar([REDACTED])")
    }
}

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

/// Draw browser controls and return the address editor's cursor in pane cells.
pub(super) fn push_live_toolbar(
    rows: &mut PanelRows,
    viewport: &BrowserViewport,
    editor: Option<&PromptEditor>,
    width: u16,
    palette: Palette,
) -> Option<(u16, u16)> {
    let mut nav = Vec::new();
    let mut actions = Vec::new();
    let muted = Style::default().fg(palette.muted);
    let mut col = 0;
    for (text, control) in [
        ("[‹]", BrowserControl::Back),
        ("[›]", BrowserControl::Forward),
        ("[↻]", BrowserControl::Reload),
    ] {
        if col + 3 > width {
            break;
        }
        nav.push(Span::styled(text, Style::default().fg(palette.primary)));
        actions.push((col, col + 3, control));
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
    let (address, cursor) = address_text(viewport, editor, available, palette);
    let mut location = vec![Span::styled("[", muted)];
    location.extend(address);
    location.push(Span::styled("]", muted));
    let lines = if narrow {
        let title = sanitize_for_terminal(viewport.title.as_deref().unwrap_or("Browser"));
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

fn address_text(
    viewport: &BrowserViewport,
    editor: Option<&PromptEditor>,
    width: u16,
    palette: Palette,
) -> (Vec<Span<'static>>, Option<u16>) {
    let style = Style::default().fg(palette.secondary);
    let Some(editor) = editor.filter(|_| width > 0) else {
        let url = sanitize_for_terminal(viewport.url.as_deref().unwrap_or("Address"));
        return (
            vec![Span::styled(
                ui::clip_columns(&url, usize::from(width)),
                style,
            )],
            None,
        );
    };
    let address = editor.text();
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

/// Draw the `Work | Browser` tab switcher and right-aligned `Expand` / `Restore` toggle at row 0.
pub(super) fn push_mode_tabs(
    rows: &mut PanelRows,
    active: PanelMode,
    interacting: bool,
    expanded: bool,
    width: usize,
    palette: Palette,
) {
    let tabs = [
        ("Work", PanelMode::LiveWork),
        ("Browser", PanelMode::Browser),
    ];
    let muted = Style::default().fg(palette.muted);
    let active_style = Style::default()
        .fg(palette.primary)
        .add_modifier(Modifier::BOLD);
    let sep = " · ";

    let mut spans = Vec::new();
    let mut hits: Vec<(u16, u16, PanelAction)> = Vec::new();
    let mut col = 0usize;

    for (index, (label, mode)) in tabs.into_iter().enumerate() {
        if index > 0 {
            if col + sep.width() >= width {
                break;
            }
            spans.push(Span::styled(sep, muted));
            col += sep.width();
        }
        let room = width.saturating_sub(col);
        if room == 0 {
            break;
        }
        let clipped = ui::clip_columns(label, room);
        let span_width = clipped.width();
        if span_width == 0 {
            break;
        }
        let start = u16::try_from(col).unwrap_or(u16::MAX);
        let end = u16::try_from(col + span_width).unwrap_or(u16::MAX);
        let style = if mode == active { active_style } else { muted };
        spans.push(Span::styled(clipped, style));
        hits.push((start, end, PanelAction::SwitchMode(mode)));
        col += span_width;
    }

    if interacting && active == PanelMode::Browser && col + sep.width() < width {
        spans.push(Span::styled(sep, muted));
        col += sep.width();
        let clipped = ui::clip_columns("INTERACT", width.saturating_sub(col));
        if !clipped.is_empty() {
            col += clipped.width();
            spans.push(Span::styled(clipped, active_style));
        }
    }

    let toggle_label = if expanded { "[Restore]" } else { "[Expand]" };
    let toggle_width = toggle_label.width();
    if col + 1 + toggle_width <= width {
        let gap = width - col - toggle_width;
        spans.push(Span::raw(" ".repeat(gap)));
        let start = u16::try_from(width - toggle_width).unwrap_or(u16::MAX);
        let end = u16::try_from(width).unwrap_or(u16::MAX);
        spans.push(Span::styled(toggle_label, muted));
        hits.push((start, end, PanelAction::ToggleExpand));
    }

    rows.section(Section::ModeTabs, vec![Line::from(spans)]);
    for (start, end, action) in hits {
        rows.action_range(0, start, end, action);
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
    state: &UiState,
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
    let viewport = state.browser_viewport.as_ref();

    let mut meta: Vec<Line<'static>> = Vec::new();
    if let Some(vp) = viewport.filter(|vp| !vp.live) {
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
    } else if viewport.is_none() {
        meta.push(Line::styled(
            ui::clip_columns("Browser", width),
            Style::default().add_modifier(Modifier::BOLD),
        ));
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

    let Some(vp) = viewport else {
        render_placeholder(frame, image_area, "No browser frame yet.", muted);
        return None;
    };
    let Some(_image_ref) = &vp.image else {
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
