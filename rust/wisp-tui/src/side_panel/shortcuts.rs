//! Searchable reference for resolved bindings and common contextual controls.

use std::borrow::Cow;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    text::{Line, Span},
    widgets::Paragraph,
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use super::OverlayKey;
use crate::{
    keybindings::{Action, Bindings},
    theme::Palette,
    ui::{clip_columns, sanitize_for_terminal},
};

const MAX_QUERY_CHARS: usize = 256;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Group {
    Essentials,
    Input,
    Conversation,
    Panels,
    BackgroundProcesses,
}

impl Group {
    const ALL: [Self; 5] = [
        Self::Essentials,
        Self::Input,
        Self::Conversation,
        Self::Panels,
        Self::BackgroundProcesses,
    ];

    fn name(self) -> &'static str {
        match self {
            Self::Essentials => "Essentials",
            Self::Input => "Input",
            Self::Conversation => "Conversation",
            Self::Panels => "Panels",
            Self::BackgroundProcesses => "Background processes",
        }
    }
}

struct Shortcut {
    name: &'static str,
    id: &'static str,
    chord: Cow<'static, str>,
}

impl Shortcut {
    const fn fixed(name: &'static str, chord: &'static str) -> Self {
        Self {
            name,
            id: "",
            chord: Cow::Borrowed(chord),
        }
    }
}

fn catalog(bindings: &Bindings) -> [Vec<Shortcut>; Group::ALL.len()] {
    use Group::{BackgroundProcesses, Conversation, Essentials, Input, Panels};

    let mut groups: [Vec<_>; Group::ALL.len()] = Default::default();
    for entry in bindings.help_entries() {
        let (group, order, name) = match entry.action {
            Action::Submit => (Essentials, 0, "Send prompt"),
            Action::FocusPanel => (Essentials, 2, "Focus side panel"),
            Action::Browse => (Essentials, 3, "Browse transcript"),
            Action::Newline => (Input, 0, "Insert newline"),
            Action::History => (Input, 1, "Search prompt history"),
            Action::ExternalEditor => (Input, 2, "Open external editor"),
            Action::RestoreQueue => (Input, 3, "Restore queued draft"),
            Action::AlternateSubmit => (Input, 4, "Insert newline or steer active run"),
            Action::AttachShellOutput => (Input, 5, "Attach or detach shell output"),
            Action::Find => (Conversation, 0, "Search transcript"),
            Action::CopyLastResponse => (Conversation, 1, "Copy last response"),
            Action::TurnDiff => (Conversation, 2, "View latest turn diff"),
            Action::Tail => (Conversation, 3, "Go to live tail"),
            Action::SessionTree => (Conversation, 4, "Open session tree"),
            Action::Home => (Conversation, 5, "Go to transcript start"),
            Action::PageUp => (Conversation, 6, "Scroll up one page"),
            Action::PageDown => (Conversation, 7, "Scroll down one page"),
            Action::LineUp => (Conversation, 8, "Scroll up one line"),
            Action::LineDown => (Conversation, 9, "Scroll down one line"),
            Action::ManageQueue => (Conversation, 10, "Manage queued prompts"),
            Action::TogglePanel => (Panels, 0, "Toggle side panel"),
            Action::ToggleTheme => (Panels, 3, "Toggle theme"),
            Action::BackgroundCommand => (BackgroundProcesses, 0, "Background running command"),
            Action::FocusBackground => (BackgroundProcesses, 4, "Focus background processes"),
        };
        groups[group as usize].push((
            order,
            Shortcut {
                name,
                id: entry.id,
                chord: Cow::Owned(sanitize_for_terminal(&entry.label).replace('\n', " ")),
            },
        ));
    }
    groups[Essentials as usize].push((1, Shortcut::fixed("Contextual help", "Ctrl+G")));
    groups[Panels as usize].extend([
        (1, Shortcut::fixed("Switch panel tabs", "[ / ]")),
        (2, Shortcut::fixed("Expand or restore panel", "f")),
    ]);
    groups[BackgroundProcesses as usize].extend([
        (1, Shortcut::fixed("Expand focused output", "f")),
        (2, Shortcut::fixed("Follow expanded output", "End")),
        (3, Shortcut::fixed("Return from expanded output", "Esc")),
    ]);
    groups.map(|mut rows| {
        rows.sort_unstable_by_key(|(order, _)| *order);
        rows.into_iter().map(|(_, shortcut)| shortcut).collect()
    })
}

