//! Live browser viewport pane integration, rendering, occlusion, and protocol tests.

use super::*;
use crate::side_panel::PanelMode;
use image::ImageEncoder;
use ratatui::buffer::CellDiffOption;
use ratatui::layout::Rect;
use sha2::Digest;

struct TempSessionDir(std::path::PathBuf);

impl TempSessionDir {
    fn new(tag: &str) -> Self {
        let dir = std::env::temp_dir().join(format!(
            "wisp-browser-viewport-{tag}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("images")).unwrap();
        Self(dir)
    }

    fn session_path(&self) -> String {
        self.0.join("session.jsonl").display().to_string()
    }

    fn images_dir(&self) -> std::path::PathBuf {
        self.0.join("images")
    }
}

impl Drop for TempSessionDir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

struct RecordingBackend {
    inner: ratatui::backend::TestBackend,
    events: Vec<String>,
    drawn_coords: Vec<(u16, u16)>,
}

impl RecordingBackend {
    fn new(width: u16, height: u16) -> Self {
        Self {
            inner: ratatui::backend::TestBackend::new(width, height),
            events: Vec::new(),
            drawn_coords: Vec::new(),
        }
    }

    fn take_events(&mut self) -> Vec<String> {
        std::mem::take(&mut self.events)
    }

    fn take_drawn_coords(&mut self) -> Vec<(u16, u16)> {
        std::mem::take(&mut self.drawn_coords)
    }
}

impl ratatui::backend::Backend for RecordingBackend {
    type Error = std::convert::Infallible;

    fn draw<'a, I>(&mut self, content: I) -> Result<(), Self::Error>
    where
        I: Iterator<Item = (u16, u16, &'a ratatui::buffer::Cell)>,
    {
        let cells: Vec<_> = content.collect();
        self.drawn_coords
            .extend(cells.iter().map(|&(x, y, _)| (x, y)));
        self.inner.draw(cells.into_iter())
    }

    fn hide_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.hide_cursor()
    }

    fn show_cursor(&mut self) -> Result<(), Self::Error> {
        self.inner.show_cursor()
    }

    fn get_cursor_position(&mut self) -> Result<ratatui::layout::Position, Self::Error> {
        self.inner.get_cursor_position()
    }

    fn set_cursor_position<P: Into<ratatui::layout::Position>>(
        &mut self,
        position: P,
    ) -> Result<(), Self::Error> {
        self.inner.set_cursor_position(position)
    }

    fn clear(&mut self) -> Result<(), Self::Error> {
        self.inner.clear()
    }

    fn clear_region(&mut self, clear_type: ratatui::backend::ClearType) -> Result<(), Self::Error> {
        self.inner.clear_region(clear_type)
    }

    fn append_lines(&mut self, n: u16) -> Result<(), Self::Error> {
        self.inner.append_lines(n)
    }

    fn size(&self) -> Result<ratatui::layout::Size, Self::Error> {
        self.inner.size()
    }

    fn window_size(&mut self) -> Result<ratatui::backend::WindowSize, Self::Error> {
        self.inner.window_size()
    }

    fn flush(&mut self) -> Result<(), Self::Error> {
        self.events.push("flush".into());
        self.inner.flush()
    }
}

impl terminal::FrameBackend for RecordingBackend {
    fn begin_frame(&mut self) -> std::io::Result<()> {
        self.events.push("begin".into());
        Ok(())
    }

    fn end_frame(&mut self) -> std::io::Result<()> {
        self.events.push("end".into());
        Ok(())
    }

    fn into_io_error(error: std::convert::Infallible) -> std::io::Error {
        match error {}
    }

    fn write_control(&mut self, _bytes: &[u8]) -> std::io::Result<()> {
        Ok(())
    }

    fn write_frame_bytes(&mut self, bytes: &[u8]) -> std::io::Result<()> {
        self.events
            .push(format!("bytes:{}", String::from_utf8_lossy(bytes)));
        Ok(())
    }

    fn suspend(&mut self) -> std::io::Result<terminal::Suspended> {
        self.inner.suspend()
    }

    fn resume(&mut self, suspended: terminal::Suspended) -> std::io::Result<()> {
        self.inner.resume(suspended)
    }
}

fn conn() -> ConnectionInfo {
    ConnectionInfo {
        backend_version: "test".into(),
    }
}

fn write_split_tone_png(images_dir: &std::path::Path, width: u32, height: u32) -> (String, usize) {
    let mut rgba = Vec::with_capacity((width * height * 4) as usize);
    for y in 0..height {
        let color = if y < height / 2 {
            [220u8, 24, 24, 255]
        } else {
            [24u8, 60, 220, 255]
        };
        for _ in 0..width {
            rgba.extend_from_slice(&color);
        }
    }
    let mut png = Vec::new();
    image::codecs::png::PngEncoder::new(&mut png)
        .write_image(&rgba, width, height, image::ExtendedColorType::Rgba8)
        .unwrap();
    let digest = format!("{:x}", sha2::Sha256::digest(&png));
    let len = png.len();
    std::fs::write(images_dir.join(&digest), &png).unwrap();
    (digest, len)
}

async fn push_backend_event(ui: &mut LiveUi, payload: serde_json::Value) {
    let event = BackendEvent::from_projection_value(&payload).unwrap();
    let (writer, _receiver) = mpsc::channel(8);
    ui.dispatch(
        UiAction::BackendEvent(event),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
}

async fn run_command(ui: &mut LiveUi, command: Command) {
    let (writer, _receiver) = mpsc::channel(8);
    ui.handle_command(command, &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
}

async fn send_input(ui: &mut LiveUi, input: Input) {
    let (writer, _receiver) = mpsc::channel(8);
    ui.handle_input(input, &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
}

fn viewport_event(
    timestamp: &str,
    image: Option<(&str, usize, u32, u32)>,
    url: Option<&str>,
    title: Option<&str>,
    source_tool: Option<&str>,
) -> serde_json::Value {
    serde_json::json!({
        "type": "browser.viewport.updated",
        "timestamp": timestamp,
        "image": image.map(|(sha, byte_size, w, h)| serde_json::json!({
            "media_type": "image/png",
            "sha256": sha,
            "byte_size": byte_size,
            "width": w,
            "height": h,
        })),
        "url": url,
        "title": title,
        "viewport_width": 1280,
        "viewport_height": 720,
        "source_tool": source_tool,
        "call_id": "call-browser-1",
    })
}

fn buffer_text(buffer: &ratatui::buffer::Buffer) -> String {
    (0..buffer.area.height)
        .map(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn draw_text(ui: &mut LiveUi, width: u16, height: u16) -> (ratatui::buffer::Buffer, String) {
    let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
    ui.draw(&mut terminal, &conn()).unwrap();
    let buf = terminal.backend().buffer().clone();
    let text = buffer_text(&buf);
    (buf, text)
}

fn ui_with_session(dir: &TempSessionDir) -> LiveUi {
    let mut ui = LiveUi::default();
    ui.state.workspace_cwd = Some(dir.0.display().to_string());
    ui.state.selected_session = Some(reducer::SessionIdentity {
        session_id: "sess-browser".into(),
        session_path: dir.session_path(),
        session_name: Some("Browser Session".into()),
    });
    ui
}

#[tokio::test]
async fn docked_auto_switch_manual_tab_and_session_reset() {
    let dir = TempSessionDir::new("docked");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);
    let mut ui = ui_with_session(&dir);

    // Warm the docked width before the first browser event arrives.
    let (_buf, initial) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(!initial.contains("Work · Browser"));

    // First image-bearing frame auto-switches docked side panel to Browser.
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 256, 128)),
            Some("https://example.com/app"),
            Some("Example Page"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;

    let (buf, screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(screen.contains("Work · Browser"), "{screen}");
    assert!(screen.contains("Example Page"), "{screen}");
    assert!(screen.contains("12:34:56Z"), "{screen}");
    assert!(screen.contains("https://example.com/app"), "{screen}");
    assert!(screen.contains("1280×720 · playwright"), "{screen}");
    let panel_rect = ui.side_panel.split(buf.area).1;
    let has_pixels = (panel_rect.top()..panel_rect.bottom())
        .any(|y| (panel_rect.left()..panel_rect.right()).any(|x| buf[(x, y)].symbol() == "▀"));
    assert!(has_pixels, "expected half-block viewport in side panel");

    // Click the "Work" tab on the Work · Browser row inside the panel body to return to LiveWork.
    let work_pos = (panel_rect.top()..panel_rect.bottom())
        .find_map(|y| {
            let row_text: String = (panel_rect.left()..panel_rect.right())
                .map(|x| buf[(x, y)].symbol())
                .collect();
            row_text
                .find("Work · Browser")
                .map(|dx| (panel_rect.left() + dx as u16, y))
        })
        .expect("Work · Browser row in panel");
    assert_eq!(
        ui.side_panel.click(work_pos.0, work_pos.1),
        side_panel::OverlayKey::Moved
    );
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);

    // Subsequent frame updates respect the user's manual switch back to LiveWork.
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:35:00Z",
            Some((&sha, len, 256, 128)),
            Some("https://example.com/next"),
            Some("Second Page"),
            Some("mcp__playwright__browser_click"),
        ),
    )
    .await;
    let (_buf, live_screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(live_screen.contains("Work · Browser"), "{live_screen}");
    assert!(live_screen.contains("Browser Session"), "{live_screen}");

    // Workspace change clears browser state, returns to LiveWork, and re-arms first-frame auto-switch.
    let next_cwd = dir.0.join("other-repo");
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.workspace",
            "cwd": next_cwd.display().to_string(),
            "git_branch": "main",
        }),
    )
    .await;
    let (_buf, reset_screen) = draw_text(&mut ui, 160, 36);
    assert!(ui.state.browser_viewport.is_none());
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(!reset_screen.contains("Work · Browser"), "{reset_screen}");

    // Next session's first image frame auto-switches again.
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:36:00Z",
            Some((&sha, len, 256, 128)),
            Some("https://example.com/rearmed"),
            Some("Rearmed Page"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;
    let (_buf, rearmed_screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(rearmed_screen.contains("Rearmed Page"), "{rearmed_screen}");
}

#[tokio::test]
async fn pre_frame_browser_command_stays_open_and_toggles() {
    let dir = TempSessionDir::new("preframe");
    let mut ui = ui_with_session(&dir);
    let _ = draw_text(&mut ui, 160, 36);

    run_command(&mut ui, Command::Browser { interact: false }).await;
    let (_buf, screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(screen.contains("No browser frame yet."), "{screen}");

    // Second frame keeps Browser open rather than snapping back to LiveWork.
    let (_buf, second_screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(second_screen.contains("No browser frame yet."));

    // Running /browser again toggles back to LiveWork on a docked panel.
    run_command(&mut ui, Command::Browser { interact: false }).await;
    let (_buf, toggled_back) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(!toggled_back.contains("Work · Browser"));
}

#[tokio::test]
async fn narrow_terminal_does_not_auto_pop_overlay_and_browser_command_opens_without_occlusion() {
    let dir = TempSessionDir::new("narrow");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);
    let mut ui = ui_with_session(&dir);

    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 256, 128)),
            Some("https://narrow.test"),
            Some("Narrow Page"),
            Some("mcp__playwright__browser_snapshot"),
        ),
    )
    .await;

