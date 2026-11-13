//! Theme selection never owns runtime state; persistence is tested in isolated homes.

use super::*;
use crate::startup_logo::LogoChoice;
use ratatui::{
    backend::TestBackend,
    buffer::Buffer,
    style::{Color, Modifier, Style},
};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

struct TestHome(PathBuf);

impl TestHome {
    fn new() -> Self {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        loop {
            let path = std::env::temp_dir().join(format!(
                "wisp-theme-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&path) {
                Ok(()) => return Self(path),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("create isolated theme home: {error}"),
            }
        }
    }
    fn path(&self) -> PathBuf {
        self.0.join(".wisp/tui.json")
    }
    fn store(&self) -> ThemePreferences {
        ThemePreferences::at(self.path())
    }
    fn write(&self, bytes: &[u8]) {
        fs::create_dir_all(self.path().parent().unwrap()).unwrap();
        fs::write(self.path(), bytes).unwrap();
    }
    fn document(&self) -> Value {
        serde_json::from_slice(&fs::read(self.path()).unwrap()).unwrap()
    }
}

impl Drop for TestHome {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

fn key(code: KeyCode) -> Input {
    Input::Key(KeyEvent::new(code, KeyModifiers::NONE))
}

fn ctrl(character: char) -> Input {
    Input::Key(KeyEvent::new(
        KeyCode::Char(character),
        KeyModifiers::CONTROL,
    ))
}

fn ui(home: &TestHome) -> LiveUi {
    LiveUi {
        theme: home.store().load(),
        theme_preferences: Some(home.store()),
        // The theme toggle has no default chord; these tests bind it as users do.
        bindings: crate::keybindings::Bindings::from_json(
            r#"{"theme.toggle":["Ctrl+T"],"session.tree":[]}"#,
        )
        .unwrap(),
        ..LiveUi::default()
    }
}

fn draw(ui: &mut LiveUi, width: u16, height: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    ui.draw(
        &mut terminal,
        &ConnectionInfo {
            backend_version: "test".into(),
        },
    )
    .unwrap();
    terminal.backend().buffer().clone()
}

fn text(buffer: &Buffer) -> String {
    buffer.content.iter().map(|cell| cell.symbol()).collect()
}

#[test]
fn catalog_is_ordered_resolvable_and_monochrome_keeps_all_roles_achromatic() {
    assert_eq!(
        theme::themes()
            .iter()
            .map(|theme| theme.slug.as_str())
            .collect::<Vec<_>>(),
        [
            "vapor", "glass", "orchid", "ember", "storm", "grove", "wave", "paper", "dawn"
        ]
    );
    assert_eq!(theme::default_theme().name, "wisp");
    assert_eq!(theme::paper_theme().name, "wisp-light");
    for theme in theme::themes() {
        assert_eq!(
            theme::resolve(&theme.slug.to_uppercase()).unwrap().name,
            theme.name
        );
        assert_eq!(theme::named(&theme.name).unwrap().slug, theme.slug);
        let color = theme.palette(false);
        let gray = theme.palette(true);
        let channels = |palette: Palette| {
            [
                palette.background,
                palette.foreground,
                palette.primary,
                palette.secondary,
                palette.accent,
                palette.success,
                palette.warning,
                palette.error,
                palette.surface,
                palette.panel,
                palette.muted,
                palette.addition,
                palette.addition_background,
                palette.deletion,
                palette.deletion_background,
                palette.logo_tip,
                palette.logo_middle,
                palette.logo_base,
            ]
        };
        for (index, (original, mono)) in channels(color).into_iter().zip(channels(gray)).enumerate()
        {
            let Color::Rgb(r, g, b) = original else {
                panic!("generated RGB")
            };
            let value = (0.2126 * f64::from(r) + 0.7152 * f64::from(g) + 0.0722 * f64::from(b))
                .round() as u8;
            let Color::Rgb(r, g, b) = mono else {
                panic!("monochrome RGB")
            };
            assert_eq!(r, g);
            assert_eq!(g, b);
            if [0, 8, 9, 12, 14].contains(&index) {
                assert_eq!(
                    mono,
                    Color::Rgb(value, value, value),
                    "backgrounds retain Textual's conversion"
                );
            }
        }
        for foreground in [
            gray.foreground,
            gray.primary,
            gray.secondary,
            gray.accent,
            gray.success,
            gray.error,
            gray.muted,
        ] {
            for background in [gray.background, gray.surface] {
                assert!(theme::contrast_ratio(foreground, background) >= 4.5);
            }
        }
        assert!(theme::contrast_ratio(gray.warning, gray.panel) >= 4.5);
        assert!(theme::contrast_ratio(gray.addition, gray.addition_background) >= 4.5);
        assert!(theme::contrast_ratio(gray.deletion, gray.deletion_background) >= 4.5);
        assert!(gray.selection().add_modifier.contains(Modifier::REVERSED));
        assert!(
            gray.text_selection()
                .add_modifier
                .contains(Modifier::REVERSED)
        );
        assert_eq!(color.text_selection().bg, Some(color.primary));
        assert_eq!(color.text_selection().fg, Some(color.background));
    }
    let glass = theme::resolve("glass").unwrap().palette(false);
    assert_eq!(glass.base().bg, None);
    assert_eq!(glass.overlay().bg, Some(glass.surface));
    let vapor = theme::resolve("vapor").unwrap().palette(false);
    assert_eq!(vapor.base().bg, Some(vapor.background));
    assert_eq!(vapor.overlay().bg, Some(vapor.background));
    assert!(theme::resolve("not-a-theme").is_none());
    assert!(
        theme::named("paper").is_none(),
        "stored names, not command aliases"
    );
}

#[test]
fn selection_reverses_without_bands() {
    use crate::color_depth::ColorDepth;
    let vapor = theme::resolve("vapor").unwrap();
    for depth in [ColorDepth::TrueColor, ColorDepth::Ansi256] {
        let band = vapor.palette(false).for_depth(depth);
        assert_eq!(band.selection().bg, Some(band.selected));
        assert_eq!(band.selection().fg, Some(band.foreground));
        assert!(band.selection().add_modifier.contains(Modifier::BOLD));
        assert!(!band.selection().add_modifier.contains(Modifier::REVERSED));
    }
    for palette in [
        vapor.palette(true),
        vapor.palette(false).for_depth(ColorDepth::Ansi16),
        theme::resolve("glass").unwrap().palette(false),
    ] {
        assert_eq!(palette.selection().bg, None);
        assert!(
            palette
                .selection()
                .add_modifier
                .contains(Modifier::REVERSED | Modifier::BOLD)
        );
        // A list highlight patches the row after drawing it, so a coloured span would
        // reverse into its own block unless the selection sets the foreground.
        let item = ratatui::text::Line::from(vec![
            ratatui::text::Span::raw("name "),
            ratatui::text::Span::styled("detail", Style::default().fg(palette.muted)),
        ]);
        let mut terminal = Terminal::new(TestBackend::new(12, 1)).unwrap();
        terminal
            .draw(|frame| {
                let list = ratatui::widgets::List::new([ratatui::widgets::ListItem::new(item)])
                    .highlight_style(palette.selection());
                let mut state = ratatui::widgets::ListState::default().with_selected(Some(0));
                frame.render_stateful_widget(list, frame.area(), &mut state);
            })
            .unwrap();
        let buffer = terminal.backend().buffer();
        assert!(
            buffer
                .content
                .iter()
                .all(|cell| cell.fg == palette.foreground),
            "{buffer:?}"
        );
    }
}

#[test]
fn band_text_stays_readable() {
    use crate::color_depth::ColorDepth;
    for theme in theme::themes() {
        for depth in [ColorDepth::TrueColor, ColorDepth::Ansi256] {
            let palette = theme.palette(false).for_depth(depth);
            if !palette.shows_bands() {
                continue;
            }
            let texts = [
                palette.foreground,
                palette.muted,
                palette.primary,
                palette.secondary,
                palette.accent,
                palette.success,
                palette.warning,
                palette.error,
                palette.rail(),
                palette.chip("Read").fg.unwrap(),
                palette.chip("MCP").fg.unwrap(),
            ];
            for text in texts {
                let shown = palette.selected_row(Style::default().fg(text)).fg.unwrap();
                let ratio = theme::contrast_ratio(shown, palette.selected);
                assert!(
                    ratio >= 4.5,
                    "{} {depth:?} {text:?}: {ratio:.2}",
                    theme.slug
                );
            }
            // Text that already reads keeps its colour.
            let foreground = palette.selected_row(Style::default().fg(palette.foreground));
            assert_eq!(foreground.fg, Some(palette.foreground));
        }
    }
}

#[test]
fn monochrome_diff_bands_show() {
    let level = |color: Color| match color {
        Color::Rgb(value, _, _) => i16::from(value),
        other => panic!("expected a gray, got {other:?}"),
    };
    for theme in theme::themes() {
        let gray = theme.palette(true);
        for band in [gray.addition_background, gray.deletion_background] {
            let apart = (level(band) - level(gray.background)).abs();
            assert!(apart >= 10, "{} {band:?}: {apart} levels", theme.slug);
        }
    }
}

#[test]
fn plan_border_stays_apart() {
    use crate::color_depth::ColorDepth;
    // Plan mode reads from the composer's border alone, so its tint shows apart from the
    // normal border wherever the theme's warning colour does.
    for theme in theme::themes() {
        for depth in [
            ColorDepth::TrueColor,
            ColorDepth::Ansi256,
            ColorDepth::Ansi16,
        ] {
            let palette = theme.palette(false).for_depth(depth);
            let shown = |color| depth.downconvert(color);
            if shown(palette.warning) == shown(palette.muted) {
                continue;
            }
            assert_ne!(
                shown(palette.plan_border()),
                shown(palette.muted),
                "{} at {depth:?}",
                theme.slug
            );
        }
    }
    // Without colour the border stays a grey at every depth, so NO_COLOR keeps no hue.
    for depth in [ColorDepth::TrueColor, ColorDepth::Ansi16] {
        let border = theme::default_theme()
            .palette(true)
            .for_depth(depth)
            .plan_border();
        assert!(
            matches!(border, Color::Rgb(r, g, b) if r == g && g == b),
            "{border:?} at {depth:?}"
        );
    }
}

#[test]
fn heading_levels_stay_apart() {
    use crate::color_depth::ColorDepth;
    for theme in theme::themes() {
        let palette = theme.palette(false);
        for depth in [ColorDepth::TrueColor, ColorDepth::Ansi256] {
            let shown = [
                palette.primary,
                palette.heading_1,
                palette.heading_2,
                palette.heading_3,
            ]
            .map(|color| depth.downconvert(color));
            for (index, colour) in shown.iter().enumerate() {
                assert!(
                    !shown[index + 1..].contains(colour),
                    "{} {depth:?}: {shown:?}",
                    theme.slug
                );
            }
        }
        // The levels a reply most often opens with stay off the speaker label's hue.
        for heading in [palette.heading_1, palette.heading_2] {
            let apart = hue_apart(heading, palette.primary);
            assert!(apart >= 30.0, "{} {heading:?}: {apart:.0}°", theme.slug);
        }
        assert_eq!(palette.semantic_heading(5).fg, Some(palette.muted));
    }
}

/// How far apart two colours' hues sit on the colour wheel, in degrees.
fn hue_apart(a: Color, b: Color) -> f64 {
    fn hue(color: Color) -> f64 {
        let Color::Rgb(r, g, b) = color else {
            panic!("expected RGB, got {color:?}");
        };
        let [r, g, b] = [r, g, b].map(|channel| f64::from(channel) / 255.0);
        let (max, min) = (r.max(g).max(b), r.min(g).min(b));
        let chroma = max - min;
        let sector = if chroma == 0.0 {
            0.0
        } else if max == r {
            ((g - b) / chroma).rem_euclid(6.0)
        } else if max == g {
            (b - r) / chroma + 2.0
        } else {
            (r - g) / chroma + 4.0
        };
        sector * 60.0
    }
    let apart = (hue(a) - hue(b)).abs();
    apart.min(360.0 - apart)
}

#[test]
fn run_mode_colours() {
    let palette = theme::default_theme().palette(false);
    assert_eq!(palette.run_mode("Plan").fg, Some(palette.warning));
    assert_eq!(palette.run_mode("Build").fg, Some(palette.accent));
    assert!(
        palette
            .run_mode("Build")
            .add_modifier
            .contains(Modifier::BOLD)
    );
}

#[test]
fn inline_code_reads_apart() {
    use crate::color_depth::ColorDepth;
    for theme in theme::themes() {
        // At 16 colours code must not turn the red that marks errors, after the same
        // contrast repair a drawn frame gets.
        // Replies draw on the background and the composer on the surface.
        let palette = theme.palette(false);
        for fill in [palette.background, palette.surface] {
            let mut cell = Buffer::empty(ratatui::layout::Rect::new(0, 0, 1, 1));
            cell[(0, 0)].set_fg(palette.code).set_bg(fill);
            ColorDepth::Ansi16.downconvert_buffer(&mut cell);
            let code = cell[(0, 0)].fg;
            assert!(
                !matches!(code, Color::Red | Color::LightRed),
                "{} {code:?}",
                theme.slug
            );
        }
        // Monochrome grays alone would not set code apart from bold text.
        let gray = theme.palette(true);
        assert_eq!(
            gray.semantic_inline_code().bg,
            Some(gray.panel),
            "{}",
            theme.slug
        );
    }
}

#[test]
fn chip_text_reads_everywhere() {
    for theme in crate::theme::themes() {
        let palette = theme.palette(false);
        let mut colors = Vec::new();
        for label in [
            "Read", "Search", "List", "Explore", "Bash", "Edit", "Write", "MCP", "Process",
        ] {
            let chip = palette.chip(label);
            let Some(fg) = chip.fg else {
                panic!("{} {label} chip has no colour", theme.name);
            };
            assert_eq!(chip.bg, None, "{} {label} chip has a fill", theme.name);
            assert!(
                crate::theme::contrast_ratio(fg, palette.background) >= 4.5,
                "{} {label} chip contrast",
                theme.name
            );
            assert!(
                !colors.contains(&fg),
                "{} {label} repeats a chip color",
                theme.name
            );
            colors.push(fg);
            assert!(chip.add_modifier.contains(Modifier::BOLD));
            assert!(!chip.add_modifier.contains(Modifier::UNDERLINED));
        }
    }
}

#[test]
fn no_color_chip_bolds() {
    for theme in crate::theme::themes() {
        let chip = theme.palette(true).chip("Read");
        assert_eq!((chip.fg, chip.bg), (None, None), "{}", theme.name);
        assert!(chip.add_modifier.contains(Modifier::BOLD));
        assert!(!chip.add_modifier.contains(Modifier::REVERSED));
    }
}

#[test]
fn preferences_round_trip_dark_history_and_preserve_unrelated_fields() {
    let home = TestHome::new();
    let store = home.store();
    assert_eq!(store.load().active.name, "wisp");
    home.write(
        br#"{"unrelated":{"keep":[1,2,3]},"theme":"wisp-wave","last_dark_theme":"wisp-wave"}"#,
    );
    let mut selection = store.load();
    selection.select(theme::resolve("dawn").unwrap());
    store.save(selection).unwrap();
    assert_eq!(home.document()["unrelated"], json!({"keep":[1,2,3]}));
    assert_eq!(home.document()["theme"], "wisp-dawn");
    assert_eq!(home.document()["last_dark_theme"], "wisp-wave");
    let mut reloaded = store.load();
    reloaded.toggle();
    assert_eq!(reloaded.active.slug, "wave");
    reloaded.toggle();
    assert_eq!(reloaded.active.slug, "paper");
    assert_eq!(
        fs::read_dir(home.path().parent().unwrap()).unwrap().count(),
        1,
        "no staged file remains"
    );
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        fs::metadata(home.path()).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[test]
fn startup_logo_round_trips_without_changing_theme_or_unowned_values() {
    let home = TestHome::new();
    let store = home.store();
    assert_eq!(store.load_logo().name(), "random");
    home.write(
        br#"{"theme":"wisp-wave","last_dark_theme":"wisp-wave","unrelated":{"large":18446744073709551617}}"#,
    );
    let choice = LogoChoice::resolve("adal-mark-braille").unwrap();
    store.save_logo(choice).unwrap();
    assert_eq!(store.load_logo().name(), "adal-mark-braille");
    assert_eq!(store.load().active.slug, "wave");
    assert_eq!(store.load().last_dark.slug, "wave");
    assert!(
        fs::read_to_string(home.path())
            .unwrap()
            .contains("18446744073709551617")
    );

    let mut theme = store.load();
    theme.select(theme::resolve("paper").unwrap());
    store.save(theme).unwrap();
    assert_eq!(store.load_logo().name(), "adal-mark-braille");
    assert_eq!(store.load().active.slug, "paper");
    assert_eq!(
        fs::read_dir(home.path().parent().unwrap()).unwrap().count(),
        1,
        "no staged file remains"
    );
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(
        fs::metadata(home.path()).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[test]
fn invalid_logo_choices_default_to_random_and_unreadable_preferences_are_preserved() {
    let home = TestHome::new();
    for contents in [
        br#"{"startup_logo":"unknown"}"#.as_slice(),
        br#"{"startup_logo":42}"#,
        br#"{"startup_logo":null}"#,
        br#"{bad json"#,
    ] {
        home.write(contents);
        assert_eq!(home.store().load_logo().name(), "random");
    }
    for contents in [vec![0xff, 0xfe], vec![b' '; 70 * 1024]] {
        home.write(&contents);
        assert_eq!(home.store().load_logo().name(), "random");
        assert!(home.store().save_logo(LogoChoice::Random).is_err());
        assert_eq!(fs::read(home.path()).unwrap(), contents);
    }
}

#[test]
fn theme_changes_preserve_unrelated_numeric_tokens_losslessly() {
    let home = TestHome::new();
    for number in [
        "18446744073709551617",
        "-9223372036854775809",
        "0.12345678901234567890123456789",
        "1e+400",
    ] {
        home.write(
            format!(
                r#"{{"theme":"\u0077isp-wave","unrelated":{{"nested":[{number},{{"value":{number}}}]}}}}"#
            )
            .as_bytes(),
        );
        let store = home.store();
        let mut selection = store.load();
        assert_eq!(selection.active.slug, "wave");
        selection.toggle();
        store.save(selection).unwrap();
        let saved = fs::read_to_string(home.path()).unwrap();
        assert_eq!(
            saved.matches(number).count(),
            2,
            "rewrote {number}: {saved}"
        );
        assert_eq!(store.load().active.slug, "paper");
    }
}

#[test]
fn unusable_preferences_fall_back_and_unreadable_bytes_are_not_overwritten() {
    let home = TestHome::new();
    for contents in [
        r#"{"theme":"dracula"}"#,
        r#"{"theme":42}"#,
        "[]",
        "{bad json",
        "",
    ] {
        home.write(contents.as_bytes());
        assert_eq!(home.store().load().active.slug, "vapor");
        home.store().save(ThemeSelection::default()).unwrap();
        assert_eq!(home.document()["theme"], "wisp");
    }
    for contents in [vec![0xff, 0xfe], vec![b' '; 70 * 1024]] {
        home.write(&contents);
        assert_eq!(home.store().load().active.slug, "vapor");
        assert!(home.store().save(ThemeSelection::default()).is_err());
        assert_eq!(fs::read(home.path()).unwrap(), contents);
    }
    home.write(br#"{"theme":"wisp-orchid","last_dark_theme":"wisp-light"}"#);
    assert_eq!(home.store().load().last_dark.slug, "orchid");
    home.write(br#"{"theme":"wisp-dawn","last_dark_theme":"invalid"}"#);
    assert_eq!(home.store().load().last_dark.slug, "vapor");
}

#[test]
fn nonregular_preference_files_fail_without_waiting_for_a_writer() {
    let home = TestHome::new();
    fs::create_dir_all(home.path().parent().unwrap()).unwrap();
    assert!(
        std::process::Command::new("mkfifo")
            .arg(home.path())
            .status()
            .unwrap()
            .success()
    );
    assert_eq!(home.store().load().active.slug, "vapor");
    assert!(home.store().save(ThemeSelection::default()).is_err());
    use std::os::unix::fs::FileTypeExt;
    assert!(
        fs::symlink_metadata(home.path())
            .unwrap()
            .file_type()
            .is_fifo()
    );
}

#[tokio::test]
async fn preview_cancel_and_toggle_do_not_commit_or_disturb_draft_or_viewport() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.editor.insert_paste("keep this\ndraft 👩‍💻");
    ui.state.transcript.start_message(1);
    ui.state
        .transcript
        .append_message_delta(1, &"**background** `code`\n".repeat(100));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::PageUp), &writer, 8192)
        .await
        .unwrap();
    let before = draw(&mut ui, 80, 24);
    let viewport = ui.transcript_viewport.clone();
    let editor = ui.editor.clone();
    let generation = ui.state.transcript.generation();
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Down), &writer, 8192)
        .await
        .unwrap();
    let preview = draw(&mut ui, 80, 24);
    assert_ne!(preview, before);
    assert_eq!(
        ui.palette(),
        theme::resolve("glass").unwrap().palette(false)
    );
    ui.handle_input(ctrl('t'), &writer, 8192).await.unwrap();
    assert_eq!(ui.theme.active.slug, "vapor");
    assert!(!home.path().exists(), "preview and Ctrl-T must not persist");
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(draw(&mut ui, 80, 24), before);
    assert_eq!(ui.transcript_viewport, viewport);
    assert_eq!(ui.editor, editor);
    assert_eq!(ui.state.transcript.generation(), generation);
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn command_and_picker_commit_without_rpc_and_selection_survives_reload() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.editor.insert_paste("/theme wave");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.theme.active.slug, "wave");
    assert_eq!(home.document()["theme"], "wisp-wave");
    assert!(ui.editor.text().is_empty());
    ui.handle_input(ctrl('t'), &writer, 8192).await.unwrap();
    assert_eq!(ui.theme.active.slug, "paper");
    ui.handle_input(ctrl('t'), &writer, 8192).await.unwrap();
    assert_eq!(ui.theme.active.slug, "wave");
    ui.editor.insert_paste("/theme");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    draw(&mut ui, 30, 8);
    ui.handle_input(key(KeyCode::End), &writer, 8192)
        .await
        .unwrap();
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.theme.active.slug, "wave", "unpainted selection");
    assert!(text(&draw(&mut ui, 30, 8)).contains("Dawn"));
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(home.store().load().active.slug, "dawn");
    assert_eq!(home.store().load().last_dark.slug, "wave");
    assert!(ui.theme_picker.is_none());
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn invalid_commands_stay_local_but_multiline_theme_text_remains_a_prompt() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    for command in ["/theme invalid", "/theme paper extra"] {
        ui.editor.clear();
        ui.editor.insert_paste(command);
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.editor.text(), command);
        assert_eq!(ui.theme.active.slug, "vapor");
        assert!(receiver.try_recv().is_err());
    }
    ui.editor.clear();
    let prompt = "/theme paper\nexplain this command";
    ui.editor.insert_paste(prompt);
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    let WriterMessage::Frame { payload, .. } = receiver.try_recv().unwrap() else {
        panic!("prompt")
    };
    assert_eq!(
        serde_json::from_slice::<Value>(&payload).unwrap()["prompt"],
        prompt
    );
    ui.editor.insert_paste("/theme paper");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.theme.active.slug, "paper",
        "theme remains local during a run"
    );
    assert!(receiver.try_recv().is_err(), "must not turn into steering");
}

