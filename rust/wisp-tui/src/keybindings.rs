//! Configurable application actions and their terminal key chords.

use std::collections::HashSet;
use std::fmt;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde::de::{self, Deserialize, Deserializer, MapAccess, Visitor};

const MAX_CONFIG_BYTES: usize = 64 * 1024;
const MAX_CONFIG_ENTRIES: usize = 64;
const MAX_CHORDS_PER_ACTION: usize = 8;
const MAX_CHORD_CHARS: usize = 64;
const MAX_DIAGNOSTIC_CHARS: usize = 512;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub(crate) enum Action {
    Submit,
    AlternateSubmit,
    Newline,
    RestoreQueue,
    History,
    ToggleTheme,
    Browse,
    PageUp,
    PageDown,
    Home,
    Tail,
    LineUp,
    LineDown,
}

impl Action {
    pub(crate) const ALL: [Self; 13] = [
        Self::Submit,
        Self::AlternateSubmit,
        Self::Newline,
        Self::RestoreQueue,
        Self::History,
        Self::ToggleTheme,
        Self::Browse,
        Self::PageUp,
        Self::PageDown,
        Self::Home,
        Self::Tail,
        Self::LineUp,
        Self::LineDown,
    ];

    pub(crate) const fn id(self) -> &'static str {
        match self {
            Self::Submit => "prompt.submit",
            Self::AlternateSubmit => "prompt.alternate_submit",
            Self::Newline => "prompt.newline",
            Self::RestoreQueue => "queue.restore",
            Self::History => "history.open",
            Self::ToggleTheme => "theme.toggle",
            Self::Browse => "transcript.browse",
            Self::PageUp => "transcript.page_up",
            Self::PageDown => "transcript.page_down",
            Self::Home => "transcript.home",
            Self::Tail => "transcript.tail",
            Self::LineUp => "transcript.line_up",
            Self::LineDown => "transcript.line_down",
        }
    }

    pub(crate) const fn description(self) -> &'static str {
        match self {
            Self::Submit => "Send prompt or steer an active run",
            Self::AlternateSubmit => "Insert a newline or queue a follow-up",
            Self::Newline => "Insert a newline",
            Self::RestoreQueue => "Restore the newest queued prompt",
            Self::History => "Open prompt history",
            Self::ToggleTheme => "Toggle the theme",
            Self::Browse => "Browse transcript details",
            Self::PageUp => "Scroll transcript up one page",
            Self::PageDown => "Scroll transcript down one page",
            Self::Home => "Go to the start of the transcript",
            Self::Tail => "Follow the transcript tail",
            Self::LineUp => "Scroll transcript up one line",
            Self::LineDown => "Scroll transcript down one line",
        }
    }

    fn from_id(id: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|action| action.id() == id)
    }

    const fn required(self) -> bool {
        matches!(self, Self::Submit | Self::Newline)
    }

    const fn index(self) -> usize {
        self as usize
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Chord {
    code: KeyCode,
    modifiers: KeyModifiers,
    control_with_extras: bool,
    label: String,
}

impl Chord {
    fn exact(code: KeyCode, modifiers: KeyModifiers, label: &str) -> Self {
        Self {
            code,
            modifiers,
            control_with_extras: false,
            label: label.into(),
        }
    }

    fn hidden_exact(code: KeyCode, modifiers: KeyModifiers) -> Self {
        Self::exact(code, modifiers, "")
    }

    fn control_alias(code: KeyCode, label: &str) -> Self {
        Self {
            code,
            modifiers: KeyModifiers::CONTROL,
            control_with_extras: true,
            label: label.into(),
        }
    }

    fn matches(&self, key: KeyEvent) -> bool {
        same_code(&self.code, &key.code)
            && if self.control_with_extras {
                key.modifiers.contains(KeyModifiers::CONTROL)
            } else {
                key.modifiers == self.modifiers
            }
    }

    fn overlaps(&self, other: &Self) -> bool {
        if !same_code(&self.code, &other.code) {
            return false;
        }
        match (self.control_with_extras, other.control_with_extras) {
            (false, false) => self.modifiers == other.modifiers,
            (true, false) => other.modifiers.contains(KeyModifiers::CONTROL),
            (false, true) => self.modifiers.contains(KeyModifiers::CONTROL),
            (true, true) => true,
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct HelpEntry {
    pub action: Action,
    pub id: &'static str,
    pub description: &'static str,
    pub label: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct Bindings {
    chords: Vec<Vec<Chord>>,
}

struct RawOverrides(Vec<(String, Vec<String>)>);

impl<'de> Deserialize<'de> for RawOverrides {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        struct OverridesVisitor;

        impl<'de> Visitor<'de> for OverridesVisitor {
            type Value = RawOverrides;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("a JSON object mapping action IDs to chord arrays")
            }

            fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
            where
                A: MapAccess<'de>,
            {
                let mut entries = Vec::<(String, Vec<String>)>::new();
                while let Some((id, chords)) = map.next_entry()? {
                    if entries.len() == MAX_CONFIG_ENTRIES {
                        return Err(de::Error::custom(format_args!(
                            "keybinding configuration has more than {MAX_CONFIG_ENTRIES} entries"
                        )));
                    }
                    if entries.iter().any(|(existing, _)| existing == &id) {
                        return Err(de::Error::custom(format_args!(
                            "duplicate keybinding action `{id}`"
                        )));
                    }
                    entries.push((id, chords));
                }
                Ok(RawOverrides(entries))
            }
        }

        deserializer.deserialize_map(OverridesVisitor)
    }
}

impl Default for Bindings {
    fn default() -> Self {
        use Action::*;

        let mut chords = vec![Vec::new(); Action::ALL.len()];
        chords[Submit.index()] = vec![
            Chord::exact(KeyCode::Enter, KeyModifiers::NONE, "Enter"),
            Chord::hidden_exact(KeyCode::Enter, KeyModifiers::CONTROL),
        ];
        chords[AlternateSubmit.index()] =
            vec![Chord::exact(KeyCode::Enter, KeyModifiers::ALT, "Alt+Enter")];
        chords[Newline.index()] = vec![
            Chord::exact(KeyCode::Enter, KeyModifiers::SHIFT, "Shift+Enter"),
            Chord::control_alias(KeyCode::Char('j'), "Ctrl+J"),
            Chord::hidden_exact(KeyCode::Enter, KeyModifiers::SHIFT | KeyModifiers::ALT),
            Chord::hidden_exact(KeyCode::Enter, KeyModifiers::CONTROL | KeyModifiers::SHIFT),
            Chord::hidden_exact(KeyCode::Enter, KeyModifiers::CONTROL | KeyModifiers::ALT),
            Chord::hidden_exact(
                KeyCode::Enter,
                KeyModifiers::CONTROL | KeyModifiers::SHIFT | KeyModifiers::ALT,
            ),
        ];
        chords[RestoreQueue.index()] = vec![Chord::exact(KeyCode::Up, KeyModifiers::ALT, "Alt+Up")];
        chords[History.index()] = vec![Chord::exact(
            KeyCode::Char('r'),
            KeyModifiers::CONTROL,
            "Ctrl+R",
        )];
        chords[ToggleTheme.index()] = vec![Chord::exact(
            KeyCode::Char('t'),
            KeyModifiers::CONTROL,
            "Ctrl+T",
        )];
        chords[Browse.index()] = vec![Chord::exact(KeyCode::F(6), KeyModifiers::NONE, "F6")];
        chords[PageUp.index()] = vec![Chord::exact(KeyCode::PageUp, KeyModifiers::NONE, "PgUp")];
        chords[PageDown.index()] =
            vec![Chord::exact(KeyCode::PageDown, KeyModifiers::NONE, "PgDn")];
        chords[Home.index()] = vec![Chord::control_alias(KeyCode::Home, "Ctrl+Home")];
        chords[Tail.index()] = vec![Chord::control_alias(KeyCode::End, "Ctrl+End")];
        chords[LineUp.index()] = vec![Chord::control_alias(KeyCode::Up, "Ctrl+Up")];
        chords[LineDown.index()] = vec![Chord::control_alias(KeyCode::Down, "Ctrl+Down")];
        Self { chords }
    }
}

impl Bindings {
    /// Resolve a complete binding set from a JSON object of action overrides.
    ///
    /// Missing actions keep their defaults. An array replaces an action's
    /// complete default chord list, and an empty array unbinds an optional
    /// action. Any invalid member rejects the complete set.
    pub(crate) fn from_json(json: &str) -> Result<Self, String> {
        if json.len() > MAX_CONFIG_BYTES {
            return Err(diagnostic(format_args!(
                "keybinding configuration exceeds {MAX_CONFIG_BYTES} bytes"
            )));
        }
        let RawOverrides(overrides) = serde_json::from_str(json)
            .map_err(|error| diagnostic(format_args!("invalid keybinding JSON: {error}")))?;

        let mut resolved = Self::default();
        for (id, values) in overrides {
            let Some(action) = Action::from_id(&id) else {
                return Err(diagnostic(format_args!(
                    "unknown keybinding action `{}`",
                    safe_fragment(&id)
                )));
            };
            if values.len() > MAX_CHORDS_PER_ACTION {
                return Err(diagnostic(format_args!(
                    "keybinding action `{}` has more than {MAX_CHORDS_PER_ACTION} chords",
                    action.id()
                )));
            }
            if action.required() && values.is_empty() {
                return Err(diagnostic(format_args!(
                    "keybinding action `{}` cannot be unbound",
                    action.id()
                )));
            }
            let mut parsed = Vec::with_capacity(values.len());
            for raw in values {
                if raw.chars().count() > MAX_CHORD_CHARS {
                    return Err(diagnostic(format_args!(
                        "keybinding action `{}` contains a chord longer than {MAX_CHORD_CHARS} characters",
                        action.id()
                    )));
                }
                parsed.push(parse_chord(&raw).map_err(|reason| {
                    diagnostic(format_args!(
                        "invalid chord `{}` for `{}`: {reason}",
                        safe_fragment(&raw),
                        action.id()
                    ))
                })?);
            }
            resolved.chords[action.index()] = parsed;
        }
        resolved.validate_conflicts()?;
        Ok(resolved)
    }

    pub(crate) fn action(&self, key: KeyEvent) -> Option<Action> {
        Action::ALL.into_iter().find(|action| {
            self.chords[action.index()]
                .iter()
                .any(|chord| chord.matches(key))
        })
    }

    pub(crate) fn label(&self, action: Action) -> String {
        let chords = &self.chords[action.index()];
        if chords.is_empty() {
            "Unbound".into()
        } else {
            chords
                .iter()
                .filter(|chord| !chord.label.is_empty())
                .map(|chord| chord.label.as_str())
                .collect::<Vec<_>>()
                .join(" / ")
        }
    }

    pub(crate) fn help_entries(&self) -> Vec<HelpEntry> {
        Action::ALL
            .into_iter()
            .map(|action| HelpEntry {
                action,
                id: action.id(),
                description: action.description(),
                label: self.label(action),
            })
            .collect()
    }

    fn validate_conflicts(&self) -> Result<(), String> {
        for action in Action::ALL {
            let chords = &self.chords[action.index()];
            for (left_index, left) in chords.iter().enumerate() {
                if chords
                    .iter()
                    .skip(left_index + 1)
                    .any(|right| left.overlaps(right))
                {
                    return Err(diagnostic(format_args!(
                        "keybinding action `{}` contains duplicate chords",
                        action.id()
                    )));
                }
            }
        }
        for (left_index, left_action) in Action::ALL.into_iter().enumerate() {
            for right_action in Action::ALL.into_iter().skip(left_index + 1) {
                if self.chords[left_action.index()].iter().any(|left| {
                    self.chords[right_action.index()]
                        .iter()
                        .any(|right| left.overlaps(right))
                }) {
                    return Err(diagnostic(format_args!(
                        "keybinding actions `{}` and `{}` have conflicting chords",
                        left_action.id(),
                        right_action.id()
                    )));
                }
            }
        }
        Ok(())
    }
}

fn parse_chord(raw: &str) -> Result<Chord, &'static str> {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Err("chord is empty");
    }
    let normalized = trimmed.to_ascii_lowercase();
    let parts = split_chord(&normalized)?;
    let (key_name, modifier_names) = parts.split_last().ok_or("chord does not contain a key")?;
    let mut modifiers = KeyModifiers::NONE;
    let mut seen = HashSet::new();
    for name in modifier_names {
        let modifier = match name.as_str() {
            "ctrl" | "control" => KeyModifiers::CONTROL,
            "alt" | "option" => KeyModifiers::ALT,
            "shift" => KeyModifiers::SHIFT,
            _ => return Err("only Ctrl, Alt, and Shift modifiers are supported"),
        };
        if !seen.insert(modifier.bits()) {
            return Err("a modifier is repeated");
        }
        modifiers.insert(modifier);
    }

    let code = parse_key(key_name)?;
    validate_chord(&code, modifiers)?;
    let label = format_label(&code, modifiers);
    Ok(Chord::exact(code, modifiers, &label))
}

fn split_chord(normalized: &str) -> Result<Vec<String>, &'static str> {
    if normalized.contains('+') {
        if let Some(prefix) = normalized.strip_suffix("++") {
            let mut parts = split_separated_modifiers(prefix.trim_end_matches('+'), '+')?;
            parts.push("+".into());
            return Ok(parts);
        }
        let parts = normalized
            .split('+')
            .map(str::trim)
            .map(str::to_owned)
            .collect::<Vec<_>>();
        if parts.iter().any(String::is_empty) {
            return Err("chord contains an empty component");
        }
        return Ok(parts);
    }
    if normalized.contains('-') {
        return split_separated_modifiers(normalized, '-');
    }

    // Accept the compact spellings used by existing Wisp help, such as
    // `CtrlJ`, `AltEnter`, and `CtrlShiftUp`.
    let mut rest = normalized;
    let mut parts = Vec::new();
    loop {
        let prefix = ["control", "ctrl", "option", "shift", "alt"]
            .into_iter()
            .find(|prefix| rest.starts_with(prefix) && rest.len() > prefix.len());
        let Some(prefix) = prefix else { break };
        parts.push(prefix.to_owned());
        rest = &rest[prefix.len()..];
    }
    if rest.is_empty() {
        return Err("chord does not contain a key");
    }
    parts.push(rest.to_owned());
    Ok(parts)
}

