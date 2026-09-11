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
use std::borrow::Cow;
use std::ops::Range;
use wisp_protocol::{
    commands::AgentMode,
    events::{CommandDescriptor, SkillCatalogEntry, SkillCatalogSnapshot},
};

pub(crate) enum Command {
    Help,
    History,
    Mode(AgentMode),
    Context,
    Skills,
    Mcp,
    AutoCompaction(bool),
    Compact(Option<String>),
    Quit,
    Model(ModelCommand),
    Session(SessionCommand),
    Invalid(String),
}

/// Actual Rust syntax; descriptions and display order come from discovery.
fn usage(name: &str) -> Option<&'static str> {
    Some(match name {
        "help" => "/help",
        "history" => "/history",
        "plan" => "/plan",
        "build" => "/build",
        "context" => "/context [auto on|off]",
        "skills" => "/skills",
        "mcp" => "/mcp",
        "compact" => "/compact [instructions]",
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
        "help" | "history" | "plan" | "build" | "quit" | "skills" | "mcp"
            if !tail.trim().is_empty() =>
        {
            Command::Invalid(format!("Usage: {syntax}"))
        }
        "help" => Command::Help,
        "history" => Command::History,
        "skills" => Command::Skills,
        "mcp" => Command::Mcp,
        "plan" => Command::Mode(AgentMode::Plan),
        "build" => Command::Mode(AgentMode::Build),
        "context" => match tail.split_whitespace().collect::<Vec<_>>().as_slice() {
            [] => Command::Context,
            ["auto", "on"] => Command::AutoCompaction(true),
            ["auto", "off"] => Command::AutoCompaction(false),
            _ => Command::Invalid(format!("Usage: {syntax}")),
        },
        "compact" => Command::Compact((!tail.trim().is_empty()).then(|| tail.trim().to_owned())),
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
    pub items: Vec<CompletionItem<'a>>,
    pub selected: usize,
}

#[derive(Clone, Copy)]
pub(crate) enum CompletionItem<'a> {
    Command(&'a CommandDescriptor),
    Skill(&'a SkillCatalogEntry),
}

impl<'a> CompletionItem<'a> {
    pub fn spelling(self) -> Cow<'a, str> {
        match self {
            Self::Command(command) => Cow::Borrowed(&command.slash_command),
            Self::Skill(skill) => Cow::Owned(format!("/skill:{}", skill.name)),
        }
    }

    fn description(self) -> String {
        match self {
            Self::Command(command) => crate::ui::sanitize_for_terminal(&command.description),
            Self::Skill(skill) => format!(
                "[{}] {}",
                skill.source.as_str(),
                crate::ui::sanitize_for_terminal(&skill.description)
            ),
        }
    }

    fn takes_arguments(self) -> bool {
        match self {
            Self::Command(command) => usage(&command.name).is_some_and(|usage| usage.contains(' ')),
            Self::Skill(_) => true,
        }
    }
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

    pub fn view<'a>(
        &self,
        catalog: Option<&'a [CommandDescriptor]>,
        skills: Option<&'a SkillCatalogSnapshot>,
    ) -> Option<CompletionView<'a>> {
        if self.dismissed {
            return None;
        }
        let (range, prefix) = self.context.as_ref()?;
        let prefix = prefix.to_ascii_lowercase();
        let items: Vec<_> = catalog
            .unwrap_or_default()
            .iter()
            .filter(|item| {
                usage(&item.name).is_some()
                    && item.slash_command.to_ascii_lowercase().starts_with(&prefix)
            })
            .map(CompletionItem::Command)
            .chain(
                // Python recognizes skill directives only at byte zero.
                skills
                    .filter(|_| range.start == 0)
                    .into_iter()
                    .flat_map(|catalog| &catalog.entries)
                    .filter(|skill| {
                        skill.is_invocable()
                            && format!("/skill:{}", skill.name).starts_with(&prefix)
                    })
                    .map(CompletionItem::Skill),
            )
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
        item: CompletionItem<'_>,
        editor: &PromptEditor,
    ) -> Option<(Range<usize>, String)> {
        let spelling = item.spelling();
        if self.rendered.as_deref() != Some(spelling.as_ref()) {
            return None;
        }
        let (range, _) = self.context.as_ref()?;
        let mut replacement = spelling.into_owned();
        if editor.text()[range.end..].is_empty() && item.takes_arguments() {
            replacement.push(' ');
        }
        Some((range.clone(), replacement))
    }

    pub fn is_exact(&self, item: CompletionItem<'_>) -> bool {
        self.context.as_ref().is_some_and(|(_, token)| match item {
            CompletionItem::Command(_) => token.eq_ignore_ascii_case(&item.spelling()),
            CompletionItem::Skill(_) => token == item.spelling().as_ref(),
        })
    }
}