#[tokio::test]
async fn save_failure_keeps_live_theme_and_existing_preferences_with_a_visible_warning() {
    let home = TestHome::new();
    home.write(&[0xff, 0xfe]);
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.editor.insert_paste("/theme paper");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.theme.active.slug, "paper");
    assert!(ui.notice.as_ref().unwrap().contains("could not save"));
    assert_eq!(fs::read(home.path()).unwrap(), [0xff, 0xfe]);
    assert!(text(&draw(&mut ui, 80, 24)).contains("could not save"));
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn trust_and_new_workflow_displacement_roll_back_uncommitted_preview() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    for trust in [true, false] {
        let mut ui = ui(&home);
        ui.editor.insert_paste("draft");
        ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
        draw(&mut ui, 80, 24);
        ui.handle_input(key(KeyCode::End), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.palette(), theme::resolve("dawn").unwrap().palette(false));
        if trust {
            ui.dispatch(
                UiAction::BackendEvent(BackendEvent::TrustRequested {
                    request_id: "trust".into(),
                    project_path: "/project".into(),
                }),
                &writer,
                8192,
            )
            .await
            .unwrap();
        } else {
            ui.apply_effects(
                vec![UiEffect::ShowConnectionPanel(
                    ui.state.connection_catalog.clone(),
                )],
                &writer,
                8192,
            )
            .await
            .unwrap();
        }
        assert!(ui.theme_picker.is_none());
        assert_eq!(ui.palette(), theme::default_theme().palette(false));
        ui.handle_input(key(KeyCode::Enter), &writer, 8192)
            .await
            .unwrap();
        assert_eq!(ui.theme.active.slug, "vapor");
        assert_eq!(ui.editor.text(), "draft");
        assert!(!home.path().exists());
        assert!(receiver.try_recv().is_err());
    }
}