fn split_separated_modifiers(
    normalized: &str,
    separator: char,
) -> Result<Vec<String>, &'static str> {
    let parts = normalized
        .split(separator)
        .map(str::trim)
        .map(str::to_owned)
        .collect::<Vec<_>>();
    if parts.iter().any(String::is_empty) {
        return Err("chord contains an empty component");
    }
    Ok(parts)
}

fn parse_key(name: &str) -> Result<KeyCode, &'static str> {
    let code = match name {
        "enter" | "return" => KeyCode::Enter,
        "pageup" | "pgup" => KeyCode::PageUp,
        "pagedown" | "pgdown" | "pgdn" => KeyCode::PageDown,
        "home" => KeyCode::Home,
        "end" => KeyCode::End,
        "up" => KeyCode::Up,
        "down" => KeyCode::Down,
        "left" => KeyCode::Left,
        "right" => KeyCode::Right,
        "esc" | "escape" => KeyCode::Esc,
        "tab" => KeyCode::Tab,
        "backtab" => KeyCode::BackTab,
        "backspace" => KeyCode::Backspace,
        "delete" | "del" => KeyCode::Delete,
        "space" => KeyCode::Char(' '),
        "plus" => KeyCode::Char('+'),
        "minus" => KeyCode::Char('-'),
        function
            if function.strip_prefix('f').is_some_and(|digits| {
                !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())
            }) =>
        {
            let number = function[1..]
                .parse::<u8>()
                .map_err(|_| "function key must be F1 through F12")?;
            if !(1..=12).contains(&number) {
                return Err("function key must be F1 through F12");
            }
            KeyCode::F(number)
        }
        character if character.len() == 1 && character.is_ascii() => {
            KeyCode::Char(character.as_bytes()[0] as char)
        }
        _ => return Err("key must be a supported named key or ASCII character"),
    };
    Ok(code)
}

