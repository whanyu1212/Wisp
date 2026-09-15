//! Opt-in, deterministic screen artifacts from the production draw path.

use super::*;
use crate::tool_cards::{ToolCallInput, ToolResultInput};
use ratatui::{
    backend::TestBackend,
    style::{Color, Modifier},
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{fs, path::PathBuf};

#[derive(Deserialize)]
struct Fixture {
    prompt: String,
    reply: String,
    tool_name: String,
    tool_arguments: Value,
    tool_output: String,
    draft: String,
}

fn fixture(scenario: &str) -> LiveUi {
    let fixture: Fixture = serde_json::from_str(include_str!(
        "../../../tests/fixtures/tui_visual_conversation.json"
    ))
    .unwrap();
    let mut ui = LiveUi {
        state: UiState::new("fake".into(), None, None),
        ..LiveUi::default()
    };
    ui.state.transcript.append_prompt(fixture.prompt);
    let tool = ui.state.transcript.observe_tool_call(ToolCallInput {
        call_id: "visual-tool".into(),
        name: fixture.tool_name.clone(),
        arguments: fixture.tool_arguments.clone(),
        detail_source: tool_detail::ToolDetailSource::None,
    });
    let result = ToolResultInput {
        call_id: "visual-tool".into(),
        name: fixture.tool_name,
        output_source_bytes: fixture.tool_output.len() as u64,
        output_source_lines: 2,
        output: fixture.tool_output,
        output_tail: None,
        output_projection_cut_mid_line: false,
        is_error: false,
        failure_code: None,
        retryable: false,
        recovery_hint: None,
        exit_code: Some(0),
        output_has_exit_status: false,
        before_text: None,
        created: false,
        summary: None,
        truncated: false,
        process_id: None,
        process_state: None,
        process_error: None,
        stdout: None,
        stdout_source_bytes: 0,
        stderr: None,
        stderr_source_bytes: 0,
        stdout_truncated: false,
        stderr_truncated: false,
        stdout_dropped_bytes: 0,
        stderr_dropped_bytes: 0,
    };
    ui.state.transcript.observe_tool_result(result.clone());
    ui.state.transcript.complete_message(1, fixture.reply);
    ui.editor.insert_paste(&fixture.draft);
    if matches!(scenario, "new-turn" | "scrollback") {
        ui.state
            .transcript
            .append_prompt("Now check the remaining edge cases.".into());
        ui.editor = PromptEditor::default();
        ui.state.view_status = ViewStatus::Running;
        ui.state.interaction_status = InteractionStatus::Running;
        ui.state.current_command = Some(reducer::ActiveCommand {
            id: "visual-next-prompt".into(),
            command_type: reducer::ActiveCommandType::Prompt,
        });
        if scenario == "scrollback" {
            ui.transcript_viewport.reduce(
                TranscriptViewAction::Home,
                &ui.state.transcript,
                &mut ui.transcript_row_cache,
            );
        }
    }
    if scenario == "tools" {
        ui.transcript_row_cache.fold_mut().expand(tool);
        ui.browse_selected = Some(tool);
        ui.transcript_viewport.reduce(
            TranscriptViewAction::Home,
            &ui.state.transcript,
            &mut ui.transcript_row_cache,
        );
    }
    if matches!(scenario, "working" | "tool-running" | "tool-dim") {
        ui.state.view_status = ViewStatus::Running;
        ui.state.interaction_status = InteractionStatus::Running;
        ui.state.current_command = Some(reducer::ActiveCommand {
            id: "visual-prompt".into(),
            command_type: reducer::ActiveCommandType::Prompt,
        });
        ui.state
            .transcript
            .append_message_delta(2, "Checking the remaining edge cases…");
    }
    if matches!(scenario, "tool-running" | "tool-dim") {
        ui.state.transcript = Default::default();
        ui.state
            .transcript
            .append_prompt("Check the build, lint, and tests.".into());
        ui.state.transcript.complete_message(
            1,
            "I’m checking the project. The completed checks stay visible while the tests run."
                .into(),
        );
        for (id, command, is_error) in [
            ("build", "cargo build", false),
            ("lint", "cargo clippy", true),
        ] {
            ui.state.transcript.observe_tool_call(ToolCallInput {
                call_id: id.into(),
                name: "bash".into(),
                arguments: json!({"command": command}),
                detail_source: tool_detail::ToolDetailSource::None,
            });
            let mut finished = result.clone();
            finished.call_id = id.into();
            finished.is_error = is_error;
            finished.exit_code = Some(if is_error { 1 } else { 0 });
            ui.state.transcript.observe_tool_result(finished);
        }
        ui.state.transcript.observe_tool_call(ToolCallInput {
            call_id: "tests".into(),
            name: "bash".into(),
            arguments: json!({"command": "cargo test --workspace"}),
            detail_source: tool_detail::ToolDetailSource::None,
        });
        ui.state
            .transcript
            .observe_approval_resolved("tests", true, None);
        ui.editor = PromptEditor::default();
        ui.activity_frame = if scenario == "tool-dim" { 4 } else { 0 };
    }
    if scenario == "approval" {
        ui.state.view_status = ViewStatus::WaitingForApproval;
        ui.state.pending_approval = Some(PendingApproval {
            call_id: "visual-approval".into(),
            name: "bash".into(),
            arguments: fixture.tool_arguments,
            detail_source: tool_detail::ToolDetailSource::None,
            safety: "command".into(),
        });
    }
    if scenario == "permissions" {
        ui.state.permissions.snapshot = Some(Arc::new(wisp_protocol::events::PermissionState {
            mode: wisp_protocol::commands::PermissionMode::Ask,
            saved_mode: Some(wisp_protocol::commands::PermissionMode::Ask),
            project_path: Some("/projects/wisp".into()),
        }));
        ui.discovery_view = Some(DiscoveryView::permissions());
    }
    ui
}

fn rgb(color: Color) -> String {
    match color {
        Color::Rgb(r, g, b) => format!("#{r:02x}{g:02x}{b:02x}"),
        other => panic!("capture requires a resolved RGB palette: {other:?}"),
    }
}

fn draw(ui: &mut LiveUi, width: u16, height: u16) -> ratatui::buffer::Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "visual".into(),
            protocol_version: wisp_protocol::LIVE_RPC_PROTOCOL_VERSION,
            event_schema_version: wisp_protocol::EVENT_SCHEMA_VERSION,
        },
    )
    .unwrap();
    terminal.backend().buffer().clone()
}