#[tokio::test]
async fn theme_toggles_preserve_unsendable_approval_trust_and_cancellation_notices() {
    for kind in ["approval", "trust", "cancelling"] {
        let home = TestHome::new();
        let (writer, mut receiver) = mpsc::channel(16);
        let mut ui = ui(&home);
        ui.state.view_status = ViewStatus::Running;
        ui.state.current_command = Some(reducer::ActiveCommand {
            id: "prompt-1".into(),
            command_type: reducer::ActiveCommandType::Prompt,
        });
        ui.state.cancel_requested = kind == "cancelling";
        let event = if kind == "trust" {
            BackendEvent::TrustRequested {
                request_id: "trust-1".into(),
                project_path: "/project".into(),
            }
        } else {
            BackendEvent::ToolApprovalRequested(PendingApproval {
                call_id: "call-1".into(),
                name: "bash".into(),
                arguments: json!({}),
                detail_source: reducer::ToolDetailSource::None,
                safety: "command".into(),
                grants_allowed: true,
                session_only_yolo: false,
            })
        };
        ui.dispatch(UiAction::BackendEvent(event), &writer, 1)
            .await
            .unwrap();
        if kind != "cancelling" {
            ui.handle_input(key(KeyCode::Esc), &writer, 1)
                .await
                .unwrap();
        }
        assert!(ui.unsendable_current_response(), "{kind}");
        let notice = ui.notice.clone();
        assert!(notice.as_ref().unwrap().contains("Esc/Ctrl-C"));
        ui.handle_input(ctrl('t'), &writer, 1).await.unwrap();
        assert_eq!(ui.theme.active.slug, "paper");
        assert_eq!(ui.notice, notice, "{kind}");
        assert!(ui.unsendable_current_response());
        assert!(text(&draw(&mut ui, 100, 24)).contains("Esc/Ctrl-C"));
        assert_eq!(
            ui.handle_input(key(KeyCode::Esc), &writer, 1)
                .await
                .unwrap(),
            LoopControl::Exit
        );
        assert!(receiver.try_recv().is_err());
    }
}