#[derive(Debug, Default)]
struct Painted {
    close: Rect,
    filter: Rect,
    body: Rect,
    groups: Vec<(Group, usize)>,
    lines: usize,
    offset: usize,
}

impl Painted {
    fn max_offset(&self) -> usize {
        self.lines.saturating_sub(usize::from(self.body.height))
    }
}

/// Disclosure, filter and viewport state for the keyboard shortcuts reference.
#[derive(Debug)]
pub(super) struct Shortcuts {
    expanded: [bool; Group::ALL.len()],
    query: String,
    filtering: bool,
    selected: Option<Group>,
    offset: usize,
    reveal_selection: bool,
    painted: Painted,
}

impl Default for Shortcuts {
    fn default() -> Self {
        let mut expanded = [false; Group::ALL.len()];
        expanded[Group::Essentials as usize] = true;
        Self {
            expanded,
            query: String::new(),
            filtering: false,
            selected: Some(Group::Essentials),
            offset: 0,
            reveal_selection: true,
            painted: Painted::default(),
        }
    }
}

impl Shortcuts {
    /// Whether paste and printable keys belong to the filter field.
    pub(super) fn filtering(&self) -> bool {
        self.filtering
    }

    /// Append single-line text to the active filter and reset the result viewport.
    pub(super) fn paste(&mut self, text: &str) {
        if !self.filtering {
            return;
        }
        let remaining = MAX_QUERY_CHARS.saturating_sub(self.query.chars().count());
        let bounded: String = text
            .chars()
            .take(remaining)
            .map(|c| if c.is_whitespace() { ' ' } else { c })
            .collect();
        self.query.push_str(&sanitize_for_terminal(&bounded));
        self.reset_results();
    }

    fn reset_results(&mut self) {
        self.offset = 0;
        self.selected = None;
        self.reveal_selection = false;
    }

    /// Handle reference navigation and filter editing, returning `Close` after Esc clears the filter.
    pub(super) fn key(&mut self, key: KeyEvent) -> OverlayKey {
        if key.code == KeyCode::Esc {
            if self.filtering || !self.query.is_empty() {
                self.query.clear();
                self.filtering = false;
                self.reset_results();
                return OverlayKey::Moved;
            }
            return OverlayKey::Close;
        }
        if self.filtering {
            return self.filter_key(key);
        }
        if !typed(key) {
            return OverlayKey::Ignored;
        }
        match key.code {
            KeyCode::Char('/') => self.filtering = true,
            KeyCode::Up if !self.query.trim().is_empty() => self.scroll(-1),
            KeyCode::Down if !self.query.trim().is_empty() => self.scroll(1),
            KeyCode::Up | KeyCode::BackTab => self.select_group(false),
            KeyCode::Down | KeyCode::Tab => self.select_group(true),
            KeyCode::PageUp => self.scroll(-i32::from(self.painted.body.height)),
            KeyCode::PageDown => self.scroll(i32::from(self.painted.body.height)),
            KeyCode::Home => self.scroll(i32::MIN),
            KeyCode::End => self.scroll(i32::MAX),
            KeyCode::Left | KeyCode::Right | KeyCode::Enter | KeyCode::Char(' ') => {
                return self.change_group(key.code);
            }
            _ => return OverlayKey::Ignored,
        }
        OverlayKey::Moved
    }

    fn filter_key(&mut self, key: KeyEvent) -> OverlayKey {
        match key.code {
            KeyCode::Enter => self.filtering = false,
            KeyCode::Char(character) if typed(key) => {
                let mut text = [0; 4];
                self.paste(character.encode_utf8(&mut text));
            }
            KeyCode::Backspace => {
                if let Some((start, _)) = self.query.grapheme_indices(true).next_back() {
                    self.query.truncate(start);
                    self.reset_results();
                }
            }
            KeyCode::Char('u') if key.modifiers == KeyModifiers::CONTROL => {
                self.query.clear();
                self.reset_results();
            }
            KeyCode::Up => self.scroll(-1),
            KeyCode::Down => self.scroll(1),
            KeyCode::PageUp => self.scroll(-i32::from(self.painted.body.height)),
            KeyCode::PageDown => self.scroll(i32::from(self.painted.body.height)),
            _ => return OverlayKey::Ignored,
        }
        OverlayKey::Moved
    }

