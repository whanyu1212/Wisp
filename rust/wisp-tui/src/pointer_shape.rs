//! The mouse pointer's shape, set with OSC 22 while the pointer is over the docked
//! side panel's border or dragging it.
//!
//! Only terminals known to draw OSC 22 receive it: a terminal that does not understand
//! the sequence may print it, so every other terminal, and tmux, which does not pass
//! it through, keeps its normal pointer and gets no bytes at all.

/// The pointer shapes the TUI asks for.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) enum PointerShape {
    /// The terminal's own pointer.
    #[default]
    Default,
    /// A horizontal resize arrow.
    Resize,
}

impl PointerShape {
    /// The OSC 22 sequence that sets this shape. Plain set and reset, not kitty's
    /// push/pop stack, and ST rather than BEL as the terminator.
    pub(crate) fn sequence(self) -> &'static [u8] {
        match self {
            Self::Default => b"\x1b]22;\x1b\\",
            Self::Resize => b"\x1b]22;ew-resize\x1b\\",
        }
    }
}

/// Whether the terminal described by `var` draws OSC 22 pointer shapes: kitty,
/// Ghostty, foot and Rex, and never inside tmux.
pub(crate) fn supported(var: impl Fn(&str) -> Option<String>) -> bool {
    let set = |name: &str| var(name).is_some_and(|value| !value.trim().is_empty());
    if set("TMUX") {
        return false;
    }
    let term = var("TERM").unwrap_or_default().trim().to_ascii_lowercase();
    let term_program = var("TERM_PROGRAM")
        .unwrap_or_default()
        .trim()
        .to_ascii_lowercase();
    set("KITTY_WINDOW_ID")
        || term.contains("kitty")
        || term_program == "ghostty"
        // Rex reports a plain xterm TERM, so only its TERM_PROGRAM names it.
        || term_program == "rex"
        || term.contains("ghostty")
        || term.starts_with("foot")
}

/// The pointer shape last sent to the terminal, so a shape is written only when it
/// changes, and never when the terminal does not support it.
#[derive(Debug, Default)]
pub(crate) struct PointerShapeState {
    supported: bool,
    sent: PointerShape,
}

impl PointerShapeState {
    pub(crate) fn new(supported: bool) -> Self {
        Self {
            supported,
            sent: PointerShape::Default,
        }
    }

    /// The shape the terminal shows, as far as the TUI has set it.
    pub(crate) fn sent(&self) -> PointerShape {
        self.sent
    }

    /// Whether `wanted` must be written to the terminal: it is supported and differs
    /// from the shape last sent.
    pub(crate) fn needs(&self, wanted: PointerShape) -> bool {
        self.supported && wanted != self.sent
    }

    /// Records that `wanted` was written.
    pub(crate) fn record(&mut self, wanted: PointerShape) {
        self.sent = wanted;
    }

    /// Forgets the shape after the terminal reset it (suspending for the external
    /// editor), so the next hover over the border sends it again.
    pub(crate) fn forget(&mut self) {
        self.sent = PointerShape::Default;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn supported_terminals() {
        let cases: &[(&[(&str, &str)], bool)] = &[
            (&[("KITTY_WINDOW_ID", "1")], true),
            (&[("TERM", "xterm-kitty")], true),
            (&[("TERM_PROGRAM", "Ghostty")], true),
            (&[("TERM", "xterm-ghostty")], true),
            (&[("TERM", "foot")], true),
            (&[("TERM", "foot-extra")], true),
            (&[("TERM_PROGRAM", "Rex"), ("TERM", "xterm-256color")], true),
            (&[("TERM_PROGRAM", "rex"), ("TMUX", "/tmp/tmux,1,0")], false),
            (
                &[("KITTY_WINDOW_ID", "1"), ("TMUX", "/tmp/tmux,1,0")],
                false,
            ),
            (&[("TERM", "foot"), ("TMUX", "/tmp/tmux,1,0")], false),
            (&[("TERM", "xterm-256color")], false),
            (&[("TERM_PROGRAM", "WezTerm")], false),
            (&[("TERM_PROGRAM", "iTerm.app")], false),
            (&[("KITTY_WINDOW_ID", " ")], false),
            (&[], false),
        ];
        for (env, expected) in cases {
            let var = |name: &str| {
                env.iter()
                    .find(|(key, _)| *key == name)
                    .map(|(_, value)| (*value).to_owned())
            };
            assert_eq!(supported(var), *expected, "{env:?}");
        }
    }

    #[test]
    fn exact_sequences() {
        assert_eq!(PointerShape::Resize.sequence(), b"\x1b]22;ew-resize\x1b\\");
        assert_eq!(PointerShape::Default.sequence(), b"\x1b]22;\x1b\\");
    }

    #[test]
    fn needs_only_changes() {
        let mut state = PointerShapeState::new(true);
        assert!(!state.needs(PointerShape::Default));
        assert!(state.needs(PointerShape::Resize));
        state.record(PointerShape::Resize);
        assert!(!state.needs(PointerShape::Resize));
        assert!(state.needs(PointerShape::Default));
        state.forget();
        assert_eq!(state.sent(), PointerShape::Default);
        assert!(state.needs(PointerShape::Resize));
    }

    #[test]
    fn unsupported_never_needs() {
        let state = PointerShapeState::new(false);
        assert!(!state.needs(PointerShape::Resize));
        assert!(!state.needs(PointerShape::Default));
    }
}