#[tokio::test]
async fn opening_and_cancelling_preview_preserves_the_latest_runtime_notice() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.notice = Some("Queued work could not be restored.".into());
    ui.editor.insert_paste("/theme");
    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(
        ui.notice.as_deref(),
        Some("Queued work could not be restored.")
    );
    draw(&mut ui, 80, 24);
    ui.apply_effects(
        vec![UiEffect::Notice("A newer backend warning.".into())],
        &writer,
        8192,
    )
    .await
    .unwrap();
    ui.handle_input(key(KeyCode::Esc), &writer, 8192)
        .await
        .unwrap();
    assert_eq!(ui.notice.as_deref(), Some("A newer backend warning."));
    assert!(!home.path().exists());
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn buffered_workflow_displaces_theme_before_enter_can_commit() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.editor.insert_paste("keep this draft");
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Down), &writer, 8192)
        .await
        .unwrap();
    draw(&mut ui, 80, 24);
    let (sender, mut events) = mpsc::channel(16);
    sender
        .send(QueuedEvent {
            event: BackendEvent::TrustRequested {
                request_id: "new-trust".into(),
                project_path: "/project".into(),
            },
            _wire_bytes: Arc::new(Semaphore::new(1)).acquire_owned().await.unwrap(),
        })
        .await
        .unwrap();
    ui.handle_received_input(key(KeyCode::Enter), &mut events, &writer, 8192)
        .await
        .unwrap();
    assert!(ui.theme_picker.is_none());
    assert_eq!(ui.theme.active.slug, "vapor");
    assert_eq!(ui.editor.text(), "keep this draft");
    assert!(!home.path().exists());
    assert!(
        receiver.try_recv().is_err(),
        "Enter belongs to the displaced theme, not trust or draft"
    );
}

