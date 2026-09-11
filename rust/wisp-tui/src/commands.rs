//! Local command routing and presentation over backend-owned discovery metadata.

use crate::model_picker::{self, ModelCommand};
use crate::prompt_editor::PromptEditor;
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::Line,
    widgets::{Block, Borders, List, ListItem, ListState, Paragraph},
};
use std::ops::Range;
use wisp_protocol::{commands::AgentMode, events::CommandDescriptor};

pub(crate) enum Command {
    Help,
    Mode(AgentMode),
    Quit,
    Model(ModelCommand),
    Session(SessionCommand),
    Invalid(String),
}

/// Actual Rust syntax; descriptions and display order come from discovery.
fn usage(name: &str) -> Option<&'static str> {
    Some(match name {
        "help" => "/help",
        "plan" => "/plan",
        "build" => "/build",
        "quit" => "/quit",
        "model" => "/model [provider::model] [effort|-]",
        "provider" => "/provider [name]",
        "connect" => "/connect",
        "resume" => "/resume [session-id]",
        "new" => "/new",
        "name" => "/name <display name> | /name --clear",
        "clone" => "/clone",
        "tree" => "/tree",
        "unrevert" => "/unrevert",
        _ => return None,
    })
}

pub(crate) fn classify(text: &str, catalog: Option<&[CommandDescriptor]>) -> Option<Command> {
    if text.contains(['\n', '\r']) {
        return None;
    }
    let trimmed = text.trim();
    let token = trimmed.split_whitespace().next()?;
    if token.starts_with("/skill:") {
        return None;
    }
    let lower = token.to_ascii_lowercase();
    let name = lower.strip_prefix('/').unwrap_or(&lower);
    let canonical = if matches!(lower.as_str(), "/exit" | ":q") {
        "quit"
    } else if lower.starts_with('/') && usage(name).is_some() {
        name
    } else if let Some(descriptor) = catalog.unwrap_or_default().iter().find(|descriptor| {
        descriptor.slash_command.eq_ignore_ascii_case(token)
            || descriptor
                .slash_aliases
                .iter()
                .any(|alias| alias.eq_ignore_ascii_case(token))
    }) {
        &descriptor.name
    } else {
        let bare_word = trimmed == token
            && lower.starts_with('/')
            && name.starts_with(|c: char| c.is_ascii_alphabetic())
            && name.chars().all(|c| c.is_ascii_alphabetic() || c == '-');
        return bare_word
            .then(|| Command::Invalid(format!("Unknown command: {token}. Use /help.")));
    };
    let Some(syntax) = usage(canonical) else {
        return Some(Command::Invalid(format!(
            "{token} is not available in the Rust TUI yet."
        )));
    };
    let tail = &trimmed[token.len()..];
    let normalized = format!("/{canonical}{tail}");
    Some(match canonical {
        "help" | "plan" | "build" | "quit" if !tail.trim().is_empty() => {
            Command::Invalid(format!("Usage: {syntax}"))
        }
        "help" => Command::Help,
        "plan" => Command::Mode(AgentMode::Plan),
        "build" => Command::Mode(AgentMode::Build),
        "quit" => Command::Quit,
        "model" | "provider" => {
            Command::Model(model_picker::command(&normalized).expect("known model command"))
        }
        _ => Command::Session(session_command(&normalized)),
    })
}

fn token_context(editor: &PromptEditor) -> Option<(Range<usize>, String)> {
    let text = editor.text();
    if text.contains(['\n', '\r']) {
        return None;
    }
    let trimmed = text.trim_start();
    let start = text.len() - trimmed.len();
    let token = trimmed.split_whitespace().next()?;
    let end = start + token.len();
    if !token.starts_with('/') || !(start + 1..=end).contains(&editor.cursor_offset()) {
        return None;
    }
    Some((start..end, token.to_owned()))
}

#[derive(Default)]
pub(crate) struct Completion {
    context: Option<(Range<usize>, String)>,
    selected: usize,
    dismissed: bool,
    rendered: Option<String>,
}

pub(crate) struct CompletionView<'a> {
    pub items: Vec<&'a CommandDescriptor>,
    pub selected: usize,
}

impl Completion {
    pub fn sync(&mut self, editor: &PromptEditor) {
        let context = token_context(editor);
        if self.context != context {
            self.context = context;
            self.selected = 0;
            self.dismissed = false;
            self.rendered = None;
        }
    }

    pub fn invalidate(&mut self) {
        self.rendered = None;
    }

    pub fn dismiss(&mut self) {
        self.dismissed = true;
        self.invalidate();
    }