    // Narrow 100×28 terminal must not auto-pop SidePanel overlay.
    let (_buf, narrow) = draw_text(&mut ui, 100, 28);
    assert!(!ui.side_panel.overlay_open());
    assert!(!narrow.contains("Narrow Page"), "{narrow}");

    // `/browser` opens SidePanel overlay in Browser mode without self-occlusion (HalfBlock).
    run_command(&mut ui, Command::Browser { interact: false }).await;
    let (overlay_buf, overlay_screen) = draw_text(&mut ui, 100, 28);
    assert!(ui.side_panel.overlay_open());
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(overlay_screen.contains("Narrow Page"), "{overlay_screen}");
    assert!(
        overlay_buf.content.iter().any(|cell| cell.symbol() == "▀"),
        "expected unoccluded half-block preview inside SidePanel overlay"
    );

    // Focus inside narrow `/browser` starts on the "Browser" tab (Enter stays in Browser),
    // and Tab moves focus to "Work" where Enter switches back to LiveWork once painted.
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
    )
    .await;
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);

    let _ = draw_text(&mut ui, 100, 28);
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE)),
    )
    .await;
    let _ = draw_text(&mut ui, 100, 28);
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
    )
    .await;
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(ui.side_panel.overlay_open());

    // Switch back to Browser for the Kitty overlay check.
    run_command(&mut ui, Command::Browser { interact: false }).await;
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);

    // Switching to Kitty inside the SidePanel overlay emits native frame bytes (no self-occlusion).
    ui.image_protocol = terminal_images::ImageProtocol::Kitty;
    let mut terminal = Terminal::new(RecordingBackend::new(100, 28)).unwrap();
    ui.draw(&mut terminal, &conn()).unwrap();
    let events = terminal.backend_mut().take_events();
    let bytes = events
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected Kitty payload in narrow SidePanel overlay");
    assert!(bytes.contains("a=T,f=32,"), "{bytes}");

    // `b` switches tab inside overlay to LiveWork; `Esc` closes overlay.
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('b'), KeyModifiers::NONE)),
    )
    .await;
    assert_eq!(ui.side_panel.mode(), PanelMode::LiveWork);
    assert!(ui.side_panel.overlay_open());

    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
    )
    .await;
    assert!(!ui.side_panel.overlay_open());
}

#[tokio::test]
async fn hostile_browser_metadata_is_sanitized() {
    let dir = TempSessionDir::new("sanitize");
    let mut ui = ui_with_session(&dir);
    run_command(&mut ui, Command::Browser { interact: false }).await;

    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            None,
            Some("https://safe.test/\x1b]52;c;c2VjcmV0\x07path"),
            Some("Safe\x1b[31mTitle\u{202E}Here"),
            Some("mcp__play\x1b[1mwright__click"),
        ),
    )
    .await;

    let (_buf, screen) = draw_text(&mut ui, 160, 36);
    assert!(
        screen.contains("Safe\u{FFFD}[31mTitle\u{FFFD}Here"),
        "{screen}"
    );
    assert!(
        screen.contains("https://safe.test/\u{FFFD}]52;c;c2VjcmV0\u{FFFD}path"),
        "{screen}"
    );
    assert!(
        screen.contains("1280×720 · play\u{FFFD}[1mwright"),
        "{screen}"
    );
    assert!(
        screen.contains("Metadata updated (no screenshot returned)."),
        "{screen}"
    );
    assert!(!screen.contains('\x1b'));
    assert!(!screen.contains('\x07'));
    assert!(!screen.contains('\u{202E}'));
}

