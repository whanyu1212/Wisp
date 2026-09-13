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
    ui.state.transcript.observe_tool_result(ToolResultInput {
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
    });
    ui.state.transcript.complete_message(1, fixture.reply);
    ui.editor.insert_paste(&fixture.draft);
    if scenario == "tools" {
        ui.transcript_row_cache.fold_mut().expand(tool);
        ui.browse_selected = Some(tool);
        ui.transcript_viewport.reduce(
            TranscriptViewAction::Home,
            &ui.state.transcript,
            &mut ui.transcript_row_cache,
        );
    }
    if scenario == "working" {
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
            protocol_version: 6,
            event_schema_version: 37,
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
            let buffer = draw(&mut ui, 100, 30);
            let user = row_containing(&buffer, "Review the startup");
            assert_eq!(buffer[(2, user)].bg, palette.panel);
            assert_eq!(buffer[(97, user)].bg, palette.panel);
            assert_eq!(buffer[(1, user)].bg, palette.background);
            assert_eq!(buffer[(98, user)].bg, palette.background);
            assert_eq!(buffer[(3, user)].fg, palette.foreground);
            assert!(!buffer[(3, user)].modifier.contains(Modifier::BOLD));
            assert_eq!(buffer[(3, user + 1)].bg, palette.background);
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
                assert_eq!(buffer[(2, y)].bg, palette.background);
            }
            let table = row_containing(&buffer, "┌");
            assert_eq!(buffer[(3, table)].fg, palette.muted);
            assert!(theme::contrast_ratio(palette.foreground, palette.panel) >= 4.5);
            assert!(theme::contrast_ratio(palette.primary, palette.panel) >= 4.5);
        }
    }
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
        assert_eq!(buffer[(margin, editor.area.y)].symbol(), "┃");
        assert_eq!(buffer[(margin + 1, editor.area.y)].symbol(), ">");
        assert_eq!(buffer[(editor.area.x, editor.area.y)].symbol(), "a");
        let bottom_padding = u16::from(height >= 16);
        assert_eq!(editor.area.bottom(), height - 1 - bottom_padding);
        if bottom_padding > 0 {
            assert_eq!(buffer[(editor.area.x, editor.area.y - 1)].symbol(), " ");
            assert_eq!(
                buffer[(editor.area.x, editor.area.y - 1)].bg,
                ui.palette().surface
            );
        }
    }
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
            for scenario in ["conversation", "tools", "working", "approval"] {
                let mut ui = fixture(scenario);
                ui.theme.active = selected;
                ui.no_color = no_color;
                let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
                ui.draw(
                    &mut terminal,
                    &ConnectionInfo {
                        backend_version: "visual".into(),
                        protocol_version: 6,
                        event_schema_version: 37,
                    },
                )
                .unwrap();
                let cells = terminal.backend().buffer().content.iter().map(|cell| json!({
                    "text": cell.symbol(), "fg": rgb(cell.fg), "bg": rgb(cell.bg),
                    "bold": cell.modifier.contains(Modifier::BOLD), "italic": cell.modifier.contains(Modifier::ITALIC),
                    "underline": cell.modifier.contains(Modifier::UNDERLINED), "reverse": cell.modifier.contains(Modifier::REVERSED),
                    "strike": cell.modifier.contains(Modifier::CROSSED_OUT)
                })).collect::<Vec<_>>();
                let path = output.join(format!("rust-{scenario}-{variant}-{width}x{height}.json"));
                fs::write(
                    path,
                    serde_json::to_vec(&json!({"width": width, "height": height, "cells": cells}))
                        .unwrap(),
                )
                .unwrap();
            }
        }
    }
}