    pub fn view<'a>(&self, catalog: Option<&'a [CommandDescriptor]>) -> Option<CompletionView<'a>> {
        if self.dismissed {
            return None;
        }
        let (_, prefix) = self.context.as_ref()?;
        let items: Vec<_> = catalog?
            .iter()
            .filter(|item| {
                usage(&item.name).is_some()
                    && item
                        .slash_command
                        .to_ascii_lowercase()
                        .starts_with(&prefix.to_ascii_lowercase())
            })
            .collect();
        if items.is_empty() {
            return None;
        }
        let selected = self.selected.min(items.len() - 1);
        Some(CompletionView { items, selected })
    }

    pub fn move_selection(&mut self, down: bool, count: usize) {
        self.selected = if down {
            (self.selected + 1) % count
        } else {
            (self.selected + count - 1) % count
        };
        self.invalidate();
    }

    pub fn mark_rendered(&mut self, name: String) {
        self.rendered = Some(name);
    }

    /// A selection may fill text only after that exact choice has been displayed.
    pub fn replacement(
        &self,
        item: &CommandDescriptor,
        editor: &PromptEditor,
    ) -> Option<(Range<usize>, String)> {
        if self.rendered.as_deref() != Some(&item.name) {
            return None;
        }
        let (range, _) = self.context.as_ref()?;
        let mut replacement = item.slash_command.clone();
        if editor.text()[range.end..].is_empty() && usage(&item.name)?.contains(' ') {
            replacement.push(' ');
        }
        Some((range.clone(), replacement))
    }

    pub fn is_exact(&self, item: &CommandDescriptor) -> bool {
        self.context
            .as_ref()
            .is_some_and(|(_, token)| token.eq_ignore_ascii_case(&item.slash_command))
    }
}

pub(crate) fn render_completion(frame: &mut Frame<'_>, area: Rect, view: &CompletionView<'_>) {
    let items = view.items.iter().map(|item| {
        ListItem::new(format!(
            "{}  {}",
            item.slash_command,
            crate::ui::sanitize_for_terminal(&item.description)
        ))
    });
    let mut state = ListState::default().with_selected(Some(view.selected));
    frame.render_stateful_widget(
        List::new(items)
            .highlight_style(
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            )
            .highlight_symbol("› "),
        area,
        &mut state,
    );
}

#[derive(Default)]
pub(crate) struct Help {
    pub offset: usize,
}

impl Help {
    pub fn scroll(&mut self, key: crossterm::event::KeyCode, rows: usize) {
        use crossterm::event::KeyCode;
        self.offset = match key {
            KeyCode::Up => self.offset.saturating_sub(1),
            KeyCode::Down => self.offset.saturating_add(1),
            KeyCode::PageUp => self.offset.saturating_sub(5),
            KeyCode::PageDown => self.offset.saturating_add(5),
            KeyCode::Home => 0,
            KeyCode::End => rows.saturating_sub(1),
            _ => self.offset,
        }
        .min(rows.saturating_sub(1));
    }
}

pub(crate) fn help_rows(catalog: Option<&[CommandDescriptor]>) -> Vec<Line<'static>> {
    catalog
        .unwrap_or_default()
        .iter()
        .filter_map(|item| {
            let syntax = usage(&item.name)?;
            let mut rows = vec![
                Line::styled(syntax, Style::default().fg(Color::Cyan)),
                Line::raw(crate::ui::sanitize_for_terminal(&item.description)),
            ];
            if !item.slash_aliases.is_empty() {
                rows.push(Line::raw(format!(
                    "Aliases: {}",
                    crate::ui::sanitize_for_terminal(&item.slash_aliases.join(", "))
                )));
            }
            Some(rows)
        })
        .flatten()
        .collect()
}

pub(crate) fn render_help(
    frame: &mut Frame<'_>,
    area: Rect,
    help: &Help,
    catalog: Option<&[CommandDescriptor]>,
    loading: bool,
    error: Option<&str>,
) {
    let block = Block::default()
        .borders(Borders::ALL)
        .title(" Commands ")
        .title_bottom(" ↑↓ PgUp/PgDn • r refresh • Esc close ");
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let mut rows = Vec::new();
    if loading {
        rows.push(Line::raw("Loading commands…"));
    }
    if let Some(error) = error {
        rows.push(Line::raw(crate::ui::sanitize_for_terminal(error)));
    }
    rows.extend(help_rows(catalog));
    if rows.is_empty() {
        rows.push(Line::raw("No commands available. Press r to retry."));
    }
    let offset = help.offset.min(rows.len().saturating_sub(1));
    frame.render_widget(
        Paragraph::new(rows.into_iter().skip(offset).collect::<Vec<_>>()),
        inner,
    );
}