#[tokio::test]
async fn stale_device_detail_and_tree_results_do_not_displace_a_theme_preview() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    let stale_detail = ui.state.transcript.append_prompt("old".into());
    ui.theme_picker = Some(ThemePicker::new(ui.theme.active));
    draw(&mut ui, 80, 24);
    ui.handle_input(key(KeyCode::Down), &writer, 8192)
        .await
        .unwrap();
    let preview = ui.palette();
    for effect in [
        UiEffect::ShowDeviceCode(wisp_protocol::events::DeviceCodeChallenge {
            provider: "old-provider".into(),
            verification_uri: "https://example.test".into(),
            user_code: "OLD-CODE".into(),
        }),
        UiEffect::OpenExactDetail(stale_detail),
        UiEffect::ShowSessionTreePage {
            append: true,
            page: reducer::SessionTreePage {
                session: None,
                active_leaf_id: None,
                total_node_count: 0,
                nodes: vec![],
                truncated: false,
                next_after_entry_id: None,
            },
        },
    ] {
        ui.apply_effects(vec![effect], &writer, 8192).await.unwrap();
        assert!(ui.theme_picker.is_some());
        assert_eq!(ui.palette(), preview);
    }
    let mut panel = ConnectionPanel::new(ui.state.connection_catalog.clone());
    panel.begin_device_code("new-provider".into());
    ui.connection_panel = Some(panel);
    ui.apply_effects(
        vec![UiEffect::ShowDeviceCode(
            wisp_protocol::events::DeviceCodeChallenge {
                provider: "old-provider".into(),
                verification_uri: "https://example.test".into(),
                user_code: "OLD".into(),
            },
        )],
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(
        ui.theme_picker.is_some(),
        "wrong provider challenge is not presentation"
    );
    assert!(!home.path().exists());
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn recoloring_during_streaming_preserves_semantic_caches_and_monochrome_is_consistent() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.state.transcript.start_message(1);
    ui.state.transcript.append_message_delta(
        1,
        "# Heading\n```rust\nfn main() { let text = \"hello\"; }\n```\n",
    );
    let generation = ui.state.transcript.generation();
    for selected in theme::themes() {
        ui.theme.select(selected);
        for no_color in [false, true] {
            ui.no_color = no_color;
            let frame = draw(&mut ui, 80, 24);
            assert!(text(&frame).contains("Heading"));
            assert_eq!(
                frame[(79, 0)].bg,
                selected.palette(no_color).base().bg.unwrap_or(Color::Reset)
            );
            if no_color {
                for cell in &frame.content {
                    for color in [cell.fg, cell.bg] {
                        let Color::Rgb(r, g, b) = color else {
                            assert_eq!(color, Color::Reset);
                            continue;
                        };
                        assert_eq!(r, g);
                        assert_eq!(g, b);
                    }
                }
            }
        }
    }
    assert_eq!(ui.state.transcript.generation(), generation);
    ui.dispatch(
        UiAction::BackendEvent(BackendEvent::MessageDelta {
            turn: 1,
            delta: "stream continues".into(),
            content_kind: reducer::MessageContentKind::Text,
        }),
        &writer,
        8192,
    )
    .await
    .unwrap();
    assert!(text(&draw(&mut ui, 80, 24)).contains("stream continues"));
    assert!(receiver.try_recv().is_err());
}