    fn select_group(&mut self, forward: bool) {
        let groups = &self.painted.groups;
        let (start, end) = (
            self.offset,
            self.offset + usize::from(self.painted.body.height),
        );
        let next = if let Some(index) = groups
            .iter()
            .position(|(group, _)| Some(*group) == self.selected)
        {
            groups.get(if forward {
                (index + 1).min(groups.len() - 1)
            } else {
                index.saturating_sub(1)
            })
        } else if forward {
            groups
                .iter()
                .find(|(_, row)| *row >= start)
                .or(groups.last())
        } else {
            groups
                .iter()
                .rev()
                .find(|(_, row)| *row < end)
                .or(groups.first())
        };
        if let Some((group, _)) = next {
            self.selected = Some(*group);
            self.reveal_selection = true;
        }
    }

    fn change_group(&mut self, key: KeyCode) -> OverlayKey {
        let Some(group) = self.selected.filter(|_| self.query.trim().is_empty()) else {
            return OverlayKey::Ignored;
        };
        let expanded = &mut self.expanded[group as usize];
        *expanded = match key {
            KeyCode::Left => false,
            KeyCode::Right => true,
            _ => !*expanded,
        };
        self.reveal_selection = true;
        OverlayKey::Moved
    }

    /// Hit-test controls and group headers from the last painted frame.
    pub(super) fn click(&mut self, column: u16, row: u16) -> OverlayKey {
        let position = (column, row).into();
        if self.painted.close.contains(position) {
            return OverlayKey::Close;
        }
        if self.painted.filter.contains(position) {
            self.filtering = true;
            return OverlayKey::Moved;
        }
        if !self.painted.body.contains(position) {
            return OverlayKey::Ignored;
        }
        self.filtering = false;
        let line = usize::from(row - self.painted.body.y) + self.painted.offset;
        if let Some((group, _)) = self
            .painted
            .groups
            .iter()
            .find(|(_, header)| *header == line)
        {
            self.selected = Some(*group);
            self.change_group(KeyCode::Enter);
        }
        OverlayKey::Moved
    }

    /// Move the result viewport by signed visual rows without changing the query.
    pub(super) fn scroll(&mut self, lines: i32) {
        self.offset = self
            .offset
            .saturating_add_signed(lines as isize)
            .min(self.painted.max_offset());
        self.reveal_selection = false;
        let end = self.offset + usize::from(self.painted.body.height);
        if !self
            .painted
            .groups
            .iter()
            .any(|(group, row)| Some(*group) == self.selected && (self.offset..end).contains(row))
        {
            self.selected = self
                .painted
                .groups
                .iter()
                .find(|(_, row)| (self.offset..end).contains(row))
                .map(|(group, _)| *group);
        }
    }

    /// Draw the reference inside the side panel's inner area.
    pub(super) fn render(
        &mut self,
        frame: &mut Frame<'_>,
        area: Rect,
        bindings: &Bindings,
        palette: Palette,
        focused: bool,
    ) {
        self.painted = Painted::default();
        if area.is_empty() {
            return;
        }
        let title_width = area.width.saturating_sub(2);
        self.painted.close = Rect::new(area.x + title_width, area.y, area.width - title_width, 1);
        let accent = Style::default().fg(palette.primary);
        let muted = Style::default().fg(palette.muted);
        let title = Line::styled("Keyboard shortcuts", accent.bold());
        frame.render_widget(
            Paragraph::new(title),
            Rect::new(area.x, area.y, title_width, 1),
        );
        frame.render_widget(
            Paragraph::new(Line::styled("×", muted).right_aligned()),
            self.painted.close,
        );

        let filter_height = u16::from(area.height >= 2);
        let foot_height = if area.height >= 10 {
            2
        } else {
            u16::from(area.height >= 5)
        };
        let width = usize::from(area.width);
        self.painted.filter = Rect::new(area.x, area.y + 1, area.width, filter_height);
        self.painted.body = Rect::new(
            area.x,
            area.y + 1 + filter_height,
            area.width,
            area.height - 1 - filter_height - foot_height,
        );
        self.render_filter(frame, palette, focused);

        let (lines, matches) = self.result_lines(bindings, palette, width, focused);
        self.painted.lines = lines.len();
        self.offset = self.offset.min(self.painted.max_offset());
        if self.reveal_selection {
            if let Some((_, row)) = self
                .painted
                .groups
                .iter()
                .find(|(group, _)| Some(*group) == self.selected)
            {
                let height = usize::from(self.painted.body.height).max(1);
                self.offset = self.offset.clamp(row.saturating_sub(height - 1), *row);
            }
            self.reveal_selection = false;
        }
        self.painted.offset = self.offset;
        frame.render_widget(
            Paragraph::new(
                lines
                    .into_iter()
                    .skip(self.offset)
                    .take(usize::from(self.painted.body.height))
                    .collect::<Vec<_>>(),
            ),
            self.painted.body,
        );
        if foot_height > 0 {
            let status = if !focused {
                "Click filter to search".into()
            } else if self.query.trim().is_empty() {
                if self.filtering {
                    "Esc clear filter"
                } else {
                    "/ filter · Esc back to composer"
                }
                .into()
            } else {
                let noun = if matches == 1 { "match" } else { "matches" };
                format!("{matches} {noun} · Esc clear filter")
            };
            let mut footer = vec![Line::styled(status, muted)];
            if foot_height == 2 && focused {
                let hint = if self.filtering {
                    "↑↓ scroll · Enter keep"
                } else if self.query.trim().is_empty() {
                    "↑↓ groups · Enter fold"
                } else {
                    "↑↓ scroll · / edit"
                };
                footer.push(Line::styled(hint, muted));
            }
            frame.render_widget(
                Paragraph::new(footer),
                Rect::new(area.x, area.bottom() - foot_height, area.width, foot_height),
            );
        }
    }