fn row_containing(buffer: &ratatui::buffer::Buffer, text: &str) -> u16 {
    (0..buffer.area.height)
        .find(|&y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
                .contains(text)
        })
        .unwrap_or_else(|| panic!("missing {text:?} in {buffer:?}"))
}

#[test]
fn user_and_code_surfaces_fill_only_their_allocated_rows() {
    for selected in theme::themes() {
        for no_color in [false, true] {
            let mut ui = fixture("conversation");
            ui.theme.active = selected;
            ui.no_color = no_color;
            let palette = ui.palette();
            let canvas = palette.base().bg.unwrap_or(Color::Reset);
            let buffer = draw(&mut ui, 100, 30);
            let user = row_containing(&buffer, "Review the startup");
            let label = row_containing(&buffer, "you");
            assert_eq!(buffer[(3, label - 1)].symbol(), " ");
            assert_eq!(buffer[(2, label - 1)].bg, palette.panel);
            assert_eq!(buffer[(97, label - 1)].bg, palette.panel);
            assert_eq!(buffer[(3, label - 2)].bg, canvas);
            assert_eq!(buffer[(2, user)].bg, palette.panel);
            assert_eq!(buffer[(97, user)].bg, palette.panel);
            assert_eq!(buffer[(1, user)].bg, canvas);
            assert_eq!(buffer[(98, user)].bg, canvas);
            assert_eq!(buffer[(3, user)].fg, palette.foreground);
            assert!(!buffer[(3, user)].modifier.contains(Modifier::BOLD));
            assert_eq!(buffer[(3, user + 1)].symbol(), " ");
            assert_eq!(buffer[(2, user + 1)].bg, palette.panel);
            assert_eq!(buffer[(97, user + 1)].bg, palette.panel);
            assert_eq!(buffer[(3, user + 2)].bg, canvas);
            let code = row_containing(&buffer, "let ready");
            for y in code..=code + 2 {
                for x in 3..97 {
                    assert_eq!(
                        buffer[(x, y)].bg,
                        palette.surface,
                        "{} at {x},{y}",
                        selected.slug
                    );
                }
                assert_eq!(buffer[(2, y)].bg, canvas);
            }
            let table = row_containing(&buffer, "┌");
            assert_eq!(buffer[(3, table)].fg, palette.muted);
            assert!(theme::contrast_ratio(palette.foreground, palette.panel) >= 4.5);
            assert!(theme::contrast_ratio(palette.primary, palette.panel) >= 4.5);
        }
    }
}

