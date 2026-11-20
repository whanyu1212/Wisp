//! Browser viewport view and mode switcher rendered inside [`super::SidePanel`].

use super::*;
use crate::terminal_images::{
    CellSize, ImagePreviewMeta, PaintedImage, PlacementKey, thumbnail_cells,
};
use chrono::Timelike;
use ratatui::layout::Position;
use ratatui::widgets::Paragraph;

/// Draw the `Work | Browser` tab switcher at row 0 when browser state is active.
pub(super) fn push_mode_tabs(
    rows: &mut PanelRows,
    active: PanelMode,
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
    let mut hits: Vec<(u16, u16, PanelMode)> = Vec::new();
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
        hits.push((start, end, mode));
        col += span_width;
    }

    rows.section(Section::ModeTabs, vec![Line::from(spans)]);
    for (start, end, mode) in hits {
        rows.action_range(0, start, end, PanelAction::SwitchMode(mode));
    }
}

/// Render header metadata (sanitized title, URL, dimensions, updated clock) and the
/// centered browser screenshot inside `body`.
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
) {
    let width = usize::from(body.width);
    if width == 0 || body.height == 0 {
        return;
    }
    let muted = Style::default().fg(palette.muted);
    let viewport = state.browser_viewport.as_ref();

    let mut meta: Vec<Line<'static>> = Vec::new();
    if let Some(vp) = viewport {
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
    } else {
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
        return;
    }
    // Leave one blank spacer row between metadata and the image frame when space permits.
    let spacer = u16::from(remaining_height > 2);
    let image_area = Rect {
        x: body.x,
        y: after_meta_y + spacer,
        width: body.width,
        height: remaining_height - spacer,
    };
    if image_area.is_empty() {
        return;
    }

    let Some(vp) = viewport else {
        render_placeholder(frame, image_area, "No browser frame yet.", muted);
        return;
    };
    let Some(_image_ref) = &vp.image else {
        render_placeholder(
            frame,
            image_area,
            "Metadata updated (no screenshot returned).",
            muted,
        );
        return;
    };
    let Some(preview) = preview else {
        render_placeholder(frame, image_area, "Preview unavailable.", muted);
        return;
    };
    let Some(painted_images) = painted_images else {
        return;
    };

    let (cols, rows) = thumbnail_cells(
        preview.pixel_width,
        preview.pixel_height,
        cell_size,
        image_area.width,
        image_area.height,
    );
    if cols == 0 || rows == 0 {
        render_placeholder(frame, image_area, "Viewport too small for frame.", muted);
        return;
    }

    let x = image_area.x + (image_area.width.saturating_sub(cols)) / 2;
    let y = image_area.y + (image_area.height.saturating_sub(rows)) / 2;
    let img_rect = Rect::new(x, y, cols, rows);

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