#[tokio::test]
async fn key_help_occludes_docked_and_f8_focused_kitty_browser_viewport() {
    let dir = TempSessionDir::new("occlusion");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 288, 160);
    let mut ui = ui_with_session(&dir);
    ui.image_protocol = terminal_images::ImageProtocol::Kitty;
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 288, 160)),
            Some("https://occlusion.test"),
            Some("Occlusion Test"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;

    // 148×36 ensures centered key_help popup (x=24..124) overlaps docked side panel (x=104..148).
    let mut terminal = Terminal::new(RecordingBackend::new(148, 36)).unwrap();
    ui.draw(&mut terminal, &conn()).unwrap();
    let f1 = terminal.backend_mut().take_events();
    assert!(
        f1.iter().any(|ev| ev.contains("a=T,f=32,")),
        "expected initial Kitty emit: {f1:?}"
    );

    // Open key_help over docked Browser panel -> deletes Kitty placement.
    ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
    ui.draw(&mut terminal, &conn()).unwrap();
    let f2 = terminal.backend_mut().take_events();
    let f2_bytes = f2
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected Kitty delete on key_help occlusion");
    assert!(f2_bytes.contains("a=d,d=i,"), "{f2_bytes}");
    assert!(!f2_bytes.contains("a=T,"), "{f2_bytes}");

    // Dismiss key_help -> re-emits Kitty placement.
    ui.key_help = None;
    ui.draw(&mut terminal, &conn()).unwrap();
    let f3 = terminal.backend_mut().take_events();
    assert!(f3.iter().any(|ev| ev.contains("a=T,f=32,")), "{f3:?}");

    // Focus the docked panel (`F8` style: docked() && overlay_open()) -> keeps placement active,
    // then opening key_help still occludes and deletes it cleanly.
    ui.side_panel.focus();
    ui.draw(&mut terminal, &conn()).unwrap();
    let _f4 = terminal.backend_mut().take_events();
    assert!(ui.image_placements.has_active_kitty());

    ui.key_help = Some(crate::key_help::KeyHelp::new(ui.key_help_owner()));
    ui.draw(&mut terminal, &conn()).unwrap();
    let f5 = terminal.backend_mut().take_events();
    let f5_bytes = f5
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected Kitty delete when key_help covers F8-focused panel");
    assert!(f5_bytes.contains("a=d,d=i,"), "{f5_bytes}");
    assert!(!f5_bytes.contains("a=T,"), "{f5_bytes}");
}

#[tokio::test]
async fn native_protocols_use_cell_diff_option_and_clear_on_tab_switch() {
    let dir = TempSessionDir::new("diff-option");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);

    for (protocol, needle) in [
        (terminal_images::ImageProtocol::Kitty, "a=T,f=32,"),
        (
            terminal_images::ImageProtocol::ITerm2,
            "\x1b]1337;File=inline=1;",
        ),
        (terminal_images::ImageProtocol::Sixel, "\x1bP0;1q"),
    ] {
        let mut ui = ui_with_session(&dir);
        ui.image_protocol = protocol;
        push_backend_event(
            &mut ui,
            viewport_event(
                "2026-11-20T12:34:56Z",
                Some((&sha, len, 256, 128)),
                Some("https://diff.test"),
                Some("Diff Option Test"),
                Some("mcp__playwright__browser_navigate"),
            ),
        )
        .await;

        let mut terminal = Terminal::new(RecordingBackend::new(160, 36)).unwrap();

        // Frame 1: native bytes emitted, AlwaysUpdate cells, zero Modifier::HIDDEN.
        ui.draw(&mut terminal, &conn()).unwrap();
        let f1 = terminal.backend_mut().take_events();
        let _ = terminal.backend_mut().take_drawn_coords();
        let f1_bytes = f1
            .iter()
            .find_map(|ev| ev.strip_prefix("bytes:"))
            .unwrap_or_else(|| panic!("expected native payload for {protocol:?}"));
        assert!(f1_bytes.contains(needle), "{f1_bytes}");
        let buf1 = terminal.backend().inner.buffer();
        let mut image_coords = Vec::new();
        for y in 0..buf1.area.height {
            for x in 0..buf1.area.width {
                if buf1[(x, y)].diff_option == CellDiffOption::AlwaysUpdate {
                    image_coords.push((x, y));
                }
            }
        }
        assert!(
            !image_coords.is_empty(),
            "expected AlwaysUpdate on first frame for {protocol:?}"
        );
        assert!(
            !buf1
                .content
                .iter()
                .any(|c| c.modifier.contains(ratatui::style::Modifier::HIDDEN)),
            "unexpected Modifier::HIDDEN for {protocol:?}"
        );

        // Frame 2 (unchanged): emits zero graphics bytes and skips every image cell in diff.
        ui.draw(&mut terminal, &conn()).unwrap();
        let f2 = terminal.backend_mut().take_events();
        let f2_drawn = terminal.backend_mut().take_drawn_coords();
        assert!(
            !f2.iter().any(|ev| ev.starts_with("bytes:")),
            "unexpected bytes on steady frame for {protocol:?}: {f2:?}"
        );
        for &(x, y) in &image_coords {
            assert!(
                !f2_drawn.contains(&(x, y)),
                "steady frame should skip drawing image cell ({x}, {y}) for {protocol:?}"
            );
        }

        // Switching to LiveWork reverts every cell back to CellDiffOption::None and deletes Kitty.
        ui.side_panel.toggle_browser();
        ui.draw(&mut terminal, &conn()).unwrap();
        let f3 = terminal.backend_mut().take_events();
        let f3_drawn = terminal.backend_mut().take_drawn_coords();
        if protocol == terminal_images::ImageProtocol::Kitty {
            let f3_bytes = f3
                .iter()
                .find_map(|ev| ev.strip_prefix("bytes:"))
                .expect("expected Kitty delete when switching back to Work");
            assert!(f3_bytes.contains("a=d,d=i,"), "{f3_bytes}");
        }
        for &(x, y) in &image_coords {
            assert!(
                f3_drawn.contains(&(x, y)),
                "switching back to LiveWork should redraw previously skipped cell ({x}, {y}) for {protocol:?}"
            );
        }
        let buf3 = terminal.backend().inner.buffer();
        assert!(
            buf3.content
                .iter()
                .all(|c| c.diff_option == CellDiffOption::None),
            "expected all cells reverted to CellDiffOption::None for {protocol:?}"
        );
    }
}