fn validate_chord(code: &KeyCode, modifiers: KeyModifiers) -> Result<(), &'static str> {
    let control = modifiers.contains(KeyModifiers::CONTROL);
    let alt = modifiers.contains(KeyModifiers::ALT);
    match code {
        KeyCode::Esc => Err("Escape is reserved for recovery"),
        KeyCode::Char(character)
            if matches!(character.to_ascii_lowercase(), 'c' | 'g') && control =>
        {
            Err("Ctrl+C and Ctrl+G are reserved for recovery and help")
        }
        KeyCode::Char(character)
            if matches!(character.to_ascii_lowercase(), 'a' | 'e') && control =>
        {
            Err("Ctrl+A and Ctrl+E are reserved for editor navigation")
        }
        KeyCode::Char(_) if !control && !alt => {
            Err("printable keys require Ctrl or Alt so typing remains available")
        }
        KeyCode::Up
        | KeyCode::Down
        | KeyCode::Left
        | KeyCode::Right
        | KeyCode::Home
        | KeyCode::End
            if modifiers.is_empty() =>
        {
            Err("unmodified editor navigation keys are reserved")
        }
        KeyCode::Tab | KeyCode::BackTab | KeyCode::Backspace | KeyCode::Delete => {
            Err("editor editing keys are reserved")
        }
        _ => Ok(()),
    }
}

