//! Opt-in, deterministic screen artifacts from the production draw path.

use super::*;
use crate::tool_cards::{ToolCallInput, ToolResultInput};
use crossterm::event::{MouseButton, MouseEventKind};
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

/// The totals closing the typography scenario's turn.
fn typography_totals() -> crate::transcript::RunTotals {
    crate::transcript::RunTotals {
        mode: Some("plan".into()),
        model: Some("demo".into()),
        ..crate::transcript::RunTotals::finished(2_000, 0)
    }
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
        nested: Default::default(),
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
        duration_ms: None,
        started_at_ms: None,
        process_started_at_ms: None,
        process_ended_at_ms: None,
        edit_locations: Vec::new(),
        images: Vec::new(),
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
        ui.browse_selected = Some(BrowseTarget::Entry(tool));
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
            grants_allowed: true,
            session_only_yolo: false,
        });
    }
    if scenario == "mcp" {
        use wisp_protocol::events::{McpServerSnapshot, McpServerStatus, McpStatusSnapshot};
        ui.state.mcp.snapshot = Some(Arc::new(McpStatusSnapshot {
            servers: vec![McpServerSnapshot {
                name: "demo-docs".into(),
                status: McpServerStatus::Connected,
                tool_names: vec!["search_docs".into(), "read_page".into()],
                error: None,
                launch: None,
                sign_in: None,
            }],
        }));
        ui.discovery_view = Some(DiscoveryView::mcp());
    }
    if scenario == "permissions" {
        ui.state.permissions.snapshot = Some(Arc::new(wisp_protocol::events::PermissionState {
            mode: wisp_protocol::commands::PermissionMode::Ask,
            saved_mode: Some(wisp_protocol::commands::PermissionMode::Ask),
            project_path: Some("/projects/wisp".into()),
            session_only_yolo: false,
        }));
        ui.discovery_view = Some(DiscoveryView::permissions());
    }
    if matches!(scenario, "composer-plan" | "composer-yolo") {
        // The composer's border names a chosen model, effort, and mode.
        ui.editor = Default::default();
        ui.state.model = Some("claude-opus-5-5".into());
        ui.state.effort = Some("high".into());
        ui.state.mode_confirmed = true;
        if scenario == "composer-plan" {
            ui.state.mode = crate::reducer::AgentMode::Plan;
            ui.editor
                .insert_paste("Sketch the migration before touching code.");
        } else {
            ui.state.permissions.snapshot =
                Some(Arc::new(wisp_protocol::events::PermissionState {
                    mode: wisp_protocol::commands::PermissionMode::Yolo,
                    saved_mode: Some(wisp_protocol::commands::PermissionMode::Yolo),
                    project_path: Some("/projects/wisp".into()),
                    session_only_yolo: false,
                }));
            ui.editor.insert_paste(
                "Refactor the retry helper so that:\n- backoff is capped at 30s\n\
                 - jitter comes from an injected clock",
            );
        }
    }
    if matches!(scenario, "activity-rail" | "activity-rail-expanded") {
        ui.state.transcript = Default::default();
        ui.state
            .transcript
            .append_prompt("Inspect the project, update the docs, and run the checks.".into());
        for (index, (name, arguments)) in [
            ("read", json!({"path": "AGENTS.md"})),
            (
                "grep",
                json!({"pattern": "Deploy", "path": ".github/workflows"}),
            ),
            ("ls", json!({"path": ".github/workflows"})),
            ("bash", json!({"command": "git status --short"})),
            ("bash", json!({"command": "gh pr view 766"})),
            ("bash", json!({"command": "gh pr checks 766"})),
            ("bash", json!({"command": "gh pr diff 766 --stat"})),
            ("bash", json!({"command": "git log -1 --oneline"})),
            (
                "edit",
                json!({"path": "README.md", "old_string": "old", "new_string": "new"}),
            ),
            ("bash", json!({"command": "git diff --check"})),
            ("bash", json!({"command": "git diff --stat"})),
            (
                "write",
                json!({"path": "notes.md", "content": "Checked workflows."}),
            ),
            ("mcp__github__get_pull_request", json!({"number": 766})),
        ]
        .into_iter()
        .enumerate()
        {
            let call_id = format!("activity-{index}");
            ui.state.transcript.observe_tool_call(ToolCallInput {
                call_id: call_id.clone(),
                name: name.into(),
                arguments,
                detail_source: tool_detail::ToolDetailSource::None,
            });
            let mut finished = result.clone();
            finished.call_id = call_id;
            finished.name = name.into();
            ui.state.transcript.observe_tool_result(finished);
        }
        ui.state.transcript.complete_message(
            1,
            "Updated the project documentation.\n\n- Checked the deployment workflows.\n- Updated README.md and wrote notes.md.\n- Inspected PR #766.".into(),
        );
        ui.editor = PromptEditor::default();
        if scenario == "activity-rail-expanded" {
            let first_bash = ui.state.transcript.entries()[4].id;
            ui.transcript_row_cache.fold_mut().expand_group(first_bash);
            ui.transcript_viewport.reduce(
                TranscriptViewAction::Home,
                &ui.state.transcript,
                &mut ui.transcript_row_cache,
            );
        }
    }
    if matches!(scenario, "tool-activity" | "tool-activity-long") {
        ui.state.transcript = Default::default();
        // The task was launched in the previous turn; its next poll keeps the name.
        ui.state
            .transcript
            .append_prompt("Start browser tests.".into());
        ui.state.transcript.observe_tool_call(ToolCallInput {
            call_id: "browser-start".into(),
            name: "bash".into(),
            arguments: json!({"operation": "start", "command": "cd site && npm run test:browser"}),
            detail_source: tool_detail::ToolDetailSource::None,
        });
        let mut started = result.clone();
        started.call_id = "browser-start".into();
        started.name = "bash".into();
        started.process_id = Some("browser-process".into());
        started.process_state = Some("running".into());
        ui.state.transcript.observe_tool_result(started);
        ui.state
            .transcript
            .append_prompt("Check the migration issues and test results.".into());
        let calls = std::iter::once((
            "issues".to_owned(),
            "mcp__linear__list_issues",
            json!({"query": "astro", "limit": 100}),
        ))
        .chain(
            ["HY-311", "HY-312", "HY-313", "HY-315", "HY-316"]
                .into_iter()
                .map(|id| {
                    (
                        id.to_owned(),
                        "mcp__linear__get_issue",
                        json!({"id": id, "includeRelations": true}),
                    )
                }),
        )
        .chain(std::iter::once((
            "checks".to_owned(),
            "bash",
            json!({"command": "cd site && npm test && npm run check"}),
        )));
        for (id, name, arguments) in calls {
            ui.state.transcript.observe_tool_call(ToolCallInput {
                call_id: id.clone(),
                name: name.into(),
                arguments,
                detail_source: tool_detail::ToolDetailSource::None,
            });
            let mut finished = result.clone();
            finished.call_id = id;
            finished.name = name.into();
            finished.duration_ms = Some(if name == "bash" { 6_900 } else { 440 });
            finished.output = "13 tests passed\nType check completed · 0 errors".into();
            ui.state.transcript.observe_tool_result(finished);
        }
        let process = ui.state.transcript.observe_tool_call(ToolCallInput {
            call_id: "browser-poll".into(),
            name: "bash".into(),
            arguments: json!({"operation": "poll", "process_id": "browser-process"}),
            detail_source: tool_detail::ToolDetailSource::None,
        });
        let mut completed = result.clone();
        completed.call_id = "browser-poll".into();
        completed.name = "bash".into();
        completed.process_id = Some("browser-process".into());
        completed.process_state = Some("completed".into());
        completed.process_started_at_ms = Some(1_000);
        completed.process_ended_at_ms = Some(9_100);
        let earlier_count = if scenario == "tool-activity-long" {
            600
        } else {
            29
        };
        let earlier = (1..=earlier_count).fold(String::new(), |mut output, i| {
            use std::fmt::Write as _;
            writeln!(&mut output, "check {i} passed").unwrap();
            output
        });
        completed.stdout = Some(format!(
            "{earlier}✓ mobile navigation\n✓ system theme changes\n✓ keyboard focus\n✓ diagram layout\n✓ search navigation\n✓ theme persistence\n\n36 passed (8.1s)"
        ));
        ui.state.transcript.observe_tool_result(completed);
        ui.transcript_row_cache.fold_mut().expand(process);
        ui.editor = PromptEditor::default();
    }
    if scenario == "typography" {
        // A finished turn whose reply thought first, then answered with three heading
        // levels and inline code, closed by its summary row.
        ui.state.transcript = Default::default();
        ui.state
            .transcript
            .append_prompt("Why is startup slow?".into());
        ui.state
            .transcript
            .append_thinking_delta(1, "Check the config loader first.");
        ui.state.transcript.complete_message(
            1,
            "# Startup review\n\nConfig loads twice.\n\n## Fixes\n\n- Call `load()` once\n\n### Risk\n\nPlugins read early."
                .into(),
        );
        ui.state.transcript.attach_run_summary(typography_totals());
        ui.editor = PromptEditor::default();
    }
    if scenario == "diff-previews" {
        ui.state.transcript = Default::default();
        ui.state
            .transcript
            .append_prompt("ok lets work on it".into());
        let mut first_edit = None;
        for (id, name, arguments) in [
            (
                "package",
                "edit",
                json!({
                    "path": "site/package.json",
                    "edits": [{
                        "oldText": "\"preview\": \"astro preview\"\n",
                        "newText": "\"preview\": \"astro preview --host 127.0.0.1\",\n\"test:browser\": \"playwright test\"\n"
                    }]
                }),
            ),
            (
                "styles",
                "edit",
                json!({
                    "path": "site/src/styles/site.css",
                    "edits": [{"oldText": ".mermaid svg * {\n  animation: none !important;\n}\n", "newText": ""}]
                }),
            ),
            (
                "browser",
                "write",
                json!({
                    "path": "site/tests/browser/layouts.spec.ts",
                    "content": "test('mobile navigation', async ({ page }) => {\n  await page.goto('/');\n  const menu = page.getByRole('button', { name: 'Menu' });\n  await menu.click();\n  const nav = page.getByRole('navigation');\n  await expect(nav).toBeVisible();\n  await page.keyboard.press('Escape');\n});\n"
                }),
            ),
        ] {
            let entry = ui.state.transcript.observe_tool_call(ToolCallInput {
                call_id: id.into(),
                name: name.into(),
                detail_source: tool_detail::project_tool_detail_source(
                    name,
                    arguments.as_object().unwrap(),
                ),
                arguments: tool_cards::bounded_tool_arguments(name, &arguments),
            });
            first_edit.get_or_insert(entry);
            let mut finished = result.clone();
            finished.call_id = id.into();
            finished.name = name.into();
            finished.created = name == "write";
            ui.state.transcript.observe_tool_result(finished);
        }
        ui.transcript_row_cache
            .fold_mut()
            .expand_group(first_edit.unwrap());
        ui.editor = PromptEditor::default();
    }
    if scenario == "reading" {
        ui.state.transcript = Default::default();
        ui.state
            .transcript
            .append_prompt("Based on Linear issues, what’s next for our Astro migration?".into());
        ui.state.transcript.complete_message(
            1,
            r#"**Next: HY-314 — finish responsive layouts, navigation, and themes.**

Linear marks **HY-312 (planning)** and **HY-313 (Astro foundation)** Done. The remaining migration work is still Backlog.

Recommended order:

1. [HY-314](https://example.test/HY-314) — complete mobile navigation, TOC, previous/next links, accessibility, and light/dark behavior. Some layout/theme groundwork already landed under HY-313; build on that rather than restart.
2. [HY-315](https://example.test/HY-315) — Pagefind search. This is unblocked and can run in parallel, coordinating with the shared layout.
3. **Finish the five structural/content tickets:**
   - **HY-269:** User-first navigation.
   - **HY-277:** homepage, screenshots, section indexes.
   - **HY-274:** split the TUI guide.
   - **HY-276:** separate SDK guides/reference; relocate capability audit.
   - **HY-270:** publish changelog; relocate maintainer release records.
4. [HY-316](https://example.test/HY-316) — CI/Pages cutover and mdBook retirement, **after all seven blockers above**. Require built-site checks and deployed-site smoke testing.

**Don’t delay migration** for commands/settings corrections, the MCP guide, troubleshooting, or new curriculum chapters—those are explicitly non-blocking follow-ups.

My pick for the next focused task: **HY-314, coordinated with HY-269’s navigation decisions.**"#.into(),
        );
        ui.editor = PromptEditor::default();
    }
    if matches!(
        scenario,
        "showcase" | "welcome" | "welcome-adal" | "welcome-classic" | "welcome-entrance"
    ) {
        ui.state = showcase_state(scenario == "showcase");
        // Each logo family takes its own gradient: theme headings, or the Adal pink.
        ui.startup_logo = match scenario {
            "welcome-adal" => LogoChoice::AdalBraille,
            "welcome-classic" => LogoChoice::Classic,
            _ => LogoChoice::WispBraille,
        };
        ui.editor = PromptEditor::default();
        // The welcome screens show the first tip; a launch picks one at random.
        ui.welcome_tip = Some(0);
        if scenario == "welcome-entrance" {
            // Captured frame by frame, with the activity frame as the entrance's.
            ui.logo_entrance = crate::LogoEntrance::Playing { started: 0 };
        }
    }
    ui
}

/// A synthetic project with every side-panel section loaded: the documentation's
/// overview of a whole session. `worked` adds two finished turns and their changes;
/// without it the session is new and the welcome screen shows.
fn showcase_state(worked: bool) -> UiState {
    use crate::reducer::git_diff::{GitDiffFile, GitDiffSnapshot};
    use crate::transcript::{RunOutcome, RunResult};
    use wisp_protocol::events::{
        ContextAccountingMethod, ContextBudget, ContextEstimate, McpServerSnapshot,
        McpServerStatus, McpStatusSnapshot, PermissionState, SessionCostSummary, SessionStats,
        SessionTokenUsage, SkillCatalogEntry, SkillCatalogSnapshot, SkillSource,
    };

    let mut state = UiState::new("demo".into(), Some("demo-model".into()), None);
    state.workspace_cwd = Some("/projects/demo-app".into());
    state.git_branch = Some("feat/retry-limit".into());
    let budget = ContextBudget {
        estimate: ContextEstimate {
            total_tokens: if worked { 61_400 } else { 18_200 },
        },
        observed_tokens: None,
        observed_is_current: false,
        effective_tokens: None,
        accounting_method: ContextAccountingMethod::FullyEstimated,
        context_window: Some(272_000),
        reserve_tokens: 16_000,
    };
    state.context.budget = Some(budget.clone());
    state.context.stats = Some(Box::new(SessionStats {
        session_id: None,
        active_message_count: if worked { 12 } else { 0 },
        compaction_count: 0,
        usage: SessionTokenUsage {
            input_tokens: 0,
            output_tokens: 0,
            total_tokens: 0,
            cache_read_input_tokens: None,
            cache_write_input_tokens: None,
        },
        context: budget,
        compaction: None,
        cost: SessionCostSummary {
            known_usd: "0".into(),
            complete: true,
            priced_record_count: 0,
            unpriced_record_count: 0,
        },
        context_breakdown: None,
        branch: Default::default(),
        used_skill_names: if worked {
            vec!["python-testing".into()]
        } else {
            Vec::new()
        },
    }));
    state.permissions.snapshot = Some(Arc::new(PermissionState {
        mode: wisp_protocol::commands::PermissionMode::Ask,
        saved_mode: Some(wisp_protocol::commands::PermissionMode::Ask),
        project_path: Some("/projects/demo-app".into()),
        session_only_yolo: false,
    }));
    state.mcp.snapshot = Some(Arc::new(McpStatusSnapshot {
        servers: ["demo-docs", "demo-issues"]
            .into_iter()
            .map(|name| McpServerSnapshot {
                name: name.into(),
                status: McpServerStatus::Connected,
                tool_names: Vec::new(),
                error: None,
                launch: None,
                sign_in: None,
            })
            .collect(),
    }));
    state.skills.snapshot = Some(Arc::new(SkillCatalogSnapshot {
        entries: ["python-testing", "release-notes"]
            .into_iter()
            .map(|name| SkillCatalogEntry {
                name: name.into(),
                description: format!("Instructions for {name}"),
                source: SkillSource::PackageWisp,
            })
            .collect(),
        diagnostics: Vec::new(),
        project_trusted: true,
    }));
    if !worked {
        state.git_diff.snapshot = Some(Arc::new(GitDiffSnapshot {
            generation: 1,
            head: "demo".into(),
            files: Vec::new(),
            truncated: false,
        }));
        return state;
    }

    let explain = state
        .transcript
        .append_prompt("How does the fetch client handle timeouts?".into());
    state.transcript.complete_message(
        1,
        "Each request gets a 10-second timeout, and failures are retried with no upper limit."
            .into(),
    );
    state.transcript.attach_run_result(
        explain,
        RunResult {
            duration_ms: Some(18_000),
            outcome: RunOutcome::Completed,
        },
    );
    let prompt = state
        .transcript
        .append_prompt("Cap the retries at three and run the client tests.".into());
    let edit = json!({
        "path": "src/demo_app/client.py",
        "edits": [{
            "oldText": "    while True:\n        try:\n",
            "newText": "    for attempt in range(MAX_RETRIES):\n        try:\n"
        }]
    });
    for (id, name, arguments, duration_ms) in [
        (
            "docs",
            "mcp__demo-docs__search_docs",
            json!({"query": "retry policy"}),
            400,
        ),
        (
            "read",
            "read",
            json!({"path": "src/demo_app/client.py"}),
            20,
        ),
        ("edit", "edit", edit, 30),
        (
            "tests",
            "bash",
            json!({"command": "uv run pytest tests/test_client.py -q"}),
            3_200,
        ),
    ] {
        state.transcript.observe_tool_call(ToolCallInput {
            call_id: id.into(),
            name: name.into(),
            detail_source: tool_detail::project_tool_detail_source(
                name,
                arguments.as_object().unwrap(),
            ),
            arguments: tool_cards::bounded_tool_arguments(name, &arguments),
        });
        state.transcript.observe_tool_result(ToolResultInput {
            nested: Default::default(),
            call_id: id.into(),
            name: name.into(),
            output: if name == "bash" {
                "14 passed in 1.84s".into()
            } else {
                "ok".into()
            },
            output_source_bytes: 2,
            output_source_lines: 1,
            output_tail: None,
            output_projection_cut_mid_line: false,
            is_error: false,
            failure_code: None,
            retryable: false,
            recovery_hint: None,
            exit_code: (name == "bash").then_some(0),
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
            duration_ms: Some(duration_ms),
            started_at_ms: None,
            process_started_at_ms: None,
            process_ended_at_ms: None,
            edit_locations: Vec::new(),
            images: Vec::new(),
        });
    }
    state.transcript.complete_message(
        2,
        "Done. Requests now stop after `MAX_RETRIES = 3` attempts and raise the last error.\n\n\
         - **Changed:** `src/demo_app/client.py` and a new test for the limit.\n\
         - **Checked:** `uv run pytest tests/test_client.py` · 14 passed."
            .into(),
    );
    state.transcript.attach_run_result(
        prompt,
        RunResult {
            duration_ms: Some(74_000),
            outcome: RunOutcome::Completed,
        },
    );
    let file = |path: &str, status: &str, before: &str, after: &str| GitDiffFile {
        path: path.into(),
        status: status.into(),
        before: before.into(),
        after: after.into(),
        ..Default::default()
    };
    state.git_diff.snapshot = Some(Arc::new(GitDiffSnapshot {
        generation: 1,
        head: "demo".into(),
        files: vec![
            file(
                "src/demo_app/client.py",
                "modified",
                "    while True:\n",
                "    for attempt in range(MAX_RETRIES):\n",
            ),
            file(
                "tests/test_client.py",
                "modified",
                "",
                "def test_retry_limit():\n    ...\n",
            ),
        ],
        truncated: false,
    }));
    state
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
fn tool_activity_matches_the_mockup_across_themes_and_resize() {
    for selected in theme::themes() {
        for no_color in [false, true] {
            let mut ui = fixture("tool-activity");
            ui.theme.active = selected;
            ui.no_color = no_color;
            for width in [40, 80, 200, 80] {
                let buffer = draw(&mut ui, width, 64);
                let tail = row_containing(&buffer, "36 passed (8.1s)");
                let omission = row_containing(&buffer, "30 earlier lines");
                assert_eq!(buffer[(10, tail)].bg, ui.palette().surface);
                assert_eq!(buffer[(10, omission)].bg, ui.palette().surface);
                assert!(!buffer[(10, tail)].modifier.contains(Modifier::DIM));
                if width >= 80 {
                    row_containing(&buffer, "linear/list_issues · astro");
                    row_containing(&buffer, "linear/get_issue · 5 calls");
                    row_containing(&buffer, "npm test && npm run check");
                    row_containing(&buffer, "Browser tests · passed");
                }
            }
        }
    }
}

#[tokio::test]
async fn earlier_output_is_independently_browsable_and_clickable() {
    let mut ui = fixture("tool-activity");
    let process = ui.state.transcript.entries().last().unwrap().id;
    draw(&mut ui, 100, 64);
    assert!(
        ui.visible_foldable_entries()
            .contains(&BrowseTarget::Output(process))
    );
    ui.browse_selected = Some(BrowseTarget::Output(process));
    let (writer, _receiver) = mpsc::channel(4);
    ui.handle_browse_key(
        KeyEvent::new(KeyCode::Right, KeyModifiers::NONE),
        &writer,
        1024,
    )
    .await
    .unwrap();
    let buffer = draw(&mut ui, 100, 64);
    row_containing(&buffer, "check 1 passed");
    row_containing(&buffer, "36 passed (8.1s)");
    row_containing(&buffer, "showing retained output");
    // Resizing keeps the earlier-output selection separate from its parent card.
    draw(&mut ui, 80, 64);
    assert_eq!(ui.browse_selected, Some(BrowseTarget::Output(process)));
    ui.handle_browse_key(
        KeyEvent::new(KeyCode::Left, KeyModifiers::NONE),
        &writer,
        1024,
    )
    .await
    .unwrap();
    assert!(ui.transcript_row_cache.fold().is_expanded(process));
    assert!(!ui.transcript_row_cache.fold().is_output_expanded(process));
    // A real click on the omission row activates the same independent fold.
    ui.mouse_enabled = true;
    let buffer = draw(&mut ui, 100, 64);
    let y = row_containing(&buffer, "30 earlier lines");
    for kind in [
        MouseEventKind::Down(MouseButton::Left),
        MouseEventKind::Up(MouseButton::Left),
    ] {
        ui.handle_input(
            Input::Mouse(MouseEvent {
                kind,
                column: 8,
                row: y,
                modifiers: KeyModifiers::NONE,
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
    }
    assert!(ui.transcript_row_cache.fold().is_output_expanded(process));
    ui.toggle_clicked_fold(BrowseTarget::Entry(process));
    assert!(!ui.transcript_row_cache.fold().is_expanded(process));
    assert!(!ui.transcript_row_cache.fold().is_output_expanded(process));
}

#[test]
fn expanded_output_scrolls_one_line_at_a_time_beneath_the_pinned_prompt() {
    let mut ui = fixture("tool-activity-long");
    let process = ui.state.transcript.entries().last().unwrap().id;
    draw(&mut ui, 80, 24);
    ui.browse_selected = Some(BrowseTarget::Output(process));
    ui.expand_selected_fold();
    let last_check = |buffer: &ratatui::buffer::Buffer| {
        (0..buffer.area.height)
            .filter_map(|y| {
                let text = (0..buffer.area.width)
                    .map(|x| buffer[(x, y)].symbol())
                    .collect::<String>();
                text.split_once("check ")?
                    .1
                    .split_whitespace()
                    .next()?
                    .parse::<usize>()
                    .ok()
            })
            .next_back()
            .unwrap()
    };
    let mut last = last_check(&draw(&mut ui, 80, 24));
    for _ in 0..8 {
        ui.transcript_viewport.reduce(
            TranscriptViewAction::ScrollLines(1),
            &ui.state.transcript,
            &mut ui.transcript_row_cache,
        );
        let next = last_check(&draw(&mut ui, 80, 24));
        assert_eq!(next, last + 1);
        last = next;
    }
}

#[tokio::test]
async fn long_output_stays_anchored_and_browsable_during_process_updates() {
    let mut ui = fixture("tool-activity-long");
    let process = ui.state.transcript.entries().last().unwrap().id;
    let first_retained = ui
        .state
        .transcript
        .entries()
        .last()
        .unwrap()
        .process_card()
        .unwrap()
        .retained_output
        .text
        .lines()
        .next()
        .unwrap()
        .to_owned();
    draw(&mut ui, 80, 24);
    ui.browse_selected = Some(BrowseTarget::Output(process));
    let (writer, _receiver) = mpsc::channel(4);
    ui.handle_browse_key(
        KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let buffer = draw(&mut ui, 80, 24);
    row_containing(&buffer, "492 earlier lines");
    row_containing(&buffer, &first_retained);
    assert!(!ui.transcript_viewport.follows_tail());
    assert_eq!(ui.browse_selected, Some(BrowseTarget::Output(process)));
    for value in [
        json!({"type": "tool.call", "call_id": "live-poll", "name": "bash", "arguments": {"operation": "poll", "process_id": "browser-process"}}),
        json!({"type": "tool.result", "message_entry_id": null, "call_id": "live-poll", "name": "bash", "output": "", "is_error": false,
            "process_id": "browser-process", "process_state": "running", "stdout": "new output\n"}),
    ] {
        let event = BackendEvent::from_projection_value(&value).unwrap();
        ui.dispatch(UiAction::BackendEvent(event), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.browse_selected, Some(BrowseTarget::Output(process)));
        let buffer = draw(&mut ui, 80, 24);
        row_containing(&buffer, "492 earlier lines");
        assert_eq!(ui.browse_selected, Some(BrowseTarget::Output(process)));
    }
    ui.handle_browse_key(
        KeyEvent::new(KeyCode::Left, KeyModifiers::NONE),
        &writer,
        8192,
    )
    .await
    .unwrap();
    let buffer = draw(&mut ui, 80, 24);
    row_containing(&buffer, "new output");
    assert!(ui.transcript_row_cache.fold().is_expanded(process));
    assert!(!ui.transcript_row_cache.fold().is_output_expanded(process));
}

#[test]
fn diff_previews_match_the_mockup_and_keep_full_review_available() {
    for selected in theme::themes() {
        for no_color in [false, true] {
            let mut ui = fixture("diff-previews");
            ui.theme.active = selected;
            ui.no_color = no_color;
            let palette = ui.palette();
            for width in [40, 80, 200, 80] {
                let buffer = draw(&mut ui, width, 64);
                let removed = row_containing(&buffer, ".mermaid svg * {");
                let added = row_containing(&buffer, "await page.goto");
                let omission = row_containing(&buffer, "5 more changed lines");
                let column = if ui.side_panel.docked() {
                    ui.side_panel.split(buffer.area).0.width
                } else {
                    width
                };
                for x in [column / 2, column - 5] {
                    assert_eq!(buffer[(x, removed)].bg, palette.deletion_background);
                    assert_eq!(buffer[(x, added)].bg, palette.addition_background);
                    assert_eq!(buffer[(x, omission)].bg, palette.surface);
                }
                let screen = buffer
                    .content
                    .iter()
                    .map(|cell| cell.symbol())
                    .collect::<String>();
                assert!(!screen.contains("menu.click"));
                assert!(!screen.contains("⋯ ▸"));
            }
            let entry = ui.state.transcript.entries().last().unwrap();
            let card = entry.tool_card().unwrap();
            let tool_detail::DetailAvailability::LiveRetained(detail) = &card.structured_detail
            else {
                panic!("expected retained write diff");
            };
            assert_eq!(detail.additions, 8);
            ui.detail_view.open(entry.id, detail);
            let buffer = draw(&mut ui, 100, 40);
            row_containing(&buffer, "await menu.click()");
            row_containing(&buffer, "await page.keyboard.press('Escape')");
        }
    }
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
            // A theme drawn on the terminal's own background has no prompt band.
            let band = palette.user_text().bg.unwrap_or(Color::Reset);
            let buffer = draw(&mut ui, 100, 48);
            let user = row_containing(&buffer, "Review the startup");
            let label = row_containing(&buffer, "you");
            assert_eq!(buffer[(3, label - 1)].symbol(), " ");
            // The prompt's bar covers its top padding too.
            assert_eq!(buffer[(2, label - 1)].symbol(), buffer[(2, label)].symbol());
            assert_eq!(buffer[(2, label - 1)].bg, band);
            assert_eq!(buffer[(97, label - 1)].bg, band);
            assert_eq!(buffer[(3, label - 2)].bg, canvas);
            assert_eq!(buffer[(2, user)].bg, band);
            assert_eq!(buffer[(97, user)].bg, band);
            assert_eq!(buffer[(1, user)].bg, canvas);
            assert_eq!(buffer[(98, user)].bg, canvas);
            assert_eq!(buffer[(3, user)].fg, palette.foreground);
            assert_eq!(
                buffer[(3, user)].modifier.contains(Modifier::BOLD),
                palette.user_text().bg.is_none()
            );
            assert_eq!(buffer[(3, user + 1)].symbol(), " ");
            assert_eq!(buffer[(2, user + 1)].bg, band);
            assert_eq!(buffer[(97, user + 1)].bg, band);
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

/// The cells of row `y` whose background is `fill`.
fn cells_on(buffer: &ratatui::buffer::Buffer, y: u16, fill: Color) -> usize {
    (0..buffer.area.width)
        .filter(|&x| buffer[(x, y)].bg == fill)
        .count()
}

/// Where `text` starts on row `y`, in columns, if it is there.
fn column_of_opt(buffer: &ratatui::buffer::Buffer, y: u16, text: &str) -> Option<u16> {
    let line = (0..buffer.area.width)
        .map(|x| buffer[(x, y)].symbol())
        .collect::<String>();
    line.find(text)
        .map(|start| u16::try_from(line[..start].chars().count()).unwrap())
}

/// Where `text` starts on row `y`, in columns.
fn column_of(buffer: &ratatui::buffer::Buffer, y: u16, text: &str) -> u16 {
    column_of_opt(buffer, y, text).unwrap_or_else(|| panic!("{text:?} not on row {y}"))
}

#[test]
fn headings_take_level_colours() {
    for depth in [ColorDepth::TrueColor, ColorDepth::Ansi256] {
        let mut ui = fixture("typography");
        ui.color_depth = depth;
        let palette = ui.palette();
        let shown = |color| depth.downconvert(color);
        let buffer = draw(&mut ui, 80, 30);
        let screen = screen_text(&buffer);
        // The `#` markers are not drawn.
        assert!(!screen.contains("# "), "{screen}");
        let cell_at = |text: &str| {
            let y = row_containing(&buffer, text);
            buffer[(column_of(&buffer, y, text), y)].clone()
        };
        let levels = [
            ("Startup review", palette.heading_1),
            ("Fixes", palette.heading_2),
            ("Risk", palette.heading_3),
        ];
        for (text, color) in levels {
            let cell = cell_at(text);
            assert_eq!(cell.fg, shown(color), "{text} {depth:?}");
            assert!(cell.modifier.contains(Modifier::BOLD), "{text}");
        }
        // Each level reads apart from the others and from the speaker label.
        let colours = [
            shown(palette.primary),
            shown(palette.heading_1),
            shown(palette.heading_2),
            shown(palette.heading_3),
        ];
        for (index, colour) in colours.iter().enumerate() {
            assert!(
                !colours[index + 1..].contains(colour),
                "{depth:?} {colours:?}"
            );
        }
        let code = cell_at("load()");
        assert_eq!(code.fg, shown(palette.code), "{depth:?}");
        assert!(code.modifier.contains(Modifier::BOLD));
    }
}

#[test]
fn thought_label_is_quiet_bold() {
    for depth in [ColorDepth::TrueColor, ColorDepth::Ansi256] {
        let mut ui = fixture("typography");
        ui.color_depth = depth;
        let muted = depth.downconvert(ui.palette().muted);
        let buffer = draw(&mut ui, 80, 30);
        let y = row_containing(&buffer, "◆ Thought for ");
        let label = column_of(&buffer, y, "Thought");
        assert_eq!(buffer[(label, y)].fg, muted, "{depth:?}");
        assert!(buffer[(label, y)].modifier.contains(Modifier::BOLD));
        // Its time and summary are muted but not bold.
        for text in ["for ", "Check the config"] {
            let x = column_of(&buffer, y, text);
            assert_eq!(buffer[(x, y)].fg, muted, "{text} {depth:?}");
            assert!(!buffer[(x, y)].modifier.contains(Modifier::BOLD), "{text}");
        }
    }
}

#[test]
fn footer_mode_takes_colour() {
    for depth in [ColorDepth::TrueColor, ColorDepth::Ansi256] {
        let mut ui = fixture("typography");
        ui.color_depth = depth;
        // A later turn with the same settings leaves them, and the mode, off its row.
        ui.state.transcript.append_prompt("Again".into());
        ui.state.transcript.complete_message(2, "Done.".into());
        assert!(ui.state.transcript.attach_run_summary(typography_totals()));
        ui.transcript_viewport.reduce(
            TranscriptViewAction::Home,
            &ui.state.transcript,
            &mut ui.transcript_row_cache,
        );
        let palette = ui.palette();
        let (warning, muted) = (
            depth.downconvert(palette.warning),
            depth.downconvert(palette.muted),
        );
        let buffer = draw(&mut ui, 80, 40);
        let y = row_containing(&buffer, "Plan · demo");
        let start = column_of(&buffer, y, "Plan");
        for x in start..start + 4 {
            assert_eq!(buffer[(x, y)].fg, warning, "column {x} {depth:?}");
            assert!(buffer[(x, y)].modifier.contains(Modifier::BOLD));
        }
        assert_eq!(buffer[(start + 4, y)].fg, muted, "{depth:?}");
        assert!(!buffer[(start + 4, y)].modifier.contains(Modifier::BOLD));
        let later = (y + 1..buffer.area.height)
            .find(|&row| column_of_opt(&buffer, row, "╰").is_some())
            .expect("the later turn's summary row");
        assert!(
            (0..buffer.area.width).all(|x| buffer[(x, later)].fg != warning),
            "{}",
            screen_text(&buffer)
        );
    }
}

#[test]
fn diff_band_skips_line_numbers() {
    for depth in [ColorDepth::TrueColor, ColorDepth::Ansi256] {
        let mut ui = fixture("diff-previews");
        ui.color_depth = depth;
        let palette = ui.palette();
        let shown = |color| depth.downconvert(color);
        let buffer = draw(&mut ui, 100, 64);
        for (text, band) in [
            (".mermaid svg * {", palette.deletion_background),
            ("await page.goto", palette.addition_background),
        ] {
            let y = row_containing(&buffer, text);
            let digit = (0..buffer.area.width)
                .find(|&x| buffer[(x, y)].symbol().chars().all(|c| c.is_ascii_digit()))
                .expect("a line number");
            assert_eq!(
                buffer[(digit, y)].bg,
                shown(palette.background),
                "{depth:?}"
            );
            assert_eq!(buffer[(90, y)].bg, shown(band), "{depth:?}");
            assert_ne!(shown(band), shown(palette.background), "{depth:?}");
        }
    }
}

#[test]
fn wrapped_diff_band_aligns() {
    let mut ui = fixture("diff-previews");
    let palette = ui.palette();
    let buffer = draw(&mut ui, 40, 64);
    let band_start = |y: u16| {
        (0..buffer.area.width)
            .find(|&x| buffer[(x, y)].bg == palette.addition_background)
            .unwrap_or_else(|| panic!("no band on row {y}"))
    };
    let wrapped = (1..buffer.area.height)
        .find(|&y| {
            (0..buffer.area.width).any(|x| buffer[(x, y)].symbol() == "↳")
                && buffer[(30, y)].bg == palette.addition_background
        })
        .expect("a wrapped added line");
    assert_eq!(band_start(wrapped), band_start(wrapped - 1));
    assert_eq!(buffer[(band_start(wrapped), wrapped)].symbol(), "↳");
}

#[test]
fn browse_selection_draws_band() {
    for depth in [ColorDepth::TrueColor, ColorDepth::Ansi256] {
        let mut ui = fixture("tools");
        ui.color_depth = depth;
        let palette = ui.palette();
        let band = depth.downconvert(palette.selected);
        let buffer = draw(&mut ui, 100, 40);
        let y = row_containing(&buffer, "Ran cargo test");
        // The band runs the transcript's width, rail and gutter included.
        assert_eq!(buffer[(2, y)].bg, band, "{depth:?}");
        let banded = (0..buffer.area.height)
            .filter(|&row| cells_on(&buffer, row, band) > 60)
            .collect::<Vec<_>>();
        assert_eq!(banded, [y], "{depth:?}");
        for tier in [palette.background, palette.surface, palette.panel] {
            assert_ne!(band, depth.downconvert(tier), "{depth:?}");
        }
    }
}

#[test]
fn reversed_selection_without_bands() {
    for (theme, no_color, depth) in [
        (theme::default_theme(), true, ColorDepth::TrueColor),
        (
            theme::resolve("glass").unwrap(),
            false,
            ColorDepth::TrueColor,
        ),
        (theme::default_theme(), false, ColorDepth::Ansi16),
    ] {
        let mut ui = fixture("tools");
        ui.theme.active = theme;
        ui.no_color = no_color;
        ui.color_depth = depth;
        let buffer = draw(&mut ui, 100, 40);
        let y = row_containing(&buffer, "Ran cargo test");
        let reversed = (0..buffer.area.height)
            .filter(|&row| buffer[(10, row)].modifier.contains(Modifier::REVERSED))
            .collect::<Vec<_>>();
        assert_eq!(reversed, [y], "{} {depth:?}", theme.slug);
    }
}

#[test]
fn scrolled_back_user_surface_keeps_a_plain_gap_after_the_previous_reply() {
    let mut ui = fixture("scrollback");
    let palette = ui.palette();
    let canvas = palette.base().bg.unwrap_or(Color::Reset);
    let buffer = draw(&mut ui, 80, 52);
    let prompt = row_containing(&buffer, "Now check the remaining edge cases.");
    let label = prompt - 1;
    assert_eq!(buffer[(3, label)].symbol(), "y");

    assert_eq!(buffer[(2, label - 1)].bg, palette.panel);
    assert_eq!(buffer[(77, label - 1)].bg, palette.panel);
    assert_eq!(screen_row(&buffer, label - 2).trim(), "");
    assert_eq!(buffer[(2, label - 2)].bg, canvas);
    assert_eq!(buffer[(77, label - 2)].bg, canvas);
    assert!(screen_row(&buffer, label - 3).contains("Tests passed"));
}

#[test]
fn scrolling_up_from_the_live_view_keeps_the_prompt_until_it_is_reached() {
    let mut ui = LiveUi {
        state: UiState::new("fake".into(), None, None),
        ..LiveUi::default()
    };
    ui.state.transcript.append_prompt("first".into());
    ui.state.transcript.complete_message(1, "short".into());
    let prompt = ui
        .state
        .transcript
        .append_prompt("Check the edge cases.".into());
    let mut reply = String::new();
    for line in 0..30 {
        reply.push_str(&format!("reply line {line}\n"));
    }
    ui.state.transcript.complete_message(2, reply);
    let last_reply_line = |buffer: &ratatui::buffer::Buffer| {
        (0..buffer.area.height)
            .filter_map(|y| {
                let row = screen_row(buffer, y);
                // Replies sit beside the turn's rail.
                row.trim()
                    .trim_start_matches('│')
                    .trim_start()
                    .strip_prefix("reply line ")?
                    .split_whitespace()
                    .next()?
                    .parse::<usize>()
                    .ok()
            })
            .max()
    };

    let mut buffer = draw(&mut ui, 60, 14);
    let mut last = last_reply_line(&buffer);
    assert_eq!(last, Some(29));
    let prompt_header = crate::transcript_view::RowAnchor {
        entry_id: prompt,
        position: RowPosition::Header,
    };
    while ui.transcript_viewport.scrolled_top() != Some(prompt_header) {
        ui.transcript_viewport.reduce(
            TranscriptViewAction::ScrollLines(-1),
            &ui.state.transcript,
            &mut ui.transcript_row_cache,
        );
        buffer = draw(&mut ui, 60, 14);
        row_containing(&buffer, "Check the edge cases.");
        let next = last_reply_line(&buffer);
        assert_eq!(next, last.and_then(|line| line.checked_sub(1)));
        last = next;
    }
    assert!(row_containing(&buffer, "reply line 0") > row_containing(&buffer, "Check the edge"));
}

fn screen_row(buffer: &ratatui::buffer::Buffer, y: u16) -> String {
    (0..buffer.area.width)
        .map(|x| buffer[(x, y)].symbol())
        .collect()
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
        // A framed composer ends with its bottom border, which carries the identity.
        let bottom_padding = if height >= 16 { 1 } else { 0 };
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
        // The status line sits right above the composer for the whole run, so the
        // reply streams above it without the transcript changing size.
        let status = composer_top - 1;
        assert!(reply < status);
        let status_text = (0..width)
            .map(|x| buffer[(x, status)].symbol())
            .collect::<String>();
        assert!(status_text.contains("◐ Working"), "{status_text}");
        assert!(!(0..width).any(|x| buffer[(x, height - 1)].symbol() == "◐"));
        if height >= 16 {
            for x in 0..width {
                assert_eq!(buffer[(x, 0)].symbol(), " ");
                assert_eq!(buffer[(x, status - 1)].symbol(), " ");
                assert_eq!(buffer[(x, status - 1)].bg, ui.palette().background);
            }
        }
        ui.activity_frame = 1;
        let next = draw(&mut ui, width, height);
        assert!(screen_text(&next).contains("wisp"));
        assert!(screen_text(&next).contains("◓ Working"));
        assert_eq!(row_containing(&next, "wisp"), reply);
        ui.state.view_status = ViewStatus::Idle;
        ui.state.interaction_status = InteractionStatus::Idle;
        ui.state.current_command = None;
        assert!(!screen_text(&draw(&mut ui, width, height)).contains("Working"));
    }
}

#[tokio::test]
async fn completed_reply_keeps_its_layout_until_command_finishes() {
    use std::fmt::Write as _;

    for (width, height) in [(100, 30), (80, 24), (40, 16), (30, 8)] {
        let mut ui = fixture("new-turn");
        ui.mouse_enabled = true;
        let (writer, _commands) = mpsc::channel(32);
        let mut content = String::new();
        for index in 0..40 {
            writeln!(content, "Response line {index}.\n").unwrap();
        }
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::MessageDelta {
                turn: 2,
                delta: content.clone(),
                content_kind: reducer::MessageContentKind::Text,
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        let streaming = draw(&mut ui, width, height);
        let composer_top = ui
            .mouse_frame
            .as_ref()
            .unwrap()
            .conversation
            .editor
            .as_ref()
            .unwrap()
            .area
            .y
            - u16::from(height >= 16);
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::MessageCompleted {
                turn: 2,
                content,
                cost: None,
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        // Paint the real intermediate state, even if command.finished is delayed
        // by persistence or transport. The run is still going, so the status line
        // stays and the transcript keeps its size.
        let completed = draw(&mut ui, width, height);
        assert!(screen_text(&completed).contains("Working"));
        ui.dispatch(
            UiAction::BackendEvent(BackendEvent::CommandFinished {
                command_id: "visual-next-prompt".into(),
                command_type: "prompt".into(),
                ok: true,
                error: None,
            }),
            &writer,
            8192,
        )
        .await
        .unwrap();
        let idle = draw(&mut ui, width, height);
        // Above the status line, the finished reply looks the same as it streamed.
        for y in 0..composer_top.saturating_sub(1) {
            for x in 0..width {
                assert_eq!(
                    streaming[(x, y)],
                    completed[(x, y)],
                    "{width}x{height} at {x},{y}"
                );
            }
        }
        // At idle the status line gives its row back to the transcript.
        assert!(!screen_text(&idle).contains("Working"));
        assert!(screen_text(&idle).contains("Response line 39."));
    }
}

#[test]
fn new_turn_hides_previous_reply_until_scrolled_back() {
    let mut ui = fixture("new-turn");
    let live = draw(&mut ui, 80, 24);
    let text = screen_text(&live);
    assert!(text.contains("Now check the remaining edge cases."));
    assert!(!text.contains("Startup review"));
    assert!(!text.contains("Configuration loads"));
    assert!(text.contains("◐ Working"));
    // The transcript draws no scrollbar on its right edge.
    assert!((0..live.area.height).all(|y| !matches!(live[(79, y)].symbol(), "│" | "┃")));
    ui.transcript_viewport.reduce(
        TranscriptViewAction::Home,
        &ui.state.transcript,
        &mut ui.transcript_row_cache,
    );
    let history = draw(&mut ui, 80, 24);
    assert!(screen_text(&history).contains("Startup review"));
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
    // The status line stays while the reply streams.
    assert!(streaming.contains("◐ Working"));
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
        .filter(|c| !c.is_whitespace() && !matches!(c, '│' | '┃' | '▎'))
        .collect();
    assert!(compact.contains("Nowchecktheremainingedgecases."), "{text}");
    assert!(text.contains("◐ Working"), "{text}");
    assert!(!text.contains("Startup review"));
}

#[test]
fn documentation_mcp_capture_shows_synthetic_server_and_tools() {
    let text = screen_text(&draw(&mut fixture("mcp"), 100, 30));
    assert!(text.contains("demo-docs  connected · 2 tools"));
    assert!(text.contains("search_docs"));
    assert!(text.contains("read_page"));
    assert!(text.contains("a add · e on/off · d remove"));
}

#[test]
fn showcase_capture_docks_panel() {
    let text = screen_text(&draw(&mut fixture("showcase"), 150, 34));
    for expected in [
        "Cap the retries at three and run the client tests.",
        "demo-docs/search_docs",
        "for attempt in range(MAX_RETRIES):",
        "2/2 connected",
        "python-testing",
        "M src/demo_app/client.py",
        "Recent turns",
        "Ctrl+T tree",
    ] {
        assert!(text.contains(expected), "{expected}\n{text}");
    }
}

#[test]
fn welcome_capture_shows_mascot() {
    let text = screen_text(&draw(&mut fixture("welcome"), 150, 34));
    assert!(text.chars().any(|c| ('\u{2801}'..='\u{28ff}').contains(&c)));
    assert!(text.contains("A coding agent that stays in sync"));
    assert!(text.contains("No changes"));
}

#[test]
#[ignore = "writes review artifacts only when explicitly requested"]
fn capture_conversation_screens() {
    let output =
        PathBuf::from(std::env::var_os("WISP_VISUAL_OUTPUT").expect("set WISP_VISUAL_OUTPUT"));
    fs::create_dir_all(&output).unwrap();
    for (width, height) in [
        (100, 30),
        (80, 24),
        (40, 16),
        (30, 8),
        (100, 64),
        (150, 34),
        (150, 40),
        (200, 40),
    ] {
        for (variant, selected, no_color) in [
            ("dark", theme::default_theme(), false),
            ("ember", theme::resolve("ember").unwrap(), false),
            ("grove", theme::resolve("grove").unwrap(), false),
            ("light", theme::paper_theme(), false),
            ("mono", theme::default_theme(), true),
        ] {
            for scenario in [
                "reading",
                "diff-previews",
                "tool-activity",
                "conversation",
                "tools",
                "working",
                "tool-running",
                "tool-dim",
                "approval",
                "permissions",
                "mcp",
                "new-turn",
                "scrollback",
                "activity-rail",
                "activity-rail-expanded",
                "showcase",
                "welcome",
                "welcome-adal",
                "welcome-classic",
                "typography",
                "composer-plan",
                "composer-yolo",
                "welcome-entrance",
            ] {
                let mut ui = fixture(scenario);
                ui.theme.active = selected;
                ui.no_color = no_color;
                let frames = match scenario {
                    "tool-running" if variant == "dark" && width == 100 && height == 30 => 8,
                    "welcome-entrance" if width == 100 && height == 30 => {
                        crate::startup_logo::Entrance::length(true) + 1
                    }
                    "welcome-entrance" => 0,
                    _ => 1,
                };
                for tick in 0..frames {
                    if frames > 1 {
                        ui.activity_frame = tick;
                    }
                    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
                    ui.draw(
                        &mut terminal,
                        &ConnectionInfo {
                            backend_version: env!("CARGO_PKG_VERSION").into(),
                        },
                    )
                    .unwrap();
                    let cells = terminal.backend().buffer().content.iter().map(|cell| json!({
                    "text": cell.symbol(), "fg": rgb(cell.fg), "bg": rgb(cell.bg),
                    "bold": cell.modifier.contains(Modifier::BOLD), "dim": cell.modifier.contains(Modifier::DIM), "italic": cell.modifier.contains(Modifier::ITALIC),
                    "underline": cell.modifier.contains(Modifier::UNDERLINED),
                    "underline_color": match cell.underline_color { Color::Rgb(..) => Some(rgb(cell.underline_color)), _ => None },
                    "reverse": cell.modifier.contains(Modifier::REVERSED),
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

/// Each link painted in `buffer`, as its URI and the text of its cells, in order.
fn painted_links(buffer: &ratatui::buffer::Buffer) -> Vec<(String, String)> {
    let mut links: Vec<(String, String)> = Vec::new();
    for cell in &buffer.content {
        let Some(rest) = cell.symbol().strip_prefix("\x1b]8;id=") else {
            continue;
        };
        let (_, rest) = rest.split_once(';').expect("an id ends at `;`");
        let (uri, rest) = rest.split_once("\x1b\\").expect("the URI ends at ST");
        let text = rest
            .strip_suffix("\x1b]8;;\x1b\\")
            .expect("each cell closes its own link");
        match links.last_mut().filter(|(last, _)| last == uri) {
            Some((_, painted)) => painted.push_str(text),
            None => links.push((uri.to_owned(), text.to_owned())),
        }
    }
    links
}

#[test]
fn links_paint_as_hyperlinks() {
    let mut ui = LiveUi::default();
    ui.state
        .transcript
        .append_prompt("Where are the docs?".into());
    ui.state.transcript.complete_message(
        1,
        "See [docs](https://example.test/docs)[more](https://example.test/more), \
         not [this](javascript:alert(1))."
            .into(),
    );
    ui.state.transcript.set_tool_cwd("/work");
    let BackendEvent::ToolCall(call) = BackendEvent::from_projection_value(&json!({
        "type": "tool.call", "call_id": "read-1", "name": "read",
        "arguments": {"path": "src/main.rs"}
    }))
    .unwrap() else {
        panic!("tool call")
    };
    ui.state.transcript.observe_tool_call(call);
    // Off by default, so every other test reads plain text.
    let buffer = draw(&mut ui, 80, 24);
    assert!(
        buffer
            .content
            .iter()
            .all(|cell| !cell.symbol().contains('\x1b')),
        "{buffer:?}"
    );

    ui.hyperlinks = true;
    let buffer = draw(&mut ui, 80, 24);
    let file = crate::hyperlink::file_uri(std::path::Path::new("/work/src/main.rs")).unwrap();
    // Adjacent links keep their own destinations; a script link stays plain text.
    assert_eq!(
        painted_links(&buffer),
        [
            ("https://example.test/docs".to_owned(), "docs".to_owned()),
            ("https://example.test/more".to_owned(), "more".to_owned()),
            (file.to_string(), "src/main.rs".to_owned()),
        ]
    );
    row_containing(&buffer, "not this.");
}

#[test]
fn search_pattern_is_not_a_link() {
    // A search names a file only through its first match, which Ctrl+O opens; the
    // pattern on its card is not a link to that file.
    let mut ui = LiveUi {
        hyperlinks: true,
        ..LiveUi::default()
    };
    ui.state.transcript.set_tool_cwd("/work");
    for event in [
        json!({
            "type": "tool.call", "call_id": "grep-1", "name": "grep",
            "arguments": {"pattern": "fn main"}
        }),
        json!({
            "type": "tool.result", "message_entry_id": null, "call_id": "grep-1",
            "name": "grep", "output": "src/main.rs:1:fn main() {}", "is_error": false
        }),
    ] {
        match BackendEvent::from_projection_value(&event).unwrap() {
            BackendEvent::ToolCall(call) => {
                ui.state.transcript.observe_tool_call(call);
            }
            BackendEvent::ToolResult(result) => {
                ui.state.transcript.observe_tool_result(*result);
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    let buffer = draw(&mut ui, 80, 24);
    row_containing(&buffer, "fn main");
    assert_eq!(painted_links(&buffer), []);
}

fn image_part(width: u64, height: u64) -> serde_json::Value {
    json!({"media_type": "image/png", "sha256": "0".repeat(64), "byte_size": 1234,
           "width": width, "height": height})
}

/// Draw one finished `read` card whose result returned `images`.
fn image_read_card(images: serde_json::Value, width: u16) -> ratatui::buffer::Buffer {
    let mut ui = LiveUi::default();
    for event in [
        json!({"type": "tool.call", "call_id": "read-1", "name": "read",
               "arguments": {"path": "screenshots/before-and-after.png"}}),
        json!({"type": "tool.result", "message_entry_id": null, "call_id": "read-1",
               "name": "read", "output": "Image file shot.png", "is_error": false,
               "images": images}),
    ] {
        match BackendEvent::from_projection_value(&event).unwrap() {
            BackendEvent::ToolCall(call) => {
                ui.state.transcript.observe_tool_call(call);
            }
            BackendEvent::ToolResult(result) => {
                ui.state.transcript.observe_tool_result(*result);
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    draw(&mut ui, width, 12)
}

/// The script every script-card test runs: six lines, so an opened card shows four.
const SCRIPT: &str = "import asyncio\nhits = await grep(pattern=\"TODO\", path=\"src\")\nfiles = sorted(hits)\ntexts = await asyncio.gather(*[read(path=f) for f in files])\nlen(texts)\ntexts";

fn script_call(title: Option<&str>) -> serde_json::Value {
    let mut arguments = json!({"code": SCRIPT});
    if let Some(title) = title {
        arguments["title"] = json!(title);
    }
    json!({"type": "tool.call", "call_id": "script-1", "name": "codemode", "arguments": arguments})
}

fn nested(
    call_id: &str,
    name: &str,
    arguments: serde_json::Value,
    error: Option<&str>,
) -> serde_json::Value {
    json!({"call_id": call_id, "name": name, "arguments": arguments,
           "is_error": error.is_some(), "error": error,
           "started_at": "2026-01-02T03:04:05Z", "duration_ms": 12})
}

/// Draw a script card after `events`, opened when `open` is set.
fn script_card(events: &[serde_json::Value], open: bool, width: u16) -> ratatui::buffer::Buffer {
    let mut ui = LiveUi::default();
    let mut card = None;
    for event in events {
        match BackendEvent::from_projection_value(event).unwrap() {
            BackendEvent::ToolCall(call) => {
                card = Some(ui.state.transcript.observe_tool_call(call))
            }
            BackendEvent::ToolResult(result) => {
                ui.state.transcript.observe_tool_result(*result);
            }
            BackendEvent::ScriptCallStarted {
                parent_call_id,
                call_id,
                tool,
                arguments,
            } => {
                ui.state
                    .transcript
                    .observe_script_call(&parent_call_id, |script| {
                        script.call_started(&call_id, &tool, arguments)
                    });
            }
            BackendEvent::ScriptCallEnded {
                parent_call_id,
                call,
            } => {
                ui.state
                    .transcript
                    .observe_script_call(&parent_call_id, |script| script.call_ended(call));
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    if open {
        ui.transcript_row_cache
            .fold_mut()
            .expand(card.expect("a script call"));
    }
    draw(&mut ui, width, 24)
}

fn finished_script(output: &str, is_error: bool) -> serde_json::Value {
    json!({"type": "tool.result", "message_entry_id": null, "call_id": "script-1",
    "name": "codemode", "output": output, "is_error": is_error,
    "nested_calls": [
        nested("n1", "grep", json!({"pattern": "TODO", "path": "src"}), None),
        nested("n2", "read", json!({"path": "src/a.rs"}), None),
        nested("n3", "read", json!({"path": "src/b.rs"}), None),
        nested("n4", "read", json!({"path": "../vendor/c.rs"}), Some("path outside the project")),
    ]})
}

#[test]
fn script_tallies_calls_while_running() {
    let buffer = script_card(
        &[
            script_call(Some("count TODOs")),
            json!({"type": "tool.nested.started", "parent_call_id": "script-1", "call_id": "n1",
                   "name": "grep", "arguments": {"pattern": "TODO"}}),
            json!({"type": "tool.nested.ended", "parent_call_id": "script-1",
                   "call": nested("n1", "grep", json!({"pattern": "TODO"}), None)}),
            json!({"type": "tool.nested.started", "parent_call_id": "script-1", "call_id": "n2",
                   "name": "read", "arguments": {"path": "a.rs"}}),
        ],
        false,
        100,
    );

    let row = buffer_row(&buffer, row_containing(&buffer, "Scripting"));
    assert!(
        row.contains("Scripting count TODOs · 2 calls · grep, read"),
        "{row:?}"
    );
    assert!(!screen_text(&buffer).contains("import asyncio"));
}

#[test]
fn finished_script_folds_to_one_line() {
    let buffer = script_card(
        &[
            script_call(Some("count TODOs")),
            finished_script("12\n[4 tool calls (1 failed)]", false),
        ],
        false,
        100,
    );

    let row = buffer_row(&buffer, row_containing(&buffer, "Scripted"));
    assert!(
        row.contains("• Scripted count TODOs · 4 calls (1 failed) · read ×3, grep"),
        "{row:?}"
    );
    assert!(!screen_text(&buffer).contains("Returned"));
}

#[test]
fn opened_script_shows_code_calls_and_result() {
    let buffer = script_card(
        &[
            script_call(None),
            finished_script("12\n[4 tool calls (1 failed)]", false),
        ],
        true,
        100,
    );

    let text = screen_text(&buffer);
    for expected in [
        "Scripted Python script, 6 lines",
        "1 import asyncio",
        "+ 2 more lines",
        "• Searched",
        "• Read src/a.rs · +1 more reads",
        "✗ Read ../vendor/c.rs · path outside the project",
        "Returned 12",
    ] {
        assert!(
            text.contains(expected),
            "{expected:?} missing from:\n{text}"
        );
    }
    assert!(
        !text.contains("5 len(texts)"),
        "only four lines show:\n{text}"
    );
}

#[test]
fn failed_script_peeks_at_its_cause() {
    let output = "Script failed (limit): The script made more than 200 tool calls.\nCalls made before it failed: 4 tool calls";
    let buffer = script_card(
        &[script_call(Some("scan")), finished_script(output, true)],
        false,
        100,
    );

    assert!(buffer_row(&buffer, row_containing(&buffer, "Scripted")).contains("✗ Scripted scan"));
    let peek = buffer_row(&buffer, row_containing(&buffer, "more than 200"));
    assert!(
        peek.contains("The script made more than 200 tool calls."),
        "{peek:?}"
    );
    assert!(!peek.contains("Script failed"), "{peek:?}");
}

#[test]
fn script_text_is_sanitized() {
    let buffer = script_card(
        &[
            script_call(Some("evil\u{1b}[31m\u{202e}title")),
            finished_script("ok", false),
        ],
        true,
        100,
    );

    let text = screen_text(&buffer);
    assert!(text.contains("Scripted evil[31mtitle"), "{text}");
    assert!(!text.contains('\u{1b}') && !text.contains('\u{202e}'));
}

#[test]
fn scripts_never_group() {
    let mut events = Vec::new();
    for id in ["script-1", "script-2"] {
        events.push(
            json!({"type": "tool.call", "call_id": id, "name": "codemode",
                           "arguments": {"code": "1", "title": id}}),
        );
        events.push(
            json!({"type": "tool.result", "message_entry_id": null, "call_id": id,
                           "name": "codemode", "output": "1", "is_error": false}),
        );
    }

    let text = screen_text(&script_card(&events, false, 100));

    assert!(
        text.contains("Scripted script-1") && text.contains("Scripted script-2"),
        "{text}"
    );
    assert!(!text.contains("2 calls"), "{text}");
}

#[test]
fn interrupted_script_closes_running_calls() {
    let mut ui = LiveUi::default();
    for event in [
        script_call(Some("scan")),
        json!({"type": "tool.nested.started", "parent_call_id": "script-1", "call_id": "n1",
               "name": "read", "arguments": {"path": "a.rs"}}),
    ] {
        match BackendEvent::from_projection_value(&event).unwrap() {
            BackendEvent::ToolCall(call) => {
                ui.state.transcript.observe_tool_call(call);
            }
            BackendEvent::ScriptCallStarted {
                parent_call_id,
                call_id,
                tool,
                arguments,
            } => {
                ui.state
                    .transcript
                    .observe_script_call(&parent_call_id, |script| {
                        script.call_started(&call_id, &tool, arguments)
                    });
            }
            other => panic!("unexpected {other:?}"),
        }
    }

    ui.state
        .transcript
        .settle_unresolved_tools("Agent run cancelled");

    let card = ui.state.transcript.entries()[0].tool_card().unwrap();
    let states: Vec<_> = card
        .script
        .as_ref()
        .unwrap()
        .calls
        .iter()
        .map(|call| call.state)
        .collect();
    assert_eq!(states, [crate::script_card::ScriptCallState::Unfinished]);
    // A late event for the finished script changes nothing.
    assert!(
        !ui.state
            .transcript
            .observe_script_call("script-1", |script| {
                script.call_started("n2", "read", serde_json::Value::Null)
            })
    );
}

#[test]
fn returned_row_shows_the_value() {
    // What the script printed comes first, then its value, then the call count.
    let output = "scanning\n[\"a.rs\", \"b.rs\"]\n[2 tool calls: read ×2]";
    let buffer = script_card(
        &[script_call(Some("list")), finished_script(output, false)],
        true,
        100,
    );

    let text = screen_text(&buffer);
    assert!(text.contains(r#"Returned ["a.rs", "b.rs"]"#), "{text}");
    assert!(!text.contains("Returned scanning"), "{text}");
}

#[test]
fn cancelled_script_says_why() {
    let mut ui = LiveUi::default();
    let BackendEvent::ToolCall(call) =
        BackendEvent::from_projection_value(&script_call(Some("scan"))).unwrap()
    else {
        panic!("a tool call");
    };
    let card = ui.state.transcript.observe_tool_call(call);
    ui.state
        .transcript
        .settle_unresolved_tools("Agent run cancelled");
    ui.transcript_row_cache.fold_mut().expand(card);

    let text = screen_text(&draw(&mut ui, 100, 24));
    assert!(text.contains("⊘ Script scan · cancelled"), "{text}");
    assert!(text.contains("Agent run cancelled"), "{text}");
}

fn buffer_row(buffer: &ratatui::buffer::Buffer, y: u16) -> String {
    (0..buffer.area.width)
        .map(|x| buffer[(x, y)].symbol())
        .collect()
}

#[test]
fn tool_card_names_images() {
    for (images, label) in [
        (json!([image_part(640, 480)]), "[image 640×480 png]"),
        (
            json!([image_part(640, 480), image_part(32, 32)]),
            "[2 images]",
        ),
    ] {
        let buffer = image_read_card(images, 80);
        let row = buffer_row(&buffer, row_containing(&buffer, "Read"));
        assert!(row.contains(label), "{row:?} lacks {label:?}");
    }
    let plain = image_read_card(json!([]), 80);
    let row = buffer_row(&plain, row_containing(&plain, "Read"));
    assert!(!row.contains("image"), "{row:?}");
}

#[test]
fn image_label_survives_narrow_row() {
    // The path gives way first; the label is kept whole, like a status note.
    let buffer = image_read_card(json!([image_part(640, 480)]), 60);
    let row = buffer_row(&buffer, row_containing(&buffer, "Read"));
    assert!(row.contains("[image 640×480 png]"), "{row:?}");
    assert!(!row.contains("before-and-after.png"), "{row:?}");
}
