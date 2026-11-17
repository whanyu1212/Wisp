use super::*;
use crate::color_depth::ColorDepth;
use crossterm::event::{KeyCode, KeyModifiers};
use ratatui::{Terminal, backend::TestBackend, style::Color};

fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

#[test]
fn frame_paints_optional_sections_and_only_visible_controls() {
    let tabs = [
        Tab {
            label: "Models",
            selected: true,
            key: key(KeyCode::Left),
        },
        Tab {
            label: "Providers",
            selected: false,
            key: key(KeyCode::Right),
        },
    ];
    let hints = [
        Hint {
            label: "Enter",
            description: "select",
            key: key(KeyCode::Enter),
        },
        Hint {
            label: "Esc",
            description: "close",
            key: key(KeyCode::Esc),
        },
    ];
    let dialog = Dialog {
        title: "Choose",
        tabs: &tabs,
        filter: Some("/ to search"),
        hints: &hints,
    };
    let mut terminal = Terminal::new(TestBackend::new(80, 20)).unwrap();
    let mut layout = None;
    terminal
        .draw(|frame| {
            layout = Some(dialog.render(frame, Rect::new(3, 2, 70, 14), Palette::default()));
        })
        .unwrap();
    let layout = layout.unwrap();
    let buffer = terminal.backend().buffer();
    let text = buffer
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect::<String>();
    for label in [
        "─ Choose ─",
        "Models",
        "Providers",
        "/ to search",
        "Enter select",
        "Esc close",
    ] {
        assert!(text.contains(label), "{label}");
    }
    assert_eq!(layout.controls.0.len(), 4);
    for control in &layout.controls.0 {
        assert_eq!(
            layout.controls.key_at(control.area.x, control.area.y),
            Some(control.key)
        );
        assert!(
            !layout
                .body
                .contains((control.area.x, control.area.y).into())
        );
        assert!(
            Rect::new(3, 2, 70, 14).contains((control.area.right() - 1, control.area.y).into())
        );
    }
    let enter = layout
        .controls
        .0
        .iter()
        .find(|control| control.key == key(KeyCode::Enter))
        .unwrap();
    assert!(
        buffer[(enter.area.x, enter.area.y)]
            .modifier
            .contains(Modifier::BOLD)
    );
    assert!(
        !buffer[(enter.area.x + 6, enter.area.y)]
            .modifier
            .contains(Modifier::BOLD)
    );
    assert_eq!(
        buffer[(enter.area.x + 6, enter.area.y)].fg,
        Palette::default().muted
    );
    assert_eq!(layout.controls.key_at(3, 2), None, "border is not a button");
}

#[test]
fn narrow_frames_drop_whole_hints_and_clipped_tabs_without_click_targets() {
    let tabs = [Tab {
        label: "Does not fit",
        selected: true,
        key: key(KeyCode::Right),
    }];
    let hints = [
        Hint {
            label: "Esc",
            description: "close",
            key: key(KeyCode::Esc),
        },
        Hint {
            label: "Enter",
            description: "a description that does not fit",
            key: key(KeyCode::Enter),
        },
    ];
    for width in 0..20 {
        for height in 0..9 {
            let mut terminal = Terminal::new(TestBackend::new(24, 12)).unwrap();
            let mut layout = None;
            terminal
                .draw(|frame| {
                    layout = Some(
                        Dialog {
                            title: "Dialog",
                            tabs: &tabs,
                            filter: Some("/ search"),
                            hints: &hints,
                        }
                        .render(
                            frame,
                            Rect::new(1, 1, width, height),
                            Palette::default(),
                        ),
                    );
                })
                .unwrap();
            let layout = layout.unwrap();
            for control in &layout.controls.0 {
                assert!(control.area.width > 0);
                assert!(
                    Rect::new(1, 1, width, height)
                        .contains((control.area.right() - 1, control.area.y).into())
                );
                assert_ne!(control.key, key(KeyCode::Enter), "clipped hints are inert");
            }
        }
    }
}

#[test]
fn picker_rows_fill_the_band_align_tags_and_sanitize_untrusted_text() {
    for theme in crate::theme::themes() {
        for no_color in [false, true] {
            for depth in [
                ColorDepth::TrueColor,
                ColorDepth::Ansi256,
                ColorDepth::Ansi16,
            ] {
                let palette = theme.palette(no_color).for_depth(depth);
                let mut terminal = Terminal::new(TestBackend::new(40, 4)).unwrap();
                terminal
                    .draw(|frame| {
                        PickerRow {
                            label: "model \u{1b}]52;bad\u{7}",
                            description: Some("a quiet description"),
                            tag: Some("Preview"),
                            selected: true,
                            muted: false,
                        }
                        .render(frame, Rect::new(2, 1, 36, 2), palette);
                        depth.downconvert_buffer(frame.buffer_mut());
                    })
                    .unwrap();
                let buffer = terminal.backend().buffer();
                for y in 1..3 {
                    for x in 2..38 {
                        let cell = &buffer[(x, y)];
                        assert!(!cell.symbol().chars().any(char::is_control));
                        if palette.shows_bands() {
                            assert_eq!(cell.bg, depth.downconvert(palette.selected));
                        } else {
                            assert!(cell.modifier.contains(Modifier::REVERSED));
                        }
                    }
                }
                assert!(buffer[(3, 1)].modifier.contains(Modifier::BOLD));
                assert!(!buffer[(3, 2)].modifier.contains(Modifier::BOLD));
                assert_eq!(buffer[(30, 1)].symbol(), "P");
                assert_eq!(buffer[(2, 1)].symbol(), " ", "no cursor glyph");
                assert_eq!(
                    buffer[(1, 1)].bg,
                    Color::Reset,
                    "fill stays inside the allocated row"
                );
            }
        }
    }
}