#[tokio::test]
async fn border_resize_updates_geometry_and_corrupt_image_shows_fallback() {
    let dir = TempSessionDir::new("resize-corrupt");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 640, 320);
    let mut ui = ui_with_session(&dir);
    ui.image_protocol = terminal_images::ImageProtocol::Kitty;
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 640, 320)),
            Some("https://resize.test"),
            Some("Resize Page"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;

    let always_update_bounds = |buf: &ratatui::buffer::Buffer| -> (u16, u16, u16, u16) {
        let mut min_x = u16::MAX;
        let mut max_x = 0;
        let mut min_y = u16::MAX;
        let mut max_y = 0;
        for y in 0..buf.area.height {
            for x in 0..buf.area.width {
                if buf[(x, y)].diff_option == CellDiffOption::AlwaysUpdate {
                    min_x = min_x.min(x);
                    max_x = max_x.max(x);
                    min_y = min_y.min(y);
                    max_y = max_y.max(y);
                }
            }
        }
        assert!(
            min_x <= max_x && min_y <= max_y,
            "no AlwaysUpdate image bbox"
        );
        (min_x, max_x - min_x + 1, min_y, max_y - min_y + 1)
    };

    let mut terminal = Terminal::new(RecordingBackend::new(180, 40)).unwrap();
    ui.draw(&mut terminal, &conn()).unwrap();
    let initial_width = ui.side_panel.split(Rect::new(0, 0, 180, 40)).1.width;
    let initial_bounds = always_update_bounds(terminal.backend().inner.buffer());
    let _ = terminal.backend_mut().take_events();

    // Widen docked side panel via border drag (from column 128 leftwards to 108).
    let now = std::time::Instant::now();
    ui.side_panel.press_border(now, 0);
    let border_col = 180 - initial_width;
    ui.side_panel.continue_resize(
        crossterm::event::MouseEventKind::Drag(crossterm::event::MouseButton::Left),
        border_col - 20,
        now,
    );
    ui.side_panel.continue_resize(
        crossterm::event::MouseEventKind::Up(crossterm::event::MouseButton::Left),
        border_col - 20,
        now,
    );

    ui.draw(&mut terminal, &conn()).unwrap();
    let widened_width = ui.side_panel.split(Rect::new(0, 0, 180, 40)).1.width;
    assert!(
        widened_width > initial_width,
        "{widened_width} vs {initial_width}"
    );
    let widened_bounds = always_update_bounds(terminal.backend().inner.buffer());
    assert!(
        widened_bounds.1 > initial_bounds.1,
        "{widened_bounds:?} vs {initial_bounds:?}"
    );
    let f2 = terminal.backend_mut().take_events();
    let f2_bytes = f2
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected a Kitty placement update on border resize");
    assert!(
        f2_bytes.contains("a=p,") && !f2_bytes.contains("a=T,"),
        "{f2_bytes}"
    );

    // Corrupt/missing stored image falls back cleanly while preserving header metadata.
    let _ = std::fs::remove_file(dir.images_dir().join(&sha));
    ui.image_cache = terminal_images::ImageCache::default();
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:35:10Z",
            Some((
                "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                64,
                1280,
                720,
            )),
            Some("https://broken.test"),
            Some("Broken Page"),
            Some("mcp__playwright__browser_snapshot"),
        ),
    )
    .await;
    let (_buf, broken_screen) = draw_text(&mut ui, 180, 40);
    assert!(broken_screen.contains("Broken Page"), "{broken_screen}");
    assert!(
        broken_screen.contains("https://broken.test"),
        "{broken_screen}"
    );
    assert!(
        broken_screen.contains("Preview unavailable."),
        "{broken_screen}"
    );
}

#[tokio::test]
async fn fresh_and_new_sessions_resolve_frames_from_workspace_images_dir() {
    let dir = TempSessionDir::new("fresh-workspace-dir");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);
    let mut ui = LiveUi::default();
    assert!(ui.state.selected_session.is_none());

    // Startup `rpc.workspace` reports the store's shared `images` directory before any session is persisted.
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.workspace",
            "cwd": dir.0.display().to_string(),
            "git_branch": null,
            "session_images_dir": dir.images_dir().display().to_string(),
        }),
    )
    .await;

    let _ = draw_text(&mut ui, 160, 36);
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 256, 128)),
            Some("https://fresh.test"),
            Some("Fresh Session Page"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;

    let (buf, screen) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(screen.contains("Fresh Session Page"), "{screen}");
    assert!(!screen.contains("Preview unavailable."), "{screen}");
    let panel_rect = ui.side_panel.split(buf.area).1;
    assert!(
        (panel_rect.top()..panel_rect.bottom()).any(|y| {
            (panel_rect.left()..panel_rect.right()).any(|x| buf[(x, y)].symbol() == "▀")
        }),
        "expected rendered half-block pixels during fresh session's first run"
    );

    // `/new` clears `selected_session` & `browser_viewport` while preserving `default_session_images_dir`.
    run_command(&mut ui, Command::Session(commands::SessionCommand::New)).await;
    let new_cmd_id = match &ui.state.session_operation {
        Some(reducer::SessionOperation::CreatingSession { command_id }) => command_id.clone(),
        other => panic!("expected CreatingSession operation, got {other:?}"),
    };
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.command.finished",
            "command_id": new_cmd_id,
            "command_type": "new_session",
            "ok": true,
            "error": null,
        }),
    )
    .await;
    assert!(ui.state.selected_session.is_none());
    assert!(ui.state.browser_viewport.is_none());
    let _ = draw_text(&mut ui, 160, 36);

    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:35:00Z",
            Some((&sha, len, 256, 128)),
            Some("https://after-new.test"),
            Some("After New Session"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;
    let (buf2, screen2) = draw_text(&mut ui, 160, 36);
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(screen2.contains("After New Session"), "{screen2}");
    assert!(!screen2.contains("Preview unavailable."), "{screen2}");
    assert!(
        (panel_rect.top()..panel_rect.bottom()).any(|y| {
            (panel_rect.left()..panel_rect.right()).any(|x| buf2[(x, y)].symbol() == "▀")
        }),
        "expected rendered half-block pixels after /new"
    );
}

fn drain_sent_commands(rx: &mut mpsc::Receiver<WriterMessage>) -> Vec<serde_json::Value> {
    let mut commands = Vec::new();
    while let Ok(msg) = rx.try_recv() {
        if let WriterMessage::Frame { payload, .. } = msg {
            commands.push(serde_json::from_slice(&payload).unwrap());
        }
    }
    commands
}

async fn send_input_collecting(ui: &mut LiveUi, input: Input) -> Vec<serde_json::Value> {
    let (writer, mut rx) = mpsc::channel(32);
    ui.handle_input(input, &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    drain_sent_commands(&mut rx)
}

async fn setup_live_browser_ui(dir: &TempSessionDir) -> (LiveUi, Rect) {
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);
    let mut ui = ui_with_session(dir);
    ui.mouse_enabled = true;
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 256, 128)),
            Some("https://app.test"),
            Some("App"),
            Some("mcp__playwright__browser_navigate"),
        ),
    )
    .await;
    let _ = draw_text(&mut ui, 160, 36);
    let image_rect = ui
        .side_panel
        .viewport_image_rect()
        .expect("expected rendered viewport image rect");
    (ui, image_rect)
}

#[tokio::test]
async fn cell_to_viewport_mapping_handles_bounds_and_image_dimension_fallback() {
    let dir = TempSessionDir::new("map-coords");
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);
    let mut ui = ui_with_session(&dir);

    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "browser.viewport.updated",
            "timestamp": "2026-11-20T12:34:56Z",
            "image": {
                "media_type": "image/png",
                "sha256": sha,
                "byte_size": len,
                "width": 1600,
                "height": 900,
            },
            "url": "https://fallback.test",
            "title": "Fallback",
            "viewport_width": null,
            "viewport_height": null,
            "source_tool": "mcp__playwright__browser_take_screenshot",
            "call_id": "call-browser-fallback",
        }),
    )
    .await;

    let _ = draw_text(&mut ui, 160, 36);
    let rect = ui
        .side_panel
        .viewport_image_rect()
        .expect("expected viewport image rect");

    // Outside the image rect returns None.
    assert_eq!(ui.side_panel.map_viewport_point(&ui.state, 0, 0), None);
    assert_eq!(
        ui.side_panel
            .map_viewport_point(&ui.state, rect.right(), rect.y),
        None
    );

    // Top-left cell maps near top-left; bottom-right maps near (1599, 899) via image dimensions.
    let (tl_x, tl_y) = ui
        .side_panel
        .map_viewport_point(&ui.state, rect.x, rect.y)
        .expect("top-left cell");
    let (br_x, br_y) = ui
        .side_panel
        .map_viewport_point(&ui.state, rect.right() - 1, rect.bottom() - 1)
        .expect("bottom-right cell");
    assert!(tl_x > 0 && tl_x < 200, "unexpected tl_x={tl_x}");
    assert!(tl_y > 0 && tl_y < 200, "unexpected tl_y={tl_y}");
    assert!(br_x > 1400 && br_x < 1600, "unexpected br_x={br_x}");
    assert!(br_y > 700 && br_y < 900, "unexpected br_y={br_y}");
}