fn format_label(code: &KeyCode, modifiers: KeyModifiers) -> String {
    let mut parts = Vec::new();
    if modifiers.contains(KeyModifiers::CONTROL) {
        parts.push("Ctrl".into());
    }
    if modifiers.contains(KeyModifiers::ALT) {
        parts.push("Alt".into());
    }
    if modifiers.contains(KeyModifiers::SHIFT) {
        parts.push("Shift".into());
    }
    parts.push(match code {
        KeyCode::Enter => "Enter".into(),
        KeyCode::PageUp => "PgUp".into(),
        KeyCode::PageDown => "PgDn".into(),
        KeyCode::Home => "Home".into(),
        KeyCode::End => "End".into(),
        KeyCode::Up => "Up".into(),
        KeyCode::Down => "Down".into(),
        KeyCode::Left => "Left".into(),
        KeyCode::Right => "Right".into(),
        KeyCode::F(number) => format!("F{number}"),
        KeyCode::Char(' ') => "Space".into(),
        KeyCode::Char(character) => character.to_ascii_uppercase().to_string(),
        _ => unreachable!("validation only permits supported key codes"),
    });
    parts.join("+")
}

fn same_code(left: &KeyCode, right: &KeyCode) -> bool {
    match (left, right) {
        (KeyCode::Char(left), KeyCode::Char(right)) if left.is_ascii() && right.is_ascii() => {
            left.eq_ignore_ascii_case(right)
        }
        _ => left == right,
    }
}