#[test]
fn active_user_surface_keeps_one_highlighted_row_above_and_below() {
    let mut ui = fixture("new-turn");
    let palette = ui.palette();
    let buffer = draw(&mut ui, 80, 24);
    let label = row_containing(&buffer, "you");
    let prompt = row_containing(&buffer, "Now check the remaining edge cases.");

    assert_eq!(buffer[(2, label - 1)].bg, palette.panel);
    assert_eq!(buffer[(77, label - 1)].bg, palette.panel);
    assert_eq!(buffer[(2, prompt + 1)].bg, palette.panel);
    assert_eq!(buffer[(77, prompt + 1)].bg, palette.panel);
    assert_eq!(buffer[(1, prompt + 1)].bg, palette.background);
    assert_eq!(buffer[(78, prompt + 1)].bg, palette.background);
}

#[test]
fn composer_geometry_tracks_padding_and_keeps_the_cursor_in_the_editor() {
    for (width, height) in [(100, 30), (80, 24), (40, 16), (30, 8)] {
        let mut ui = fixture("conversation");
        ui.mouse_enabled = true;
        ui.editor.restore_prompt("ab界\nsecond");
        let buffer = draw(&mut ui, width, height);
        let editor = ui
            .mouse_frame
            .as_ref()
            .unwrap()
            .conversation
            .editor
            .as_ref()
            .unwrap();
        let margin = if width >= 60 { 2 } else { 1 };
        assert_eq!(editor.area.x, margin + 3);
        assert_eq!(editor.area.right(), width - margin - 1);
        assert_eq!(
            buffer[(margin, editor.area.y)].symbol(),
            if height >= 16 { "│" } else { " " }
        );
        assert_eq!(buffer[(margin + 1, editor.area.y)].symbol(), "›");
        assert_eq!(buffer[(editor.area.x, editor.area.y)].symbol(), "a");
        let bottom_padding = u16::from(height >= 16);
        assert_eq!(editor.area.bottom(), height - 1 - bottom_padding);
        if bottom_padding > 0 {
            assert_eq!(buffer[(margin, editor.area.y - 1)].symbol(), "╭");
            assert_eq!(buffer[(editor.area.x, editor.area.y - 1)].symbol(), "─");
            assert_eq!(
                buffer[(editor.area.x, editor.area.y - 1)].bg,
                ui.palette().background
            );
        }
    }
}

fn screen_text(buffer: &ratatui::buffer::Buffer) -> String {
    buffer.content.iter().map(|cell| cell.symbol()).collect()
}

#[test]
fn composer_soft_wraps_long_logical_lines_without_changing_the_prompt() {
    let mut ui = fixture("conversation");
    ui.mouse_enabled = true;
    let prompt = format!("{}TAIL", "x".repeat(100));
    ui.editor.restore_prompt(&prompt);

    let buffer = draw(&mut ui, 80, 24);
    let editor = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap();
    assert!(editor.area.height >= 2);
    assert!(editor.rows.len() >= 2);
    assert_eq!(editor.rows[0].logical_row, 0);
    assert_eq!(editor.rows[1].logical_row, 0);
    assert!(editor.rows[1].column_start > 0);
    assert!(row_containing(&buffer, "TAIL") > editor.area.y);
    assert_eq!(ui.editor.text(), prompt);
    assert_eq!(ui.editor.line_count(), 1);
}