#[tokio::test]
async fn browser_takeover_entry_chrome_and_auto_exits() {
    let dir = TempSessionDir::new("takeover-lifecycle");
    let mut ui = ui_with_session(&dir);
    let _ = draw_text(&mut ui, 160, 36);

    // `/browser interact` before any frame opens Browser tab with notice and stays inactive.
    run_command(&mut ui, Command::Browser { interact: true }).await;
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);
    assert!(!ui.browser_takeover_active());
    assert_eq!(
        ui.notice.as_deref(),
        Some("No browser viewport is active yet.")
    );

    let (sha, len) = write_split_tone_png(&dir.images_dir(), 256, 128);
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:34:56Z",
            Some((&sha, len, 256, 128)),
            Some("https://takeover.test"),
            Some("Takeover"),
            Some("mcp__playwright__browser_snapshot"),
        ),
    )
    .await;
    let _ = draw_text(&mut ui, 160, 36);

    // `/browser interact` activates docked takeover and renders badge + primary border + footer hints.
    run_command(&mut ui, Command::Browser { interact: true }).await;
    assert!(ui.browser_takeover_active());
    assert!(ui.notice.is_none());
    let (buf, screen) = draw_text(&mut ui, 160, 36);
    assert!(screen.contains("Work · Browser · INTERACT"), "{screen}");
    assert!(screen.contains("Esc exit · Click/Scroll"), "{screen}");
    let panel_rect = ui.side_panel.split(buf.area).1;
    assert_eq!(buf[(panel_rect.x, panel_rect.y)].fg, ui.palette().primary);

    // Esc exits takeover without leaving Browser mode.
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
    )
    .await;
    assert!(!ui.browser_takeover_active());
    assert_eq!(ui.side_panel.mode(), PanelMode::Browser);

    // Narrow overlay: `/browser` then `i` enters takeover, `Ctrl+C` exits takeover without closing overlay.
    let _ = draw_text(&mut ui, 100, 28);
    assert!(!ui.side_panel.overlay_open());
    run_command(&mut ui, Command::Browser { interact: false }).await;
    let _ = draw_text(&mut ui, 100, 28);
    assert!(ui.side_panel.overlay_open());
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE)),
    )
    .await;
    assert!(ui.browser_takeover_active());
    let (_ov_buf, ov_screen) = draw_text(&mut ui, 100, 28);
    assert!(
        ov_screen.contains("Work · Browser · INTERACT"),
        "{ov_screen}"
    );
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
    )
    .await;
    assert!(!ui.browser_takeover_active());
    assert!(ui.side_panel.overlay_open());
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
    )
    .await;
    assert!(!ui.side_panel.overlay_open());

    // Back to wide docked: Ctrl+G opens key help and exits takeover.
    let _ = draw_text(&mut ui, 160, 36);
    run_command(&mut ui, Command::Browser { interact: true }).await;
    assert!(ui.browser_takeover_active());
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('g'), KeyModifiers::CONTROL)),
    )
    .await;
    assert!(ui.key_help.is_some());
    assert!(!ui.browser_takeover_active());
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
    )
    .await;
    assert!(ui.key_help.is_none());

    // Clicking outside the docked panel exits takeover.
    ui.mouse_enabled = true;
    let _ = draw_text(&mut ui, 160, 36);
    run_command(&mut ui, Command::Browser { interact: true }).await;
    assert!(ui.browser_takeover_active());
    send_input(
        &mut ui,
        Input::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 2,
            row: 2,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
    assert!(!ui.browser_takeover_active());

    // Entering WaitingForApproval auto-exits takeover.
    run_command(&mut ui, Command::Browser { interact: true }).await;
    assert!(ui.browser_takeover_active());
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "tool.approval.requested",
            "timestamp": "2026-11-20T12:35:10Z",
            "call_id": "call-approve-1",
            "name": "bash",
            "arguments": {"command": "ls"},
            "safety": "command",
        }),
    )
    .await;
    assert_eq!(ui.state.view_status, ViewStatus::WaitingForApproval);
    assert!(!ui.browser_takeover_active());
}