    fn render_filter(&self, frame: &mut Frame<'_>, palette: Palette, focused: bool) {
        let area = self.painted.filter;
        if area.is_empty() {
            return;
        }
        let active = focused && self.filtering;
        let field = if active {
            Style::default().fg(palette.primary)
        } else {
            Style::default().fg(palette.muted)
        };
        let prefix = "/ ";
        let query_width =
            usize::from(area.width).saturating_sub(prefix.len() + usize::from(active));
        let query = if self.query.is_empty() {
            "Filter by action or key…"
        } else {
            visible_query(&self.query, query_width)
        };
        let input_style = Style::default().fg(if self.query.is_empty() {
            palette.muted
        } else {
            palette.foreground
        });
        if active && usize::from(area.width) > prefix.len() {
            let cursor = if self.query.is_empty() {
                0
            } else {
                query.width() as u16
            };
            frame.set_cursor_position((area.x + prefix.len() as u16 + cursor, area.y));
        }
        frame.render_widget(
            Paragraph::new(Line::from(vec![
                Span::styled(prefix, field.bold()),
                Span::styled(query, input_style),
            ])),
            area,
        );
    }

    fn result_lines(
        &mut self,
        bindings: &Bindings,
        palette: Palette,
        width: usize,
        focused: bool,
    ) -> (Vec<Line<'static>>, usize) {
        let query = self.query.trim().to_lowercase();
        let filtered = !query.is_empty();
        let mut results = 0;
        let mut lines = Vec::new();
        for (group, mut shortcuts) in Group::ALL.into_iter().zip(catalog(bindings)) {
            if filtered && !group.name().to_lowercase().contains(&query) {
                shortcuts.retain(|shortcut| {
                    shortcut.name.to_lowercase().contains(&query)
                        || shortcut.chord.to_lowercase().contains(&query)
                        || shortcut.id.contains(&query)
                });
            }
            let count = shortcuts.len();
            if count == 0 {
                continue;
            }
            results += count;
            let expanded = filtered || self.expanded[group as usize];
            self.painted.groups.push((group, lines.len()));
            if self.selected.is_none() && self.offset == 0 {
                self.selected = Some(group);
            }
            let heading = if expanded {
                format!("▾ {}", group.name())
            } else {
                format!("▸ {}", group.name())
            };
            let count = count.to_string();
            let label_style = Style::default()
                .fg(if expanded {
                    palette.primary
                } else {
                    palette.foreground
                })
                .bold();
            let label_width = width.saturating_sub(count.len() + 1);
            let label = if label_width == 0 {
                String::new()
            } else {
                clip_columns(&heading, label_width)
            };
            let gap = width.saturating_sub(label.width() + count.len());
            let selected = focused && !self.filtering && self.selected == Some(group);
            let header = if selected {
                Line::styled(format!("{label}{:gap$}{count}", ""), palette.selection())
            } else {
                Line::from(vec![
                    Span::styled(label, label_style),
                    Span::raw(" ".repeat(gap)),
                    Span::styled(count, Style::default().fg(palette.muted)),
                ])
            };
            lines.push(header);
            if expanded {
                for shortcut in &shortcuts {
                    lines.extend(shortcut_lines(shortcut, palette, width));
                }
            }
        }
        if results == 0 {
            lines.push(Line::styled(
                "No matching shortcuts.",
                Style::default().fg(palette.muted),
            ));
        }
        (lines, results)
    }
}