#[tokio::test]
async fn logo_command_applies_immediately_and_persists_without_rpc() {
    let home = TestHome::new();
    let (writer, mut receiver) = mpsc::channel(16);
    let mut ui = ui(&home);
    ui.editor.insert_paste("/logo adal-mark-braille");

    ui.handle_input(key(KeyCode::Enter), &writer, 8192)
        .await
        .unwrap();

    assert_eq!(ui.logo_choice, LogoChoice::AdalMarkBraille);
    assert_eq!(ui.startup_logo, LogoChoice::AdalMarkBraille);
    assert_eq!(home.store().load_logo(), LogoChoice::AdalMarkBraille);
    assert_eq!(
        ui.notice.as_deref(),
        Some("Startup logo: Adal Mark Braille")
    );
    assert!(receiver.try_recv().is_err());
}

#[test]
fn random_logo_is_resolved_once_and_redraws_do_not_change_it() {
    let home = TestHome::new();
    let mut ui = ui(&home);
    ui.apply_logo_choice(LogoChoice::Random);
    let resolved = ui.startup_logo;

    draw(&mut ui, 80, 24);
    draw(&mut ui, 42, 14);
    ui.apply_logo_choice(LogoChoice::Random);

    assert_eq!(ui.logo_choice, LogoChoice::Random);
    assert_eq!(ui.startup_logo, resolved);
    assert_ne!(resolved, LogoChoice::Random);
    assert_eq!(home.store().load_logo(), LogoChoice::Random);
}