#[tokio::test]
async fn mouse_click_double_click_buttons_scroll_and_busy_guards() {
    let dir = TempSessionDir::new("mouse-interact");
    let (mut ui, rect) = setup_live_browser_ui(&dir).await;
    let mid_col = rect.x + rect.width / 2;
    let mid_row = rect.y + rect.height / 2;
    let expected = ui
        .side_panel
        .map_viewport_point(&ui.state, mid_col, mid_row)
        .unwrap();

    // First left click enters takeover and emits single click.
    let cmds = send_input_collecting(
        &mut ui,
        Input::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: mid_col,
            row: mid_row,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
    assert!(ui.browser_takeover_active());
    assert_eq!(cmds.len(), 1);
    assert_eq!(cmds[0]["type"], "browser_interact");
    assert_eq!(cmds[0]["action"], "click");
    assert_eq!(cmds[0]["button"], "left");
    assert!(cmds[0].get("double_click").is_none());
    assert_eq!(cmds[0]["x"], expected.0);
    assert_eq!(cmds[0]["y"], expected.1);

    // Second left click on same cell within 400ms emits double_click: true; third omits it.
    let cmds2 = send_input_collecting(
        &mut ui,
        Input::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: mid_col,
            row: mid_row,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
    assert_eq!(cmds2[0]["double_click"], true);
    let cmds3 = send_input_collecting(
        &mut ui,
        Input::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: mid_col,
            row: mid_row,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
    assert!(cmds3[0].get("double_click").is_none());

    // Right and middle clicks inside viewport forward their button.
    let right_cmds = send_input_collecting(
        &mut ui,
        Input::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Right),
            column: mid_col,
            row: mid_row,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
    assert_eq!(right_cmds[0]["button"], "right");
    let middle_cmds = send_input_collecting(
        &mut ui,
        Input::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Middle),
            column: mid_col,
            row: mid_row,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
    assert_eq!(middle_cmds[0]["button"], "middle");

    // Scroll inside viewport emits scroll delta_y = lines * 120.
    let scroll_cmds = send_input_collecting(
        &mut ui,
        Input::Mouse(MouseEvent {
            kind: MouseEventKind::ScrollDown,
            column: mid_col,
            row: mid_row,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
    assert_eq!(scroll_cmds.len(), 1);
    assert_eq!(scroll_cmds[0]["action"], "scroll");
    assert!(scroll_cmds[0]["delta_y"].as_i64().unwrap() > 0);

    // Right click outside panel exits takeover cleanly without dispatching a command.
    let outside_cmds = send_input_collecting(
        &mut ui,
        Input::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Right),
            column: 2,
            row: 2,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
    assert!(outside_cmds.is_empty());
    assert!(!ui.browser_takeover_active());

    // Busy run blocks click/scroll and sets notice.
    ui.state.view_status = ViewStatus::Running;
    let busy_cmds = send_input_collecting(
        &mut ui,
        Input::Mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: mid_col,
            row: mid_row,
            modifiers: KeyModifiers::NONE,
        }),
    )
    .await;
    assert!(busy_cmds.is_empty());
    assert!(!ui.browser_takeover_active());
    assert_eq!(
        ui.notice.as_deref(),
        Some("Wait for the active run to finish before controlling the browser.")
    );
}

#[tokio::test]
async fn takeover_typing_coalescing_paste_nav_and_secret_redaction() {
    let dir = TempSessionDir::new("typing-paste-nav");
    let (mut ui, _rect) = setup_live_browser_ui(&dir).await;

    run_command(&mut ui, Command::Browser { interact: true }).await;
    assert!(ui.browser_takeover_active());

    // Printable chars buffer instead of hitting the composer, and Debug redacts text.
    for ch in ['s', 'e', 'c', 'r', 'e', 't'] {
        let cmds = send_input_collecting(
            &mut ui,
            Input::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE)),
        )
        .await;
        assert!(cmds.is_empty());
    }
    assert_eq!(ui.editor.text(), "");
    let buf_debug = format!("{:?}", ui.browser_typing_buffer);
    assert!(!buf_debug.contains("secret"), "{buf_debug}");
    assert!(buf_debug.contains("[REDACTED]"), "{buf_debug}");

    let req_debug = format!(
        "{:?}",
        UiAction::BrowserInteract(BrowserInteractRequest::Type {
            text: "top-secret-pw".into(),
        })
    );
    assert!(!req_debug.contains("top-secret-pw"), "{req_debug}");
    assert!(req_debug.contains("[REDACTED]"), "{req_debug}");

    // Esc discards buffered chars without dispatching them.
    let esc_cmds = send_input_collecting(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
    )
    .await;
    assert!(esc_cmds.is_empty());
    assert!(ui.browser_typing_buffer.is_empty());
    assert!(!ui.browser_takeover_active());

    // Enter flushes pending typed chars FIFO before sending press_key Enter.
    run_command(&mut ui, Command::Browser { interact: true }).await;
    for ch in ['h', 'i'] {
        send_input(
            &mut ui,
            Input::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE)),
        )
        .await;
    }
    let enter_cmds = send_input_collecting(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
    )
    .await;
    assert_eq!(enter_cmds.len(), 2);
    assert_eq!(enter_cmds[0]["action"], "type");
    assert_eq!(enter_cmds[0]["text"], "hi");
    assert_eq!(enter_cmds[1]["action"], "press_key");
    assert_eq!(enter_cmds[1]["key"], "Enter");

    // 64 printable chars flush immediately; debounced flush sends after deadline.
    for i in 0..64 {
        let ch = if i % 2 == 0 { 'a' } else { 'b' };
        let cmds = send_input_collecting(
            &mut ui,
            Input::Key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE)),
        )
        .await;
        if i < 63 {
            assert!(cmds.is_empty());
        } else {
            assert_eq!(cmds.len(), 1);
            assert_eq!(cmds[0]["action"], "type");
            assert_eq!(cmds[0]["text"].as_str().unwrap().chars().count(), 64);
        }
    }
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('z'), KeyModifiers::NONE)),
    )
    .await;
    let (writer, mut rx) = mpsc::channel(8);
    ui.flush_due_browser_typing(
        std::time::Instant::now() + BROWSER_TYPE_DEBOUNCE + Duration::from_millis(5),
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    let flushed = drain_sent_commands(&mut rx);
    assert_eq!(flushed.len(), 1);
    assert_eq!(flushed[0]["text"], "z");

    // Special/modified keys and navigation shortcuts.
    let backtab = send_input_collecting(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT)),
    )
    .await;
    assert_eq!(backtab[0]["key"], "Tab");
    assert_eq!(backtab[0]["modifiers"], serde_json::json!(["Shift"]));

    let ctrl_a = send_input_collecting(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::CONTROL)),
    )
    .await;
    assert_eq!(ctrl_a[0]["key"], "a");
    assert_eq!(ctrl_a[0]["modifiers"], serde_json::json!(["Control"]));

    let back = send_input_collecting(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Left, KeyModifiers::ALT)),
    )
    .await;
    assert_eq!(back[0]["action"], "back");

    let reload = send_input_collecting(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('r'), KeyModifiers::CONTROL)),
    )
    .await;
    assert_eq!(reload[0]["action"], "reload");

    // Paste strips disallowed controls, splits >1024 chars, and leaves editor untouched.
    let long_paste = format!("x\x00{}", "p".repeat(1_030));
    let paste_cmds = send_input_collecting(&mut ui, Input::Paste(long_paste)).await;
    assert_eq!(paste_cmds.len(), 2);
    assert_eq!(
        paste_cmds[0]["text"].as_str().unwrap().chars().count(),
        1_024
    );
    assert_eq!(paste_cmds[1]["text"].as_str().unwrap().chars().count(), 7);
    assert_eq!(ui.editor.text(), "");
}

#[tokio::test]
async fn browser_interact_non_blocking_completion_and_event_loop_guards() {
    let dir = TempSessionDir::new("nonblocking-reducer");
    let (mut ui, _rect) = setup_live_browser_ui(&dir).await;
    run_command(&mut ui, Command::Browser { interact: true }).await;

    // PendingInput during takeover does not classify keys as editor_input.
    let pending = event_loop::PendingInput::capture(
        Input::Key(KeyEvent::new(KeyCode::Char('k'), KeyModifiers::NONE)),
        &ui,
        0,
    );
    assert!(!pending.is_editor_input());

    // Viewport updates and browser_interact finishes never churn ActivationTarget.
    let vp_event = BackendEvent::from_projection_value(&viewport_event(
        "2026-11-20T12:35:00Z",
        None,
        Some("https://next.test"),
        None,
        None,
    ))
    .unwrap();
    assert!(!event_loop::changes_activation_target(
        &UiAction::BackendEvent(vp_event),
        &ui.state,
    ));

    // Dispatching browser_interact leaves ViewStatus::Idle and current_command None.
    let cmds = send_input_collecting(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
    )
    .await;
    let cmd_id = cmds[0]["id"].as_str().unwrap().to_owned();
    assert_eq!(ui.state.view_status, ViewStatus::Idle);
    assert!(ui.state.current_command.is_none());
    assert!(ui.state.browser_interact_in_flight());

    let finished_ok = serde_json::json!({
        "type": "rpc.command.finished",
        "command_id": cmd_id,
        "command_type": "browser_interact",
        "ok": true,
        "error": null,
    });
    assert!(!event_loop::changes_activation_target(
        &UiAction::BackendEvent(BackendEvent::from_projection_value(&finished_ok).unwrap()),
        &ui.state,
    ));
    push_backend_event(&mut ui, finished_ok).await;
    assert!(!ui.state.browser_interact_in_flight());
    assert!(ui.notice.is_none());

    // Failed browser_interact command emits Notice and clears in-flight state.
    let cmds2 = send_input_collecting(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE)),
    )
    .await;
    let cmd_id2 = cmds2[0]["id"].as_str().unwrap().to_owned();
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.command.finished",
            "command_id": cmd_id2,
            "command_type": "browser_interact",
            "ok": false,
            "error": "No interactive browser MCP tool is connected.",
        }),
    )
    .await;
    assert!(!ui.state.browser_interact_in_flight());
    assert!(
        ui.notice
            .as_deref()
            .is_some_and(|n| n.contains("Browser interaction failed:")),
        "{:?}",
        ui.notice
    );
}