pub(crate) fn render_completion(frame: &mut Frame<'_>, area: Rect, view: &CompletionView<'_>) {
    let items = view.items.iter().map(|item| {
        ListItem::new(format!(
            "{}  {}",
            crate::ui::sanitize_for_terminal(&item.spelling()),
            item.description()
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
        .chain([
            Line::styled("@ project files", Style::default().fg(Color::Cyan)),
            Line::raw("Type @ to find paths; ↑↓ select, Enter insert (not submit)."),
            Line::raw("Tab fuzzy/tree; ←/→ folders; Esc close; Tab at a dismissed reference refreshes."),
            Line::raw("Only paths are inserted. Limited snapshots may omit files; discovery stays in Python."),
        ])
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
            "clone", "tree", "unrevert", "context", "compact", "skills", "mcp", "history",
            "update", "quit",
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

    pub(crate) fn skills() -> SkillCatalogSnapshot {
        serde_json::from_value(
            serde_json::from_str::<serde_json::Value>(include_str!(
                "../../../tests/fixtures/rust_tui_discovery.json"
            ))
            .unwrap()["rpc.skills"]["catalog"]
                .clone(),
        )
        .unwrap()
    }

    #[test]
    fn combined_menu_keeps_commands_before_skills_and_preserves_canonical_identity() {
        let catalog = catalog();
        let mut skills = skills();
        skills.entries[0].name = "unsafe\n/quit".into();
        let mut editor = PromptEditor::default();
        editor.insert_paste("/");
        let mut completion = Completion::default();
        completion.sync(&editor);
        let view = completion.view(Some(&catalog), Some(&skills)).unwrap();
        let first_skill = view
            .items
            .iter()
            .position(|item| matches!(item, CompletionItem::Skill(_)))
            .unwrap();
        assert!(
            view.items[..first_skill]
                .iter()
                .all(|item| matches!(item, CompletionItem::Command(_)))
        );
        assert!(
            !view
                .items
                .iter()
                .any(|item| item.spelling().contains("unsafe"))
        );
        assert!(view.items.iter().any(|item| item.spelling() == "/build"));
        assert!(
            view.items
                .iter()
                .any(|item| item.spelling() == "/skill:build")
        );
        editor.clear();
        editor.insert_paste("/SKILL:BU 候選  arguments");
        editor.handle_key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
        for _ in 0..9 {
            editor.handle_key(KeyEvent::new(KeyCode::Right, KeyModifiers::NONE));
        }
        completion.sync(&editor);
        let view = completion.view(Some(&catalog), Some(&skills)).unwrap();
        assert_eq!(view.items.len(), 1);
        let item = view.items[0];
        assert_eq!(item.spelling(), "/skill:build");
        assert!(completion.replacement(item, &editor).is_none());
        completion.mark_rendered(item.spelling().into_owned());
        let (range, replacement) = completion.replacement(item, &editor).unwrap();
        editor.replace_range(range, &replacement);
        assert_eq!(editor.text(), "/skill:build 候選  arguments");
        assert!(classify(editor.text(), Some(&catalog)).is_none());
        completion.invalidate();
        assert!(completion.replacement(item, &editor).is_none());
    }

    #[test]
    fn skill_completion_requires_a_directive_at_byte_zero_and_adds_a_space() {
        let skills = skills();
        let mut completion = Completion::default();
        let mut editor = PromptEditor::default();
        for literal in ["  /skill:re", "\t/skill:re", "/skill:re\nrequest"] {
            editor.clear();
            editor.insert_paste(literal);
            completion.sync(&editor);
            assert!(completion.view(None, Some(&skills)).is_none(), "{literal}");
        }
        editor.clear();
        editor.insert_paste("/skill:re");
        completion.sync(&editor);
        let item = completion.view(None, Some(&skills)).unwrap().items[0];
        completion.mark_rendered(item.spelling().into_owned());
        let (range, replacement) = completion.replacement(item, &editor).unwrap();
        editor.replace_range(range, &replacement);
        assert_eq!(editor.text(), "/skill:review ");
        assert!(classify("/skill:unknown request", None).is_none());
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
            matches!(classify("/compact instructions", Some(&catalog())), Some(Command::Compact(Some(instructions))) if instructions == "instructions")
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
        let view = completion.view(Some(&catalog), None).unwrap();
        assert_eq!(view.items.len(), 1);
        let model = view.items[0];
        assert!(completion.replacement(model, &editor).is_none());
        completion.mark_rendered(model.spelling().into_owned());
        let (range, text) = completion.replacement(model, &editor).unwrap();
        assert!(editor.replace_range(range, &text).changed);
        assert_eq!(editor.text(), "  /model 候選 arguments");
        completion.sync(&editor);
        completion.dismiss();
        assert!(completion.view(Some(&catalog), None).is_none());
        editor.handle_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        completion.sync(&editor);
        assert!(completion.view(Some(&catalog), None).is_some());
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
        assert!(text.contains("/compact [instructions]"));
        assert!(text.contains("/context [auto on|off]"));
        assert!(text.contains("/skills"));
        assert!(text.contains("/mcp"));
        assert!(text.contains("/history"));
        assert!(!text.contains("/update"));
        assert!(text.contains("Aliases: /exit, :q"));
        let mut editor = PromptEditor::default();
        editor.insert_paste("/");
        let mut completion = Completion::default();
        completion.sync(&editor);
        assert!(
            completion
                .view(Some(&catalog), None)
                .unwrap()
                .items
                .iter()
                .all(|item| item.spelling() != "/update")
        );
        assert!(completion.view(None, None).is_none());
    }
}