fn typed(key: KeyEvent) -> bool {
    (key.modifiers - KeyModifiers::SHIFT).is_empty()
}

fn visible_query(query: &str, width: usize) -> &str {
    let mut remaining = width;
    for (start, grapheme) in query.grapheme_indices(true).rev() {
        let cells = grapheme.width();
        if cells > remaining {
            return &query[start + grapheme.len()..];
        }
        remaining -= cells;
    }
    query
}

fn shortcut_lines(shortcut: &Shortcut, palette: Palette, width: usize) -> Vec<Line<'static>> {
    let indentation = " ".repeat(2.min(width / 4));
    let available = width - indentation.len();
    let key_style = if palette.shows_bands() {
        palette.keycap().bg(palette.panel)
    } else {
        palette.keycap()
    };
    let chords: Vec<_> = shortcut
        .chord
        .split(" / ")
        .map(|chord| format!(" {chord} "))
        .collect();
    let chord_width =
        chords.iter().map(|chord| chord.width()).sum::<usize>() + chords.len().saturating_sub(1);
    if shortcut.name.width() + chord_width + 2 <= available {
        let gap = available - shortcut.name.width() - chord_width;
        let mut spans = vec![Span::raw(format!(
            "{indentation}{}{:gap$}",
            shortcut.name, ""
        ))];
        for (index, chord) in chords.into_iter().enumerate() {
            if index > 0 {
                spans.push(Span::raw(" "));
            }
            spans.push(Span::styled(chord, key_style));
        }
        return vec![Line::from(spans)];
    }

    let mut lines = wrap_words(shortcut.name, available)
        .into_iter()
        .map(|line| Line::raw(format!("{indentation}{line}")))
        .collect::<Vec<_>>();
    let mut row = vec![Span::raw(indentation.clone())];
    let mut cells = 0;
    for chord in chords {
        let parts = if chord.width() <= available {
            vec![chord]
        } else {
            wrap_words(chord.trim(), available)
        };
        for part in parts {
            let gap = usize::from(cells > 0);
            let part_width = part.width();
            if cells + gap + part_width > available {
                lines.push(Line::from(std::mem::replace(
                    &mut row,
                    vec![Span::raw(indentation.clone())],
                )));
                cells = 0;
            }
            if cells > 0 {
                row.push(Span::raw(" "));
                cells += 1;
            }
            cells += part_width;
            row.push(Span::styled(part, key_style));
        }
    }
    lines.push(Line::from(row));
    lines
}

// The result list scrolls by terminal rows, including wrapped labels and chords.
fn wrap_words(mut text: &str, width: usize) -> Vec<String> {
    let mut rows = Vec::new();
    while !text.is_empty() {
        let mut cells = 0;
        let end = text
            .grapheme_indices(true)
            .find_map(|(index, grapheme)| {
                cells += grapheme.width();
                (cells > width).then_some(if index == 0 { grapheme.len() } else { index })
            })
            .unwrap_or(text.len());
        let split = if end < text.len() && !text[end..].starts_with(char::is_whitespace) {
            text[..end]
                .rfind(char::is_whitespace)
                .filter(|&index| index > 0)
                .unwrap_or(end)
        } else {
            end
        };
        rows.push(text[..split].trim_end().to_owned());
        text = text[split..].trim_start();
    }
    rows
}

#[cfg(test)]
mod tests {
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    use ratatui::{Terminal, backend::TestBackend};

    use super::Shortcuts;
    use crate::{keybindings::Bindings, side_panel::OverlayKey, theme::Palette};

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn draw(panel: &mut Shortcuts, bindings: &Bindings, width: u16, height: u16) -> Vec<String> {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        let rendered = terminal
            .draw(|frame| panel.render(frame, frame.area(), bindings, Palette::default(), true))
            .unwrap();
        (0..height)
            .map(|y| {
                (0..width)
                    .map(|x| rendered.buffer[(x, y)].symbol())
                    .collect()
            })
            .collect()
    }