#[tokio::test]
async fn stale_takeover_exit_drops_captured_ctrl_c_and_escape_before_apply() {
    let dir = TempSessionDir::new("stale-takeover-exit");
    let (mut ui, _rect) = setup_live_browser_ui(&dir).await;

    // 1) Ctrl+C captured in takeover must NOT cancel a run that started before apply.
    run_command(&mut ui, Command::Browser { interact: true }).await;
    assert!(ui.browser_takeover_active());
    let pending_ctrl_c = event_loop::PendingInput::capture(
        Input::Key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::CONTROL)),
        &ui,
        0,
    );
    let (run_tx, _run_rx) = mpsc::channel(8);
    ui.dispatch(
        UiAction::Submit("run".into()),
        &run_tx,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    assert_eq!(ui.state.view_status, ViewStatus::Running);
    assert!(!ui.browser_takeover_active());

    let (writer, mut rx) = mpsc::channel(8);
    pending_ctrl_c
        .apply(&mut ui, &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    let cmds = drain_sent_commands(&mut rx);
    assert!(cmds.is_empty(), "stale takeover Ctrl+C leaked: {cmds:?}");
    assert!(!ui.state.cancel_requested);

    // Finish the submitted prompt cleanly before testing Esc vs unseen approval prompt.
    let active_cmd_id = ui.state.current_command.as_ref().unwrap().id.clone();
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.command.finished",
            "command_id": active_cmd_id,
            "command_type": "prompt",
            "ok": true,
            "error": null,
        }),
    )
    .await;
    assert_eq!(ui.state.view_status, ViewStatus::Idle);

    // 2) Esc captured in takeover must NOT silently deny an unseen approval prompt.
    run_command(&mut ui, Command::Browser { interact: true }).await;
    assert!(ui.browser_takeover_active());
    let pending_esc = event_loop::PendingInput::capture(
        Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
        &ui,
        0,
    );
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "tool.approval.requested",
            "timestamp": "2026-11-20T12:35:20Z",
            "call_id": "call-approve-stale",
            "name": "bash",
            "arguments": {"command": "ls"},
            "safety": "command",
        }),
    )
    .await;
    assert_eq!(ui.state.view_status, ViewStatus::WaitingForApproval);
    assert!(!ui.browser_takeover_active());

    let (writer2, mut rx2) = mpsc::channel(8);
    pending_esc
        .apply(&mut ui, &writer2, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    let cmds2 = drain_sent_commands(&mut rx2);
    assert!(cmds2.is_empty(), "stale takeover Esc leaked: {cmds2:?}");
    assert_eq!(ui.state.view_status, ViewStatus::WaitingForApproval);
}

#[tokio::test]
async fn takeover_ignores_non_printable_unicode_format_chars_with_modifiers() {
    let dir = TempSessionDir::new("format-char-filter");
    let (mut ui, _rect) = setup_live_browser_ui(&dir).await;
    run_command(&mut ui, Command::Browser { interact: true }).await;
    assert!(ui.browser_takeover_active());

    for (ch, modifier) in [
        ('\u{200b}', KeyModifiers::CONTROL),
        ('\u{200e}', KeyModifiers::ALT),
    ] {
        let cmds = send_input_collecting(
            &mut ui,
            Input::Key(KeyEvent::new(KeyCode::Char(ch), modifier)),
        )
        .await;
        assert!(cmds.is_empty(), "unexpected command emitted: {cmds:?}");
        assert!(!ui.state.browser_interact_in_flight());
    }
}

#[tokio::test]
async fn kitty_unicode_viewport_paints_placeholders_and_expands_panel() {
    let dir = TempSessionDir::new("kitty-u1-expand");
    let (mut ui, _) = setup_live_browser_ui(&dir).await;
    // Use a desktop-resolution frame (1280×720) so expanding the panel grows the viewport rect.
    let (sha, len) = write_split_tone_png(&dir.images_dir(), 1280, 720);
    push_backend_event(
        &mut ui,
        viewport_event(
            "2026-11-20T12:35:00Z",
            Some((&sha, len, 1280, 720)),
            Some("https://example.com/app"),
            Some("Desktop App"),
            Some("mcp__playwright__browser_take_screenshot"),
        ),
    )
    .await;
    ui.image_protocol = terminal_images::ImageProtocol::KittyUnicode;

    let mut terminal = Terminal::new(RecordingBackend::new(160, 36)).unwrap();
    ui.draw(&mut terminal, &conn()).unwrap();
    let docked_rect = ui
        .side_panel
        .viewport_image_rect()
        .expect("docked viewport image rect");
    let events = terminal.backend_mut().take_events();
    let bytes = events
        .iter()
        .find_map(|ev| ev.strip_prefix("bytes:"))
        .expect("expected Kitty U=1 virtual placement payload");
    assert!(bytes.contains("a=T,f=32,"), "{bytes}");
    assert!(bytes.contains(",U=1,q=2,"), "{bytes}");

    let buf = terminal.backend().inner.buffer();
    let mut placeholder_cells = 0usize;
    for y in 0..buf.area.height {
        for x in 0..buf.area.width {
            let cell = &buf[(x, y)];
            if cell
                .symbol()
                .starts_with(terminal_images::KITTY_PLACEHOLDER)
            {
                assert_eq!(
                    cell.diff_option,
                    CellDiffOption::ForcedWidth(std::num::NonZeroU16::new(1).unwrap())
                );
                placeholder_cells += 1;
            }
        }
    }
    assert!(placeholder_cells > 0, "expected U=1 placeholder cells");

    // Steady-state redraw emits zero native graphics bytes.
    ui.draw(&mut terminal, &conn()).unwrap();
    let steady_events = terminal.backend_mut().take_events();
    assert!(
        !steady_events.iter().any(|ev| ev.starts_with("bytes:")),
        "steady U=1 frame should not re-transmit: {steady_events:?}"
    );

    // Focusing side panel and pressing `f` expands into a much larger centered overlay.
    ui.side_panel.focus();
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::NONE)),
    )
    .await;
    assert!(ui.side_panel.expanded());
    ui.draw(&mut terminal, &conn()).unwrap();
    let expanded_rect = ui
        .side_panel
        .viewport_image_rect()
        .expect("expanded viewport image rect");
    assert!(
        expanded_rect.width > docked_rect.width,
        "expanded viewport ({expanded_rect:?}) should be wider than docked ({docked_rect:?})"
    );

    // Entering interactive takeover while expanded must keep the expanded overlay open.
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE)),
    )
    .await;
    assert!(ui.browser_takeover_active());
    assert!(ui.side_panel.expanded());
    assert!(ui.side_panel.overlay_open());

    // First Esc leaves INTERACT while keeping the expanded overlay open.
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)),
    )
    .await;
    assert!(!ui.browser_takeover_active());
    assert!(ui.side_panel.expanded());
    assert!(ui.side_panel.overlay_open());

    // Pressing `f` restores the docked panel.
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('f'), KeyModifiers::NONE)),
    )
    .await;
    assert!(!ui.side_panel.expanded());
    assert!(!ui.side_panel.overlay_open());
}

async fn push_live_browser_frame(
    ui: &mut LiveUi,
    dir: &TempSessionDir,
    geometry: wisp_protocol::commands::LiveBrowserViewport,
) {
    use base64::Engine;
    let (sha, _) = write_split_tone_png(
        &dir.images_dir(),
        geometry.width * geometry.device_scale_factor,
        geometry.height * geometry.device_scale_factor,
    );
    let png = std::fs::read(dir.images_dir().join(sha)).unwrap();
    push_backend_event(
        ui,
        serde_json::json!({
            "type": "browser.viewport.updated",
            "timestamp": "2026-11-20T12:34:56Z",
            "live_png": base64::engine::general_purpose::STANDARD.encode(png),
            "live_device_scale": geometry.device_scale_factor,
            "viewport_width": geometry.width,
            "viewport_height": geometry.height,
            "url": "https://example.test/",
            "title": "Live Chromium",
        }),
    )
    .await;
}