#[test]
fn every_theme_keeps_diff_text_and_changed_words_readable() {
    for theme in theme::themes() {
        for monochrome in [false, true] {
            let palette = theme.palette(monochrome);
            for background in [palette.addition_background, palette.deletion_background] {
                let base = Style::default().fg(palette.foreground).bg(background);
                let emphasis = palette.diff_change(base);
                assert!(
                    theme::contrast_ratio(emphasis.fg.unwrap(), emphasis.bg.unwrap()) >= 4.5,
                    "{} changed words",
                    theme.name
                );
                for class in [
                    crate::syntax::SyntaxClass::Keyword,
                    crate::syntax::SyntaxClass::String,
                    crate::syntax::SyntaxClass::Number,
                    crate::syntax::SyntaxClass::Comment,
                ] {
                    let style = palette.syntax(base, class);
                    assert!(
                        theme::contrast_ratio(style.fg.unwrap(), background) >= 4.5,
                        "{} {class:?}",
                        theme.name
                    );
                }
                assert!(
                    emphasis
                        .add_modifier
                        .contains(Modifier::BOLD | Modifier::UNDERLINED)
                );
            }
        }
    }
}

#[test]
fn breathing_cycle() {
    let palette = theme::default_theme().palette(false);
    let warning = palette.warning;
    // A still frame is the solid colour, so captures and snapshots do not move.
    assert_eq!(palette.breathing(warning, 0), warning);
    assert_eq!(palette.breathing(warning, 16), warning);
    assert_eq!(
        palette.breathing(warning, 255),
        palette.breathing(warning, 15)
    );
    // Halfway through the cycle it is at its quietest, still distinct from the surface.
    let quietest = palette.breathing(warning, 8);
    assert_ne!(quietest, warning);
    assert_ne!(quietest, palette.surface);
    assert_eq!(
        palette.breathing(warning, 4),
        palette.breathing(warning, 12)
    );
    let distance = |color| match (color, palette.surface) {
        (Color::Rgb(r, g, b), Color::Rgb(sr, sg, sb)) => {
            u32::from(r.abs_diff(sr)) + u32::from(g.abs_diff(sg)) + u32::from(b.abs_diff(sb))
        }
        _ => panic!("RGB palette"),
    };
    assert!(distance(palette.breathing(warning, 4)) > distance(quietest));
    assert!(distance(warning) > distance(palette.breathing(warning, 4)));
}