pub(crate) enum SessionCommand {
    Prompt,
    ResumeCatalog,
    ResumeSession(String),
    New,
    Name(String),
    Clone,
    Tree,
    Unrevert,
    Connect,
    Invalid(&'static str),
}

pub(crate) fn session_command(prompt: &str) -> SessionCommand {
    let trimmed = prompt.trim();
    if trimmed == "/resume" {
        return SessionCommand::ResumeCatalog;
    }
    if trimmed == "/new" {
        return SessionCommand::New;
    }
    if trimmed == "/clone" {
        return SessionCommand::Clone;
    }
    if trimmed == "/tree" {
        return SessionCommand::Tree;
    }
    if trimmed == "/unrevert" {
        return SessionCommand::Unrevert;
    }
    if trimmed == "/connect" {
        return SessionCommand::Connect;
    }
    let parts = trimmed.split_whitespace().collect::<Vec<_>>();
    match parts.as_slice() {
        ["/resume", session_id] => SessionCommand::ResumeSession((*session_id).into()),
        ["/resume", ..] => SessionCommand::Invalid("Usage: /resume [session-id]"),
        ["/new", ..] => SessionCommand::Invalid("Usage: /new"),
        ["/clone", ..] => SessionCommand::Invalid("Usage: /clone"),
        ["/tree", ..] => SessionCommand::Invalid("Usage: /tree"),
        ["/unrevert", ..] => SessionCommand::Invalid("Usage: /unrevert"),
        ["/connect", ..] => SessionCommand::Invalid("Usage: /connect"),
        ["/name", "--clear"] => SessionCommand::Name(String::new()),
        ["/name", "--clear", ..] | ["/name"] => {
            SessionCommand::Invalid("Usage: /name <display name> | /name --clear")
        }
        ["/name", ..] => SessionCommand::Name(
            trimmed
                .strip_prefix("/name")
                .expect("matched /name command")
                .trim()
                .into(),
        ),
        _ => SessionCommand::Prompt,
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

    pub(crate) fn catalog() -> Vec<CommandDescriptor> {
        [
            "help", "plan", "build", "model", "provider", "connect", "resume", "new", "name",
            "clone", "tree", "unrevert", "compact", "quit",
        ]
        .into_iter()
        .enumerate()
        .map(|(order, name)| CommandDescriptor {
            name: name.into(),
            description: format!("Describe {name}"),
            slash_command: format!("/{name}"),
            slash_aliases: if name == "quit" {
                vec!["/exit".into(), ":q".into()]
            } else {
                vec![]
            },
            order: order as i64,
        })
        .collect()
    }

    #[test]
    fn classification_keeps_literals_and_recognizes_commands_without_discovery() {
        for literal in [
            "hello",
            "/etc/hosts",
            "/todo remember \"this",
            "/ note",
            "/model\nexplain this",
            "/help\r",
            "/skill:review",
        ] {
            assert!(classify(literal, Some(&catalog())).is_none(), "{literal}");
        }
        for invalid in ["/missing", "/tmp", "/quit now", "/plan unexpected"] {
            assert!(
                matches!(classify(invalid, None), Some(Command::Invalid(_))),
                "{invalid}"
            );
        }
        for quit in ["/quit", "/exit", ":q", " /QUIT "] {
            assert!(matches!(classify(quit, None), Some(Command::Quit)));
        }
        assert!(
            matches!(classify("/compact instructions", Some(&catalog())), Some(Command::Invalid(message)) if message.contains("not available"))
        );
        assert!(
            matches!(classify("/name  release  候選", None), Some(Command::Session(SessionCommand::Name(name))) if name == "release  候選")
        );
        assert!(matches!(
            classify("/connect alpha", None),
            Some(Command::Session(SessionCommand::Invalid("Usage: /connect")))
        ));
        for name in [
            "help", "plan", "build", "model", "provider", "connect", "resume", "new", "name",
            "clone", "tree", "unrevert",
        ] {
            assert!(classify(&format!("/{name}"), None).is_some());
        }
    }

    #[test]
    fn completion_preserves_unicode_arguments_and_requires_rendered_selection() {
        let catalog = catalog();
        let mut editor = PromptEditor::default();
        editor.insert_paste("  /mo 候選 arguments");
        editor.handle_key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        for _ in 0..4 {
            editor.handle_key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE));
        }
        let mut completion = Completion::default();
        completion.sync(&editor);
        let view = completion.view(Some(&catalog)).unwrap();
        assert_eq!(view.items.len(), 1);
        let model = view.items[0];
        assert!(completion.replacement(model, &editor).is_none());
        completion.mark_rendered(model.name.clone());
        let (range, text) = completion.replacement(model, &editor).unwrap();
        assert!(editor.replace_command_token(range, &text).changed);
        assert_eq!(editor.text(), "  /model 候選 arguments");
        completion.sync(&editor);
        completion.dismiss();
        assert!(completion.view(Some(&catalog)).is_none());
        editor.handle_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        completion.sync(&editor);
        assert!(completion.view(Some(&catalog)).is_some());
    }

    #[test]
    fn help_and_completion_exclude_unsupported_handlers_and_use_rust_syntax() {
        let catalog = catalog();
        let rows = help_rows(Some(&catalog));
        let text = rows
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join("\n");
        assert!(text.contains("/connect\n"));
        assert!(!text.contains("/connect ["));
        assert!(!text.contains("/compact"));
        assert!(text.contains("Aliases: /exit, :q"));
        let mut editor = PromptEditor::default();
        editor.insert_paste("/");
        let mut completion = Completion::default();
        completion.sync(&editor);
        assert!(
            completion
                .view(Some(&catalog))
                .unwrap()
                .items
                .iter()
                .all(|item| item.name != "compact")
        );
        assert!(completion.view(None).is_none());
    }
}