#[tokio::test]
async fn kitty_drag_reuses_browser_pixels() {
    use crossterm::event::{MouseButton, MouseEventKind};
    use terminal_images::ImageProtocol;
    use wisp_protocol::commands::LiveBrowserViewport;

    for protocol in [ImageProtocol::Kitty, ImageProtocol::KittyUnicode] {
        let dir = TempSessionDir::new("kitty-drag");
        let mut ui = ui_with_session(&dir);
        ui.image_protocol = protocol;
        push_live_browser_frame(
            &mut ui,
            &dir,
            LiveBrowserViewport {
                width: 1280,
                height: 720,
                device_scale_factor: 1,
            },
        )
        .await;
        let mut terminal = Terminal::new(RecordingBackend::new(180, 40)).unwrap();
        ui.draw(&mut terminal, &conn()).unwrap();
        assert!(
            terminal
                .backend_mut()
                .take_events()
                .iter()
                .any(|event| event.contains("a=T,f=100,"))
        );

        let now = std::time::Instant::now();
        let border = 180 - ui.side_panel.docked_width(180);
        let mut image_width = ui.side_panel.viewport_image_rect().unwrap().width;
        ui.side_panel.press_border(now, 0);
        for step in [8, 16] {
            ui.side_panel.continue_resize(
                MouseEventKind::Drag(MouseButton::Left),
                border - step,
                now,
            );
            ui.draw(&mut terminal, &conn()).unwrap();
            let resized = ui.side_panel.viewport_image_rect().unwrap().width;
            assert!(resized > image_width);
            image_width = resized;
            let events = terminal.backend_mut().take_events();
            assert!(events.iter().any(|event| event.contains("a=p,")));
            assert!(events.iter().all(|event| !event.contains("a=T,")));
        }
    }
}

#[tokio::test]
async fn resize_targets_painted_geometry() {
    let dir = TempSessionDir::new("live-resize");
    let mut ui = ui_with_session(&dir);
    use wisp_protocol::commands::LiveBrowserViewport;
    push_live_browser_frame(
        &mut ui,
        &dir,
        LiveBrowserViewport {
            width: 900,
            height: 600,
            device_scale_factor: 1,
        },
    )
    .await;
    ui.cell_size = terminal_images::CellSize {
        width_px: 16,
        height_px: 32,
    };
    draw_text(&mut ui, 160, 36);
    let old_rect = ui.side_panel.viewport_image_rect().unwrap();
    let desired = ui
        .side_panel
        .desired_browser_geometry(ui.cell_size)
        .unwrap();
    assert_eq!(desired.device_scale_factor, 2);
    assert!(desired.width < 900);
    assert!(
        ui.side_panel
            .map_viewport_point(&ui.state, old_rect.x, old_rect.y)
            .is_some()
    );

    let (writer, mut commands) = mpsc::channel(8);
    let now = std::time::Instant::now();
    ui.poll_browser_viewport(now, &writer, MAX_APPLICATION_FRAME_BYTES)
        .await
        .unwrap();
    let requests = drain_sent_commands(&mut commands);
    assert_eq!(requests.len(), 1);
    let request = &requests[0];
    assert_eq!(request["action"], "live_resize");
    assert_eq!(request["viewport"], serde_json::to_value(desired).unwrap());
    assert!(
        ui.side_panel
            .map_viewport_point(&ui.state, old_rect.x, old_rect.y)
            .is_none()
    );
    ui.enter_browser_takeover();
    send_input(
        &mut ui,
        Input::Key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE)),
    )
    .await;
    assert_eq!(ui.browser_typing_buffer.text, "x");
    ui.clear_browser_takeover_buffers();

    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.command.finished", "timestamp": "2026-11-20T12:34:57Z",
            "command_type": "browser_interact", "command_id": request["id"], "ok": false,
            "error": "Finish or cancel the active operation before interacting with the browser",
        }),
    )
    .await;
    ui.poll_browser_viewport(
        now + LIVE_BROWSER_RESIZE_INTERVAL,
        &writer,
        MAX_APPLICATION_FRAME_BYTES,
    )
    .await
    .unwrap();
    let retry = drain_sent_commands(&mut commands);
    assert_eq!(retry.len(), 1);
    assert_eq!(retry[0]["action"], "live_resize");
    assert_eq!(retry[0]["viewport"], request["viewport"]);

    push_live_browser_frame(&mut ui, &dir, desired).await;
    push_backend_event(
        &mut ui,
        serde_json::json!({
            "type": "rpc.command.finished", "timestamp": "2026-11-20T12:34:57Z",
            "command_type": "browser_interact", "command_id": retry[0]["id"], "ok": true,
        }),
    )
    .await;
    assert!(
        ui.side_panel
            .map_viewport_point(&ui.state, old_rect.x, old_rect.y)
            .is_none()
    );
    draw_text(&mut ui, 160, 36);
    let rect = ui.side_panel.viewport_image_rect().unwrap();
    assert_eq!(
        ui.side_panel.map_viewport_point(&ui.state, rect.x, rect.y),
        Some((4, 8))
    );
}

#[tokio::test]
async fn address_focus_keeps_chat_draft() {
    let dir = TempSessionDir::new("live-address");
    let mut ui = ui_with_session(&dir);
    push_live_browser_frame(
        &mut ui,
        &dir,
        wisp_protocol::commands::LiveBrowserViewport {
            width: 320,
            height: 240,
            device_scale_factor: 1,
        },
    )
    .await;
    ui.editor.replace_range(0..0, "draft for the agent");
    draw_text(&mut ui, 160, 36);
    ui.enter_browser_takeover();

    for width in [100, 160] {
        draw_text(&mut ui, width, 36);
        assert!(ui.side_panel.visible());
        assert_eq!(ui.side_panel.overlay_open(), width == 100);
        assert!(ui.browser_takeover_active());
        send_input(
            &mut ui,
            Input::Key(KeyEvent::new(KeyCode::Char('l'), KeyModifiers::CONTROL)),
        )
        .await;
        assert!(ui.browser_address_active());

        let mut terminal = Terminal::new(ratatui::backend::TestBackend::new(width, 36)).unwrap();
        ui.draw(&mut terminal, &conn()).unwrap();
        let (address_row, _) = buffer_text(terminal.backend().buffer())
            .lines()
            .enumerate()
            .find(|(_, line)| line.contains("https://example.test/"))
            .expect("painted address");
        let caret = ratatui::backend::Backend::get_cursor_position(terminal.backend_mut()).unwrap();
        assert_eq!(usize::from(caret.y), address_row);

        send_input(&mut ui, Input::Paste("example.net/next".into())).await;
        let commands = send_input_collecting(
            &mut ui,
            Input::Key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE)),
        )
        .await;
        assert_eq!(commands.len(), 1);
        assert_eq!(commands[0]["action"], "live_navigate");
        assert_eq!(commands[0]["url"], "https://example.net/next");
        assert_eq!(ui.editor.text(), "draft for the agent");
        assert!(!ui.browser_address_active());
        push_backend_event(
            &mut ui,
            serde_json::json!({
                "type": "rpc.command.finished", "timestamp": "2026-11-20T12:34:57Z",
                "command_type": "browser_interact", "command_id": commands[0]["id"], "ok": true,
            }),
        )
        .await;
        ui.enter_browser_takeover();
    }
}