#[test]
fn breathe_by_motion() {
    use theme::MotionMode::{Smooth, Stepped, Still};
    let palette = theme::default_theme().palette(false);
    let style = Style::default().fg(palette.secondary);
    // Smooth motion blends the colour toward the background and back.
    assert_eq!(palette.breathe(style, Smooth, 0), style);
    let quietest = palette.breathe(style, Smooth, 8);
    assert_ne!(quietest.fg, style.fg);
    assert_eq!(quietest.add_modifier, Modifier::empty());
    // Still motion never changes the style.
    for frame in [0, 4, 8, 12] {
        assert_eq!(palette.breathe(style, Still, frame), style);
    }
    // Stepped motion keeps the colour and steps the weight: bold at the peak, plain
    // between, dim at the trough.
    let stepped = [0, 4, 8].map(|frame| palette.breathe(style, Stepped, frame));
    assert!(stepped.iter().all(|stepped| stepped.fg == style.fg));
    assert_eq!(stepped[0], style.add_modifier(Modifier::BOLD));
    assert_eq!(stepped[1], style);
    assert_eq!(stepped[2], style.add_modifier(Modifier::DIM));
    // A theme on the terminal's own background has no backdrop to blend toward.
    let glass = theme::resolve("glass").unwrap().palette(false);
    let style = Style::default().fg(glass.secondary);
    assert_eq!(
        glass.breathe(style, Smooth, 8),
        style.add_modifier(Modifier::DIM)
    );
    assert_eq!(glass.finish_glow(Smooth, 0), None);
}

#[test]
fn finish_glow_fades() {
    use theme::{FINISH_GLOW_FRAMES, MotionMode};
    let palette = theme::default_theme().palette(false);
    let glows: Vec<_> = (0..FINISH_GLOW_FRAMES)
        .map(|age| palette.finish_glow(MotionMode::Smooth, age).unwrap())
        .collect();
    let distance = |color| match (color, palette.background) {
        (Color::Rgb(r, g, b), Color::Rgb(br, bg, bb)) => {
            u32::from(r.abs_diff(br)) + u32::from(g.abs_diff(bg)) + u32::from(b.abs_diff(bb))
        }
        _ => panic!("RGB palette"),
    };
    // Each frame is closer to the background than the last, and then it is gone.
    assert!(
        glows
            .windows(2)
            .all(|pair| distance(pair[0]) > distance(pair[1]))
    );
    assert_eq!(
        palette.finish_glow(MotionMode::Smooth, FINISH_GLOW_FRAMES),
        None
    );
    assert_eq!(palette.finish_glow(MotionMode::Stepped, 0), None);
    assert_eq!(palette.finish_glow(MotionMode::Still, 0), None);
}