#[test]
fn composer_wraps_prose_at_word_boundaries_without_changing_the_prompt() {
    let mut ui = fixture("conversation");
    ui.mouse_enabled = true;
    let prompt = "alpha beta gamma delta tail";
    ui.editor.restore_prompt(prompt);

    let buffer = draw(&mut ui, 30, 16);
    let editor = ui
        .mouse_frame
        .as_ref()
        .unwrap()
        .conversation
        .editor
        .as_ref()
        .unwrap();
    let rendered = (editor.area.y..editor.area.bottom())
        .map(|y| {
            (editor.area.x..editor.area.right())
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>();

    assert_eq!(ui.editor.text(), prompt);
    assert_eq!(ui.editor.line_count(), 1);
    assert!(
        rendered
            .iter()
            .any(|row| row.trim_end() == "alpha beta gamma delta")
    );
    assert!(rendered.iter().any(|row| row.trim_end() == "tail"));
}

#[test]
fn streamed_reply_stops_activity_without_changing_transcript_padding() {
    for (width, height) in [(100, 30), (80, 24), (40, 16), (30, 8)] {
        let mut ui = fixture("working");
        ui.mouse_enabled = true;
        let buffer = draw(&mut ui, width, height);
        let reply = row_containing(&buffer, "wisp");
        let editor = ui
            .mouse_frame
            .as_ref()
            .unwrap()
            .conversation
            .editor
            .as_ref()
            .unwrap();
        let composer_top = editor.area.y - u16::from(height >= 16);
        assert!(reply < composer_top);
        assert!(!(0..width).any(|x| buffer[(x, height - 1)].symbol() == "◐"));
        if height >= 16 {
            for x in 0..width {
                assert_eq!(buffer[(x, 0)].symbol(), " ");
                assert_eq!(buffer[(x, composer_top - 1)].symbol(), " ");
                assert_eq!(buffer[(x, composer_top - 1)].bg, ui.palette().background);
            }
        }
        ui.activity_frame = 1;
        let next = draw(&mut ui, width, height);
        assert!(screen_text(&next).contains("wisp"));
        assert!(!screen_text(&next).contains("working ◓"));
        assert!(!screen_text(&next).contains('◓'));
        ui.state.view_status = ViewStatus::Idle;
        ui.state.interaction_status = InteractionStatus::Idle;
        ui.state.current_command = None;
        assert!(!screen_text(&draw(&mut ui, width, height)).contains("wisp ◓"));
    }
}

#[test]
fn new_turn_hides_previous_reply_until_scrolled_back_and_moves_scrollbar() {
    let mut ui = fixture("new-turn");
    let live = draw(&mut ui, 80, 24);
    let text = screen_text(&live);
    assert!(text.contains("Now check the remaining edge cases."));
    assert!(!text.contains("Startup review"));
    assert!(!text.contains("Configuration loads"));
    assert!(text.contains("working ◐"));
    let thumb = |buffer: &ratatui::buffer::Buffer| {
        (0..buffer.area.height)
            .filter(|&y| buffer[(79, y)].symbol() == "┃")
            .collect::<Vec<_>>()
    };
    let live_thumb = thumb(&live);
    assert!(!live_thumb.is_empty());
    ui.transcript_viewport.reduce(
        TranscriptViewAction::Home,
        &ui.state.transcript,
        &mut ui.transcript_row_cache,
    );
    let history = draw(&mut ui, 80, 24);
    assert!(screen_text(&history).contains("Startup review"));
    assert!(thumb(&history)[0] < live_thumb[0]);
    ui.transcript_viewport.reduce(
        TranscriptViewAction::FollowTail,
        &ui.state.transcript,
        &mut ui.transcript_row_cache,
    );
    ui.state
        .transcript
        .append_message_delta(2, "The next review has started.");
    let streaming = screen_text(&draw(&mut ui, 80, 24));
    assert!(streaming.contains("The next review has started."));
    assert!(streaming.contains("wisp"));
    assert!(!streaming.contains("working ◐"));
    assert!(!streaming.contains('◐'));
    assert!(!streaming.contains("Startup review"));
    ui.state
        .transcript
        .complete_message(2, "The edge cases are covered.".into());
    let completed = screen_text(&draw(&mut ui, 80, 24));
    assert!(completed.contains("The edge cases are covered."));
    assert!(!completed.contains("Startup review"));
    assert_eq!(ui.state.transcript.entries().len(), 5);
}

#[test]
fn minimum_terminal_keeps_the_submitted_prompt_before_the_first_token() {
    let mut ui = fixture("new-turn");
    let text = screen_text(&draw(&mut ui, 30, 8));
    let compact: String = text
        .chars()
        .filter(|c| !c.is_whitespace() && !matches!(c, '│' | '┃'))
        .collect();
    assert!(compact.contains("Nowchecktheremainingedgecases."), "{text}");
    assert!(text.contains("working ◐"), "{text}");
    assert!(!text.contains("Startup review"));
}

#[test]
#[ignore = "writes review artifacts only when explicitly requested"]
fn capture_conversation_screens() {
    let output =
        PathBuf::from(std::env::var_os("WISP_VISUAL_OUTPUT").expect("set WISP_VISUAL_OUTPUT"));
    fs::create_dir_all(&output).unwrap();
    for (width, height) in [(100, 30), (80, 24), (40, 16), (30, 8)] {
        for (variant, selected, no_color) in [
            ("dark", theme::default_theme(), false),
            ("light", theme::paper_theme(), false),
            ("mono", theme::default_theme(), true),
        ] {
            for scenario in [
                "conversation",
                "tools",
                "working",
                "tool-running",
                "tool-dim",
                "approval",
                "permissions",
                "new-turn",
                "scrollback",
            ] {
                let mut ui = fixture(scenario);
                ui.theme.active = selected;
                ui.no_color = no_color;
                let frames = if scenario == "tool-running"
                    && variant == "dark"
                    && width == 100
                    && height == 30
                {
                    8
                } else {
                    1
                };
                for tick in 0..frames {
                    if frames > 1 {
                        ui.activity_frame = tick;
                    }
                    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
                    ui.draw(
                        &mut terminal,
                        &ConnectionInfo {
                            backend_version: "visual".into(),
                            protocol_version: wisp_protocol::LIVE_RPC_PROTOCOL_VERSION,
                            event_schema_version: wisp_protocol::EVENT_SCHEMA_VERSION,
                        },
                    )
                    .unwrap();
                    let cells = terminal.backend().buffer().content.iter().map(|cell| json!({
                    "text": cell.symbol(), "fg": rgb(cell.fg), "bg": rgb(cell.bg),
                    "bold": cell.modifier.contains(Modifier::BOLD), "dim": cell.modifier.contains(Modifier::DIM), "italic": cell.modifier.contains(Modifier::ITALIC),
                    "underline": cell.modifier.contains(Modifier::UNDERLINED), "reverse": cell.modifier.contains(Modifier::REVERSED),
                    "strike": cell.modifier.contains(Modifier::CROSSED_OUT)
                })).collect::<Vec<_>>();
                    let suffix = if tick == 0 {
                        String::new()
                    } else {
                        format!("-frame-{tick}")
                    };
                    let path = output.join(format!(
                        "rust-{scenario}-{variant}-{width}x{height}{suffix}.json"
                    ));
                    fs::write(
                        path,
                        serde_json::to_vec(
                            &json!({"width": width, "height": height, "cells": cells}),
                        )
                        .unwrap(),
                    )
                    .unwrap();
                }
            }
        }
    }
}