fn safe_fragment(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if character.is_control() {
                '\u{fffd}'
            } else {
                character
            }
        })
        .take(80)
        .collect()
}

fn diagnostic(arguments: std::fmt::Arguments<'_>) -> String {
    arguments
        .to_string()
        .chars()
        .map(|character| {
            if character.is_control()
                || matches!(character, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
            {
                '\u{fffd}'
            } else {
                character
            }
        })
        .take(MAX_DIAGNOSTIC_CHARS)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key(code: KeyCode, modifiers: KeyModifiers) -> KeyEvent {
        KeyEvent::new(code, modifiers)
    }

    #[test]
    fn defaults_match_existing_actions_and_labels() {
        let bindings = Bindings::default();
        let expected = [
            (Action::Submit, KeyCode::Enter, KeyModifiers::NONE, "Enter"),
            (
                Action::AlternateSubmit,
                KeyCode::Enter,
                KeyModifiers::ALT,
                "Alt+Enter",
            ),
            (
                Action::RestoreQueue,
                KeyCode::Up,
                KeyModifiers::ALT,
                "Alt+Up",
            ),
            (
                Action::History,
                KeyCode::Char('r'),
                KeyModifiers::CONTROL,
                "Ctrl+R",
            ),
            (
                Action::ToggleTheme,
                KeyCode::Char('t'),
                KeyModifiers::CONTROL,
                "Ctrl+T",
            ),
            (Action::Browse, KeyCode::F(6), KeyModifiers::NONE, "F6"),
            (Action::PageUp, KeyCode::PageUp, KeyModifiers::NONE, "PgUp"),
            (
                Action::PageDown,
                KeyCode::PageDown,
                KeyModifiers::NONE,
                "PgDn",
            ),
        ];
        for (action, code, modifiers, label) in expected {
            assert_eq!(bindings.action(key(code, modifiers)), Some(action));
            assert_eq!(bindings.label(action), label);
        }
        assert_eq!(bindings.label(Action::Newline), "Shift+Enter / Ctrl+J");
    }

    #[test]
    fn default_control_aliases_preserve_current_modifier_matching() {
        let bindings = Bindings::default();
        assert_eq!(
            bindings.action(key(
                KeyCode::Char('J'),
                KeyModifiers::CONTROL | KeyModifiers::SHIFT
            )),
            Some(Action::Newline)
        );
        assert_eq!(
            bindings.action(key(
                KeyCode::Home,
                KeyModifiers::CONTROL | KeyModifiers::ALT
            )),
            Some(Action::Home)
        );
        assert_eq!(
            bindings.action(key(
                KeyCode::Char('r'),
                KeyModifiers::CONTROL | KeyModifiers::SHIFT
            )),
            None
        );
        assert_eq!(
            bindings.action(key(
                KeyCode::Char('t'),
                KeyModifiers::CONTROL | KeyModifiers::ALT
            )),
            None
        );
    }

    #[test]
    fn default_enter_aliases_preserve_editor_modifier_behavior() {
        let bindings = Bindings::default();
        assert_eq!(
            bindings.action(key(KeyCode::Enter, KeyModifiers::CONTROL)),
            Some(Action::Submit)
        );
        for modifiers in [
            KeyModifiers::SHIFT | KeyModifiers::ALT,
            KeyModifiers::CONTROL | KeyModifiers::SHIFT,
            KeyModifiers::CONTROL | KeyModifiers::ALT,
            KeyModifiers::CONTROL | KeyModifiers::SHIFT | KeyModifiers::ALT,
        ] {
            assert_eq!(
                bindings.action(key(KeyCode::Enter, modifiers)),
                Some(Action::Newline)
            );
        }
        assert_eq!(bindings.label(Action::Submit), "Enter");
        assert_eq!(bindings.label(Action::Newline), "Shift+Enter / Ctrl+J");
    }

    #[test]
    fn overrides_replace_defaults_and_missing_actions_inherit() {
        let bindings = Bindings::from_json(
            r#"{
                "prompt.submit": ["Ctrl+Enter"],
                "prompt.newline": ["ShiftEnter", "CtrlJ"],
                "history.open": ["Ctrl-R", "F4"],
                "theme.toggle": []
            }"#,
        )
        .unwrap();
        assert_eq!(
            bindings.action(key(KeyCode::Enter, KeyModifiers::NONE)),
            None
        );
        assert_eq!(
            bindings.action(key(KeyCode::Enter, KeyModifiers::CONTROL)),
            Some(Action::Submit)
        );
        assert_eq!(
            bindings.action(key(KeyCode::F(4), KeyModifiers::NONE)),
            Some(Action::History)
        );
        assert_eq!(bindings.label(Action::History), "Ctrl+R / F4");
        assert_eq!(bindings.label(Action::ToggleTheme), "Unbound");
        assert_eq!(
            bindings.action(key(KeyCode::F(6), KeyModifiers::NONE)),
            Some(Action::Browse)
        );
    }

    #[test]
    fn custom_chords_match_exact_modifiers() {
        let bindings =
            Bindings::from_json(r#"{"prompt.submit":["Ctrl+Enter"],"prompt.newline":["Ctrl+J"]}"#)
                .unwrap();
        assert_eq!(
            bindings.action(key(
                KeyCode::Enter,
                KeyModifiers::CONTROL | KeyModifiers::SHIFT
            )),
            None
        );
        assert_eq!(
            bindings.action(key(
                KeyCode::Char('j'),
                KeyModifiers::CONTROL | KeyModifiers::ALT
            )),
            None
        );
    }

    #[test]
    fn custom_overrides_remove_hidden_default_enter_aliases() {
        let bindings =
            Bindings::from_json(r#"{"prompt.submit":["F2"],"prompt.newline":["F3"]}"#).unwrap();
        for modifiers in [
            KeyModifiers::CONTROL,
            KeyModifiers::SHIFT,
            KeyModifiers::SHIFT | KeyModifiers::ALT,
            KeyModifiers::CONTROL | KeyModifiers::SHIFT,
            KeyModifiers::CONTROL | KeyModifiers::ALT,
            KeyModifiers::CONTROL | KeyModifiers::SHIFT | KeyModifiers::ALT,
        ] {
            assert_eq!(bindings.action(key(KeyCode::Enter, modifiers)), None);
        }
        assert_eq!(
            bindings.action(key(KeyCode::Enter, KeyModifiers::ALT)),
            Some(Action::AlternateSubmit)
        );
        assert_eq!(
            bindings.action(key(KeyCode::F(2), KeyModifiers::NONE)),
            Some(Action::Submit)
        );
        assert_eq!(
            bindings.action(key(KeyCode::F(3), KeyModifiers::NONE)),
            Some(Action::Newline)
        );
    }

    #[test]
    fn accepts_named_keys_aliases_and_canonicalizes_labels() {
        let bindings = Bindings::from_json(
            r#"{
                "prompt.submit":["control+return"],
                "prompt.newline":["option+space"],
                "history.open":["CtrlShiftF4"],
                "transcript.page_up":["alt+pageup"],
                "transcript.page_down":["AltPgDown"]
            }"#,
        )
        .unwrap();
        assert_eq!(bindings.label(Action::Submit), "Ctrl+Enter");
        assert_eq!(bindings.label(Action::Newline), "Alt+Space");
        assert_eq!(bindings.label(Action::History), "Ctrl+Shift+F4");
        assert_eq!(bindings.label(Action::PageUp), "Alt+PgUp");
        assert_eq!(bindings.label(Action::PageDown), "Alt+PgDn");

        let punctuation =
            Bindings::from_json(r#"{"prompt.submit":["Alt++"],"prompt.newline":["Alt+-"]}"#)
                .unwrap();
        assert_eq!(punctuation.label(Action::Submit), "Alt++");
        assert_eq!(punctuation.label(Action::Newline), "Alt+-");

        let letter_f =
            Bindings::from_json(r#"{"prompt.submit":["Ctrl+F"],"prompt.newline":["Alt+F"]}"#)
                .unwrap();
        assert_eq!(
            letter_f.action(key(KeyCode::Char('f'), KeyModifiers::CONTROL)),
            Some(Action::Submit)
        );
        assert_eq!(
            letter_f.action(key(KeyCode::Char('F'), KeyModifiers::ALT)),
            Some(Action::Newline)
        );
        assert_eq!(letter_f.label(Action::Submit), "Ctrl+F");
        assert_eq!(letter_f.label(Action::Newline), "Alt+F");
    }

    #[test]
    fn rejects_unbinding_required_actions_but_allows_optional_actions() {
        assert!(
            Bindings::from_json(r#"{"prompt.submit":[]}"#)
                .unwrap_err()
                .contains("cannot be unbound")
        );
        assert!(
            Bindings::from_json(r#"{"prompt.newline":[]}"#)
                .unwrap_err()
                .contains("cannot be unbound")
        );
        assert!(Bindings::from_json(r#"{"queue.restore":[]}"#).is_ok());
    }

    #[test]
    fn rejects_reserved_and_typing_keys() {
        for chord in [
            "Esc",
            "Ctrl+C",
            "Ctrl+Shift+G",
            "Ctrl+Alt+A",
            "Ctrl+E",
            "x",
            "Shift+X",
            "Up",
            "Backspace",
            "Tab",
            "Ctrl+Tab",
            "Ctrl+Shift+BackTab",
            "Alt+Backspace",
            "Shift+Delete",
        ] {
            let json = format!(r#"{{"theme.toggle":["{chord}"],"prompt.newline":["Ctrl+J"]}}"#);
            assert!(Bindings::from_json(&json).is_err(), "accepted {chord}");
        }
        assert!(
            Bindings::from_json(r#"{"theme.toggle":["Alt+X"],"prompt.newline":["Ctrl+J"]}"#)
                .is_ok()
        );
    }

    #[test]
    fn rejects_exact_and_control_alias_conflicts() {
        let exact = Bindings::from_json(
            r#"{"history.open":["F4"],"theme.toggle":["F4"],"prompt.newline":["Ctrl+J"]}"#,
        )
        .unwrap_err();
        assert!(exact.contains("history.open"));
        assert!(exact.contains("theme.toggle"));

        let alias =
            Bindings::from_json(r#"{"theme.toggle":["Ctrl+Alt+Up"],"prompt.newline":["Ctrl+J"]}"#)
                .unwrap_err();
        assert!(alias.contains("theme.toggle"));
        assert!(alias.contains("transcript.line_up"));

        let duplicate =
            Bindings::from_json(r#"{"history.open":["F4","f4"],"prompt.newline":["Ctrl+J"]}"#)
                .unwrap_err();
        assert!(duplicate.contains("duplicate"));
        assert!(duplicate.contains("history.open"));

        let submit_alias =
            Bindings::from_json(r#"{"prompt.alternate_submit":["Ctrl+Enter"]}"#).unwrap_err();
        assert!(submit_alias.contains("prompt.submit"));
        assert!(submit_alias.contains("prompt.alternate_submit"));

        let newline_alias =
            Bindings::from_json(r#"{"theme.toggle":["Ctrl+Shift+Enter"]}"#).unwrap_err();
        assert!(newline_alias.contains("prompt.newline"));
        assert!(newline_alias.contains("theme.toggle"));

        assert!(
            Bindings::from_json(
                r#"{"prompt.submit":["F2"],"prompt.alternate_submit":["Ctrl+Enter"]}"#
            )
            .is_ok()
        );
    }

    #[test]
    fn rejects_unknown_ids_wrong_shapes_and_malformed_chords() {
        for json in [
            "[]",
            r#"{"unknown.action":["F4"]}"#,
            r#"{"history.open":"F4"}"#,
            r#"{"history.open":[4]}"#,
            r#"{"history.open":["Ctrl++R"]}"#,
            r#"{"history.open":["Super+R"]}"#,
            r#"{"history.open":["F13"]}"#,
            r#"{"history.open":["Ctrl+Ctrl+R"]}"#,
            r#"{"history.open":["F3"],"history.open":["F4"]}"#,
        ] {
            assert!(Bindings::from_json(json).is_err(), "accepted {json}");
        }
    }

    #[test]
    fn enforces_configuration_chord_and_diagnostic_bounds() {
        let oversized = " ".repeat(MAX_CONFIG_BYTES + 1);
        assert!(
            Bindings::from_json(&oversized)
                .unwrap_err()
                .contains("bytes")
        );

        let too_many = (0..=MAX_CHORDS_PER_ACTION)
            .map(|index| format!(r#""Alt+{index}""#))
            .collect::<Vec<_>>()
            .join(",");
        assert!(
            Bindings::from_json(&format!(r#"{{"theme.toggle":[{too_many}]}}"#))
                .unwrap_err()
                .contains("more than")
        );

        let long = "x".repeat(MAX_CHORD_CHARS + 1);
        assert!(
            Bindings::from_json(&format!(r#"{{"theme.toggle":["Alt+{long}"]}}"#))
                .unwrap_err()
                .contains("longer")
        );

        let unsafe_id = format!("{}\u{1b}[2J", "x".repeat(600));
        let error = Bindings::from_json(&format!(r#"{{"{unsafe_id}":[]}}"#)).unwrap_err();
        assert!(error.chars().count() <= MAX_DIAGNOSTIC_CHARS);
        assert!(!error.contains('\u{1b}'));

        let too_many_entries = (0..=MAX_CONFIG_ENTRIES)
            .map(|index| format!(r#""unknown.{index}":[]"#))
            .collect::<Vec<_>>()
            .join(",");
        assert!(
            Bindings::from_json(&format!("{{{too_many_entries}}}"))
                .unwrap_err()
                .contains("entries")
        );
    }

    #[test]
    fn help_entries_are_stable_and_include_unbound_actions() {
        let bindings = Bindings::from_json(r#"{"theme.toggle":[]}"#).unwrap();
        let entries = bindings.help_entries();
        assert_eq!(entries.len(), Action::ALL.len());
        assert_eq!(entries[0].id, "prompt.submit");
        assert_eq!(entries[0].label, "Enter");
        assert_eq!(entries[5].action, Action::ToggleTheme);
        assert_eq!(entries[5].label, "Unbound");
        assert!(!entries[5].description.is_empty());
    }
}