    #[test]
    fn opens_essentials_and_counts_groups() {
        let mut panel = Shortcuts::default();
        let rendered = draw(&mut panel, &Bindings::default(), 54, 18);
        for (name, count) in [
            ("▾ Essentials", "4"),
            ("▸ Input", "6"),
            ("▸ Conversation", "11"),
            ("▸ Panels", "4"),
            ("▸ Background processes", "5"),
        ] {
            assert!(
                rendered
                    .iter()
                    .any(|row| row.starts_with(name) && row.ends_with(count)),
                "{name} should show {count}"
            );
        }
        assert!(
            rendered
                .iter()
                .any(|row| row.contains("Contextual help") && row.contains("Ctrl+G"))
        );
        assert!(
            !rendered
                .iter()
                .any(|row| row.contains("Copy last response"))
        );
    }

    #[test]
    fn filter_matches_custom_chord() {
        let mut panel = Shortcuts::default();
        let bindings = Bindings::from_json(r#"{"transcript.copy_last_response":["F4"]}"#).unwrap();
        assert_eq!(panel.key(key(KeyCode::Char('/'))), OverlayKey::Moved);
        assert!(panel.filtering());
        panel.paste("f4");
        let rendered = draw(&mut panel, &bindings, 54, 12).join("\n");
        assert!(rendered.contains("▾ Conversation"));
        assert!(rendered.contains("Copy last response"));
        assert!(rendered.contains("F4"));
        assert!(rendered.contains("1 match · Esc clear filter"));
        assert!(!rendered.contains("Alt+Y"));
        assert!(!rendered.contains("Essentials"));
        panel.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
        panel.paste("transcript.copy_last_response");
        assert!(
            draw(&mut panel, &bindings, 54, 12)
                .join("\n")
                .contains("Copy last response")
        );

        assert_eq!(panel.key(key(KeyCode::Enter)), OverlayKey::Moved);
        assert!(!panel.filtering());
        assert_eq!(panel.key(key(KeyCode::Esc)), OverlayKey::Moved);
        let rendered = draw(&mut panel, &bindings, 54, 14).join("\n");
        assert!(rendered.contains("▸ Conversation"));
        assert!(!rendered.contains("Copy last response"));
        assert_eq!(panel.key(key(KeyCode::Esc)), OverlayKey::Close);
    }

    #[test]
    fn navigates_and_toggles_groups() {
        let mut panel = Shortcuts::default();
        let bindings = Bindings::default();
        draw(&mut panel, &bindings, 54, 18);
        assert_eq!(panel.key(key(KeyCode::Down)), OverlayKey::Moved);
        assert_eq!(panel.key(key(KeyCode::Enter)), OverlayKey::Moved);
        let rendered = draw(&mut panel, &bindings, 54, 18);
        assert!(rendered.iter().any(|row| row.starts_with("▾ Input")));
        assert!(
            rendered
                .iter()
                .any(|row| row.contains("Restore queued draft"))
        );
        let heading = rendered
            .iter()
            .position(|row| row.starts_with("▾ Input"))
            .unwrap() as u16;
        assert_eq!(panel.click(4, heading), OverlayKey::Moved);
        let rendered = draw(&mut panel, &bindings, 54, 18);
        assert!(rendered.iter().any(|row| row.starts_with("▸ Input")));
        assert!(
            !rendered
                .iter()
                .any(|row| row.contains("Restore queued draft"))
        );
        assert_eq!(panel.click(53, 0), OverlayKey::Close);
    }

    #[test]
    fn scrolls_narrow_filter_results() {
        let mut panel = Shortcuts::default();
        let bindings = Bindings::default();
        draw(&mut panel, &bindings, 31, 7);
        assert_eq!(panel.click(5, 1), OverlayKey::Moved);
        assert!(panel.filtering());
        panel.paste("unknown action");
        assert!(
            draw(&mut panel, &bindings, 31, 7)
                .join("\n")
                .contains("No matching shortcuts.")
        );
        assert_eq!(
            panel.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL)),
            OverlayKey::Moved
        );
        panel.paste("Background processes");
        let top = draw(&mut panel, &bindings, 31, 7).join("\n");
        assert!(top.contains("▾ Background processes"));
        assert!(!top.contains("Return from"));
        panel.scroll(i32::MAX);
        let bottom = draw(&mut panel, &bindings, 31, 7);
        assert!(bottom[..6].iter().any(|row| row.contains(" Esc ")));
        draw(&mut panel, &bindings, 1, 3);
        let resized = draw(&mut panel, &bindings, 54, 18).join("\n");
        assert!(resized.contains("Return from expanded output"));
        assert!(resized.contains("5 matches"));
    }
}
