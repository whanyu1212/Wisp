use crate::theme::{Palette, mix, ramp};
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};

const ADAL_BACKGROUND: Color = Color::Rgb(255, 88, 152);

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) enum LogoChoice {
    #[default]
    Random,
    Classic,
    AdalBraille,
    AdalMarkBraille,
    WispBraille,
}

impl LogoChoice {
    pub(crate) const ALL: [Self; 5] = [
        Self::Random,
        Self::Classic,
        Self::WispBraille,
        Self::AdalBraille,
        Self::AdalMarkBraille,
    ];

    const NAMED: [Self; 4] = [
        Self::Classic,
        Self::WispBraille,
        Self::AdalBraille,
        Self::AdalMarkBraille,
    ];

    pub(crate) const fn name(self) -> &'static str {
        match self {
            Self::Random => "random",
            Self::Classic => "classic",
            Self::AdalBraille => "adal-braille",
            Self::AdalMarkBraille => "adal-mark-braille",
            Self::WispBraille => "wisp-braille",
        }
    }

    pub(crate) const fn label(self) -> &'static str {
        match self {
            Self::Random => "Random",
            Self::Classic => "Classic WISP",
            Self::AdalBraille => "Adal Braille",
            Self::AdalMarkBraille => "Adal Mark Braille",
            Self::WispBraille => "Wisp Braille",
        }
    }

    pub(crate) fn resolve(value: &str) -> Option<Self> {
        Self::ALL
            .into_iter()
            .find(|choice| choice.name().eq_ignore_ascii_case(value.trim()))
    }

    pub(crate) fn choose_random() -> Self {
        static PROCESS_LOGO: OnceLock<LogoChoice> = OnceLock::new();
        *PROCESS_LOGO.get_or_init(|| {
            let time = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_or(0, |duration| duration.as_nanos() as usize);
            Self::NAMED[time % Self::NAMED.len()]
        })
    }
}

#[derive(Clone, Copy)]
struct LogoSize {
    rows: &'static [&'static str],
    width: usize,
}

#[derive(Clone, Copy)]
struct LogoArt {
    mini: LogoSize,
    normal: LogoSize,
    full: LogoSize,
    treatment: ColorTreatment,
}

#[derive(Clone, Copy)]
enum ColorTreatment {
    ThemeForeground,
    AdalForeground,
}

const CLASSIC: [&str; 5] = [
    "█   █  ███  ████  ████",
    "█   █   █   █     █  █",
    "█ █ █   █   ████  ████",
    "██ ██   █      █  █   ",
    "█   █  ███  ████  █   ",
];

const ADAL_BRAILLE_MINI: [&str; 5] = [
    "⠀⠀⣠⣴⣾⣿⣿⣿⣿⣿⣷⣄",
    "⢠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄",
    "⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿",
    "⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟",
    "⠀⠀⠉⠻⣿⣿⣿⣿⣿⠟⠉",
];
const ADAL_BRAILLE_NORMAL: [&str; 10] = [
    "⠀⠀⠀⠀⠀⠀⢀⣀⣤⣶⣿⣿⣿⣿⣶⣤⣀",
    "⠀⠀⠀⢀⣴⣿⣿⣿⣿⣿⠟⣩⣿⣿⣿⣿⣷⣄",
    "⠀⢀⣴⣿⣿⣿⣿⣿⣫⣴⣿⣿⣿⠿⣿⣿⣿⣿⡄",
    "⢠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠁⢀⣤⣤⡀⠘⣿⡇",
    "⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠿⠿⠿⠃⠀⢸⠁",
    "⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣆⠀⠀⠀⠀⠀⢸",
    "⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⡀⠀⠀⣠⠟",
    "⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⢀⡾⠋",
    "⠀⠀⠉⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⡾⠁",
    "⠀⠀⠀⠀⠀⠀⠉⠛⠿⣿⣿⣿⣿⣿⠏",
];
const ADAL_BRAILLE_FULL: [&str; 19] = [
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⣤⣤⣶⣶⣾⣿⣿⣿⣿⣶⣶⣦⣄⡀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⢀⣠⣴⣾⣿⣿⣿⣿⣿⣿⣿⣿⠿⢛⣽⣿⣿⣿⣿⣿⣷⣄",
    "⠀⠀⠀⠀⠀⠀⣀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠋⢁⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡄",
    "⠀⠀⠀⠀⢀⣼⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⢁⣠⣤⣾⣿⡿⠿⠿⣿⣿⣿⣿⡿⣿⣿⣿⡄",
    "⠀⠀⠀⢠⣿⣿⣿⣿⣿⣿⣿⣿⠟⣁⣤⣾⣿⣿⣿⣿⣿⠁⠀⠀⠀⠀⠉⠙⢻⡈⢻⣿⡇",
    "⠀⠀⢠⣿⣿⣿⣿⣿⣿⣿⣫⣴⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⢀⡤⠖⠚⠛⠛⠉⠀⠀⢿⡧⠄",
    "⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠁⠀⠁⠀⣀⣠⣤⡶⠾⠃⠀⠈⠑⠦⠤⢤⡀",
    "⠀⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠀⠠⠽⠿⠿⣟⣗⣹⠤⠄⠀⠀⠀⠀⠀⢠⠃",
    "⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⡀",
    "⢀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⠄⡇",
    "⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⡇",
    "⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢱",
    "⣸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡦⣄⡀⠀⠀⠀⠀⠀⠀⣀⣠⠤⠚",
    "⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⠀⠀⠀⠀⣴⡶⠟⠋⠉",
    "⠻⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡽⣿⣿⣿⣿⣿⣧⠀⠀⢰⡿⠁",
    "⠀⠀⠀⠉⠙⠻⠿⣿⣿⡿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠘⣿⣿⣿⣿⣿⣧⠀⢸⠃",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠈⠁⠈⠛⠿⠿⣿⣿⣿⣿⣿⣿⣧⠈⢻⣿⣿⣿⣿⣧⠏",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠉⠛⠛⠻⠆⠀⠹⣿⣿⣿⣿⣇",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠉⠙⠛⠂",
];

const ADAL_MARK_BRAILLE_MINI: [&str; 5] = [
    "⠀⠀⠀⣠⣾⣿⣿⣷⣄",
    "⠀⠀⣰⣿⣿⠏⣿⣿⣆",
    "⠀⣰⣿⣿⠃⠀⠘⣿⣿⣆",
    "⣰⣿⣿⣷⣶⣶⣶⣾⣿⣿⣆",
    "⣿⣿⠟⠉⠀⠀⠉⠻⣿⣿",
];
const ADAL_MARK_BRAILLE_NORMAL: [&str; 10] = [
    "⠀⠀⠀⠀⢀⣾⣿⣿⣿⣿⣷⡀",
    "⠀⠀⠀⢀⣾⣿⣿⣿⣿⣿⣿⣷⡀",
    "⠀⠀⠀⣾⣿⣿⣿⠏⠹⣿⣿⣿⣷",
    "⠀⠀⣾⣿⣿⣿⡟⠀⠀⢻⣿⣿⣿⣷",
    "⠀⣾⣿⣿⣿⡟⠀⠀⠀⠀⢻⣿⣿⣿⣷",
    "⣰⣿⣿⣿⣿⠁⠀⢀⡀⠀⠈⣿⣿⣿⣿⣆",
    "⣿⣿⣿⣿⠃⣠⣾⣿⣿⣷⣄⠘⣿⣿⣿⣿",
    "⣿⣿⣿⣿⣾⣿⣿⡿⢿⣿⣿⣷⣿⣿⣿⣿",
    "⣿⣿⣿⣿⣿⠟⠉⠀⠀⠉⠻⣿⣿⣿⣿⣿",
    "⣿⣿⣿⠟⠁⠀⠀⠀⠀⠀⠀⠀⠙⢿⣿⣿⣿",
];
const ADAL_MARK_BRAILLE_FULL: [&str; 16] = [
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣾⣿⣿⣿⣿⣿⣿⣿⣿⣷⡀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣰⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣆",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⢰⣿⣿⣿⣿⣿⣿⣿⠏⠹⣿⣿⣿⣿⣿⣿⣿⡆",
    "⠀⠀⠀⠀⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⣿⣿⡏⠀⠀⢹⣿⣿⣿⣿⣿⣿⣿⡄",
    "⠀⠀⠀⠀⠀⠀⠀⢀⣿⣿⣿⣿⣿⣿⣿⡟⠀⠀⠀⠀⢻⣿⣿⣿⣿⣿⣿⣿⡀",
    "⠀⠀⠀⠀⠀⠀⢀⣾⣿⣿⣿⣿⣿⣿⡟⠀⠀⠀⠀⠀⠀⢻⣿⣿⣿⣿⣿⣿⣷⡀",
    "⠀⠀⠀⠀⠀⠀⣼⣿⣿⣿⣿⣿⣿⡿⠁⠀⠀⠀⠀⠀⠀⠈⢿⣿⣿⣿⣿⣿⣿⣧",
    "⠀⠀⠀⠀⠀⣼⣿⣿⣿⣿⣿⣿⣿⠁⠀⠀⠀⢀⡀⠀⠀⠀⠈⣿⣿⣿⣿⣿⣿⣿⣧",
    "⠀⠀⠀⠀⣰⣿⣿⣿⣿⣿⣿⣿⠃⠀⢀⣤⣾⣿⣿⣷⣤⡀⠀⠘⣿⣿⣿⣿⣿⣿⣿⣆",
    "⠀⠀⠀⢰⣿⣿⣿⣿⣿⣿⣿⢇⣠⣶⣿⣿⣿⣿⣿⣿⣿⣿⣶⣄⡸⣿⣿⣿⣿⣿⣿⣿⡆",
    "⠀⠀⢠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄",
    "⠀⢀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠉⠀⠀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡀",
    "⢀⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠋⠀⠀⠀⠀⠀⠀⠀⠀⠙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡀",
    "⣼⣿⣿⣿⣿⣿⣿⣿⣿⠿⠋⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⠿⣿⣿⣿⣿⣿⣿⣿⣿⣧",
];

// Keep the face hollow and the eyes distinct at every size. Glass reflections
// and background glow are omitted so the mark also reads in monochrome.
#[rustfmt::skip]
const WISP_BRAILLE_MINI: [&str; 5] = [
    "⠀⠀⠀⠀⢸⣭⡀",
    "⠀⢀⠤⠶⠶⣿⣿⡄",
    "⢀⡖⠋⠉⠛⢮⠛",
    "⢸⢸⡇⢸⡇⢸⠆",
    "⠈⠳⢤⣬⠵⠋",
];

const WISP_BRAILLE_NORMAL: [&str; 10] = [
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣞⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⣿⣷⣄",
    "⠀⠀⠀⠀⣀⣀⣀⣀⠀⢀⣠⣴⣴⣿⣿⠿⣧",
    "⠀⣄⢿⡿⠉⢙⣿⣿⣿⣿⣿⣿⣿⣿⣁⣠⡿",
    "⣾⠃⠀⠀⢀⣼⠟⠋⠉⠀⠀⠉⠙⢿⣿⣿⠱⣦⡀",
    "⢻⡇⠀⠀⣼⠇⣠⡀⠀⠀⠀⠀⠀⠀⢹⣿⣦⠈⠻⣆",
    "⠀⠙⢦⡀⣿⠀⣿⠇⠀⢀⣾⡇⠀⠀⢸⡿⠈⠀⠀⢹",
    "⠀⠀⠀⠙⠻⣧⠀⠀⠀⠀⠉⠀⠀⢀⣾⠇⠀⠀⢀⣼",
    "⠀⠀⠀⠀⠀⠙⠿⣦⣄⣀⣀⣤⡶⠿⠥⠄⣶⣶⡝⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠉⠉⠉⠁⠀⠀⠀⠀⠉⠉",
];

const WISP_BRAILLE_FULL: [&str; 16] = [
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣴⠖⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣆",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⣿⣷⣄",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣷⣄",
    "⠀⠀⠀⠀⠀⠀⠀⣀⣀⣀⣀⣀⣀⣀⠀⠀⢀⣀⣤⢞⣡⣼⣿⣿⣿⠟⠻⣿⡆",
    "⠀⠀⠀⢰⣿⣿⡆⠙⠛⠩⠭⣭⣥⣶⣾⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⠀⠀⣾⡇",
    "⠀⣠⡷⠘⠻⠟⠃⠀⠀⢠⣾⣿⣿⣿⣿⠿⠿⠿⢿⣿⣿⣿⣿⣦⣄⣤⣾⡿⠁",
    "⢰⣿⠁⠀⠀⠀⠀⠀⠀⣼⣿⠟⠋⠁⠀⠀⠀⠀⠀⠀⠉⠛⢿⣿⣿⣿⣿⠡⣶⣄",
    "⢸⣧⠀⠀⠀⠀⠀⢀⣾⡟⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⣿⣿⡀⠀⠙⢿⣦",
    "⠈⣿⡄⠀⠀⠀⠀⣼⡿⠀⠀⣠⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠸⣿⣿⣷⡄⠀⠀⠙⣷⡄",
    "⠀⠈⢿⣆⠀⠀⠀⣿⡇⠀⣾⣿⡿⠀⠀⠀⠀⣠⣴⡄⠀⠀⠀⠀⠀⣿⣿⠉⠃⠀⠀⠀⠘⣿",
    "⠀⠀⠀⠙⢷⣄⡀⣿⡇⠀⠻⠟⠁⠀⠀⠀⢠⣿⣿⠇⠀⠀⠀⠀⠀⣿⡏⠀⠀⠀⠀⠀⠀⢸",
    "⠀⠀⠀⠀⠀⠙⠧⠹⣿⡄⠀⠀⠀⠀⠀⠀⠀⠙⠉⠀⠀⠀⠀⠀⣼⣿⠁⠀⠀⠀⠀⠀⠀⣼",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⣆⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⣾⡿⠁⠀⠀⠀⠀⠀⣠⣾⠟",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠻⢿⣦⣤⣀⣀⣀⣀⣀⣠⣴⣾⣿⣯⣤⣴⠀⢰⣿⣿⣷⠈⠁",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠙⠛⠛⠿⠟⠛⠋⠉⠀⠀⠀⠀⠀⠀⠘⠿⠿⠟",
];

fn art(choice: LogoChoice) -> LogoArt {
    match choice {
        LogoChoice::Random | LogoChoice::Classic => LogoArt {
            mini: LogoSize {
                rows: &CLASSIC,
                width: 22,
            },
            normal: LogoSize {
                rows: &CLASSIC,
                width: 22,
            },
            full: LogoSize {
                rows: &CLASSIC,
                width: 22,
            },
            treatment: ColorTreatment::ThemeForeground,
        },
        LogoChoice::AdalBraille => LogoArt {
            mini: LogoSize {
                rows: &ADAL_BRAILLE_MINI,
                width: 13,
            },
            normal: LogoSize {
                rows: &ADAL_BRAILLE_NORMAL,
                width: 19,
            },
            full: LogoSize {
                rows: &ADAL_BRAILLE_FULL,
                width: 37,
            },
            treatment: ColorTreatment::AdalForeground,
        },
        LogoChoice::AdalMarkBraille => LogoArt {
            mini: LogoSize {
                rows: &ADAL_MARK_BRAILLE_MINI,
                width: 11,
            },
            normal: LogoSize {
                rows: &ADAL_MARK_BRAILLE_NORMAL,
                width: 17,
            },
            full: LogoSize {
                rows: &ADAL_MARK_BRAILLE_FULL,
                width: 36,
            },
            treatment: ColorTreatment::AdalForeground,
        },
        LogoChoice::WispBraille => LogoArt {
            mini: LogoSize {
                rows: &WISP_BRAILLE_MINI,
                width: 8,
            },
            normal: LogoSize {
                rows: &WISP_BRAILLE_NORMAL,
                width: 20,
            },
            full: LogoSize {
                rows: &WISP_BRAILLE_FULL,
                width: 34,
            },
            treatment: ColorTreatment::ThemeForeground,
        },
    }
}

fn fitting_size(art: LogoArt, width: usize, height: usize) -> Option<LogoSize> {
    [art.full, art.normal, art.mini]
        .into_iter()
        .find(|size| size.width <= width && size.rows.len() <= height)
}

/// How many colours a gradient logo steps through, so a row stays a few spans.
const GRADIENT_STEPS: f64 = 12.0;

/// How a logo is coloured: one colour, or a gradient through three stops.
#[derive(Clone, Copy, Debug, PartialEq)]
enum Paint {
    Flat(Color),
    Gradient([Color; 3]),
}

/// The logo's paint. Where a gradient can show, theme logos run through the heading
/// colours and the Adal marks through their pink, lighter at the tip and deeper at the
/// far end; elsewhere they keep one colour.
fn paint(art: LogoArt, palette: Palette, no_color: bool) -> Paint {
    let monochrome = no_color || palette.is_monochrome();
    let gradient = !monochrome && palette.shows_gradients();
    match art.treatment {
        ColorTreatment::ThemeForeground if gradient => {
            Paint::Gradient([palette.heading_1, palette.heading_3, palette.heading_2])
        }
        ColorTreatment::ThemeForeground => Paint::Flat(palette.primary),
        ColorTreatment::AdalForeground if monochrome => Paint::Flat(palette.primary),
        ColorTreatment::AdalForeground if gradient => Paint::Gradient([
            mix(ADAL_BACKGROUND, palette.foreground, 0.65),
            ADAL_BACKGROUND,
            mix(ADAL_BACKGROUND, palette.background, 0.7),
        ]),
        ColorTreatment::AdalForeground => Paint::Flat(ADAL_BACKGROUND),
    }
}

/// The colour of the `Wisp` name under the logo: the gradient's tip colour, or the
/// theme's primary where the logo is flat.
pub(crate) fn name_color(choice: LogoChoice, palette: Palette, no_color: bool) -> Color {
    match paint(art(choice), palette, no_color) {
        Paint::Gradient([tip, ..]) => tip,
        Paint::Flat(_) => palette.primary,
    }
}

fn is_ink(cell: char) -> bool {
    !matches!(cell, ' ' | '\u{2800}')
}

/// Frames the entrance's gather takes at the 125 ms activity tick: 1 s.
pub(crate) const GATHER_FRAMES: u8 = 8;
/// Frames the sheen after it takes: 1 s. Each step moves the band less than its width,
/// so it sweeps across the logo instead of jumping.
pub(crate) const SHEEN_FRAMES: u8 = 8;
/// Half the sheen band's width, in the gradient's 0 to 1 distance from the tip.
const SHEEN_HALF_WIDTH: f64 = 0.15;

/// How much of the welcome logo's entrance a frame shows.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) enum Entrance {
    /// The finished logo.
    #[default]
    Finished,
    /// The entrance at `frame`, counted from 0. The logo's dots gather over the first
    /// [`GATHER_FRAMES`]; then, with `sheen`, a light band crosses it once from the tip
    /// over [`SHEEN_FRAMES`].
    Playing { frame: u8, sheen: bool },
}

impl Entrance {
    /// How many frames an entrance plays, with or without its sheen.
    pub(crate) const fn length(sheen: bool) -> u8 {
        if sheen {
            GATHER_FRAMES + SHEEN_FRAMES
        } else {
            GATHER_FRAMES
        }
    }

    /// The share of dots this frame shows, from a sixth on the first frame to all.
    fn gathered(self) -> f64 {
        match self {
            Self::Finished => 1.0,
            Self::Playing { frame, .. } => {
                (f64::from(frame) + 1.0).min(f64::from(GATHER_FRAMES)) / f64::from(GATHER_FRAMES)
            }
        }
    }

    /// Where the sheen band's centre is on this frame, along the gradient's 0 to 1
    /// distance from the tip, while it crosses the logo.
    fn sheen_centre(self) -> Option<f64> {
        let Self::Playing { frame, sheen: true } = self else {
            return None;
        };
        let step = frame
            .checked_sub(GATHER_FRAMES)
            .filter(|step| *step < SHEEN_FRAMES)?;
        let travel = 1.0 + 2.0 * SHEEN_HALF_WIDTH;
        Some(-SHEEN_HALF_WIDTH + travel * (f64::from(step) + 0.5) / f64::from(SHEEN_FRAMES))
    }
}

/// A fixed threshold in [0, 1) for one dot of a cell, so every launch gathers the dots
/// in the same scattered order. Dot 8 stands for a whole character with no dots.
fn gather_threshold(row: usize, column: usize, dot: u32) -> f64 {
    let mut hash = (row as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15)
        ^ (column as u64).wrapping_mul(0xC2B2_AE3D_27D4_EB4F)
        ^ u64::from(dot).wrapping_mul(0x1656_67B1_9E37_79F9);
    hash ^= hash >> 33;
    hash = hash.wrapping_mul(0xFF51_AFD7_ED55_8CCD);
    hash ^= hash >> 33;
    (hash >> 11) as f64 / (1_u64 << 53) as f64
}

/// The part of `cell` a gather at `progress` shows: the braille dots whose threshold it
/// has passed, or, for a character without dots, all of it or none.
fn gathered_cell(cell: char, row: usize, column: usize, progress: f64) -> char {
    if progress >= 1.0 {
        return cell;
    }
    let braille = u32::from(cell)
        .checked_sub(0x2800)
        .filter(|dots| *dots <= 0xFF);
    match braille {
        Some(dots) => {
            let kept = (0..8)
                .filter(|dot| dots & (1 << dot) != 0)
                .filter(|dot| gather_threshold(row, column, *dot) < progress)
                .fold(0, |kept, dot| kept | (1 << dot));
            char::from_u32(0x2800 + kept).unwrap_or(cell)
        }
        None if is_ink(cell) && gather_threshold(row, column, 8) >= progress => ' ',
        None => cell,
    }
}

/// The lighter of the theme's foreground and background, which the sheen blends toward:
/// a bright band on a dark theme, a pale one on a light theme.
fn sheen_tone(palette: Palette) -> Option<Color> {
    let luminance = |color| match color {
        Color::Rgb(red, green, blue) => {
            Some(0.2126 * f64::from(red) + 0.7152 * f64::from(green) + 0.0722 * f64::from(blue))
        }
        _ => None,
    };
    let (foreground, background) = (
        luminance(palette.foreground)?,
        luminance(palette.background)?,
    );
    Some(if foreground >= background {
        palette.foreground
    } else {
        palette.background
    })
}

pub(crate) fn preview_lines(
    choice: LogoChoice,
    width: u16,
    height: u16,
    palette: Palette,
    no_color: bool,
    entrance: Entrance,
) -> Vec<Line<'static>> {
    let width = usize::from(width);
    let art = art(choice);
    let Some(size) = fitting_size(art, width, usize::from(height)) else {
        return Vec::new();
    };
    let rows = size
        .rows
        .iter()
        .map(|row| row.chars().collect::<Vec<_>>())
        .collect::<Vec<_>>();
    let ink = || {
        rows.iter().enumerate().flat_map(|(row, cells)| {
            cells
                .iter()
                .enumerate()
                .filter(|(_, cell)| is_ink(**cell))
                .map(move |(column, _)| (row, column))
        })
    };
    let left = width.saturating_sub(size.width) / 2;
    // The gradient starts at the top row's last ink, the Wisp flame's tip, and reaches
    // its last stop at the farthest ink. A cell is about twice as tall as it is wide.
    let tip = ink().next().map_or((0, 0), |(top, _)| {
        let column = rows[top].iter().rposition(|cell| is_ink(*cell));
        (top, column.unwrap_or(0))
    });
    let distance = |row: usize, column: usize| {
        (row.abs_diff(tip.0) as f64).hypot(column.abs_diff(tip.1) as f64 / 2.0)
    };
    let farthest = ink()
        .map(|(row, column)| distance(row, column))
        .fold(0.0, f64::max)
        .max(1.0);
    let paint = paint(art, palette, no_color);
    let progress = entrance.gathered();
    let sheen = entrance
        .sheen_centre()
        .and_then(|centre| Some((centre, sheen_tone(palette)?)));
    let color = |row: usize, column: usize| {
        let along = (distance(row, column) / farthest * GRADIENT_STEPS).round() / GRADIENT_STEPS;
        let color = match paint {
            Paint::Flat(color) => color,
            Paint::Gradient(stops) => ramp(&stops, along),
        };
        // The band lightens each cell's own colour, most at its centre.
        match sheen {
            Some((centre, tone)) if (along - centre).abs() < SHEEN_HALF_WIDTH => {
                let strength = 0.6 * (1.0 - (along - centre).abs() / SHEEN_HALF_WIDTH);
                mix(tone, color, strength)
            }
            _ => color,
        }
    };
    rows.iter()
        .enumerate()
        .map(|(row, cells)| {
            let mut spans = vec![Span::raw(" ".repeat(left))];
            // Runs of one colour make one span; blank cells join whichever run they are in.
            let mut run = String::new();
            let mut run_color = None;
            for (column, cell) in cells.iter().enumerate() {
                let shown = gathered_cell(*cell, row, column, progress);
                let cell_color = is_ink(shown).then(|| color(row, column));
                if let (Some(current), Some(next)) = (run_color, cell_color) {
                    if current != next {
                        let text = std::mem::take(&mut run);
                        spans.push(Span::styled(text, Style::default().fg(current)));
                    }
                }
                run_color = cell_color.or(run_color);
                run.push(shown);
            }
            if !run.is_empty() {
                let color = run_color.unwrap_or(match paint {
                    Paint::Flat(color) => color,
                    Paint::Gradient([tip, ..]) => tip,
                });
                spans.push(Span::styled(run, Style::default().fg(color)));
            }
            Line::from(spans)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn all_logos_fit_each_supported_preview() {
        for choice in LogoChoice::ALL {
            for (width, height) in [(30, 8), (40, 14), (80, 30)] {
                let lines = preview_lines(
                    choice,
                    width,
                    height,
                    Palette::default(),
                    false,
                    Entrance::Finished,
                );
                assert!(!lines.is_empty(), "{} at {width}x{height}", choice.name());
                assert!(lines.len() <= usize::from(height));
                assert!(lines.iter().all(|line| line.width() <= usize::from(width)));
            }
        }
    }

    #[test]
    fn wisp_sizes_fit_exact_bounds_and_fall_back() {
        let palette = Palette::default();
        for (width, height, expected_rows) in [
            (34, 16, 16),
            (33, 16, 10),
            (34, 15, 10),
            (20, 10, 10),
            (19, 10, 5),
            (20, 9, 5),
            (8, 5, 5),
            (7, 5, 0),
            (8, 4, 0),
        ] {
            for no_color in [false, true] {
                let lines = preview_lines(
                    LogoChoice::WispBraille,
                    width,
                    height,
                    palette,
                    no_color,
                    Entrance::Finished,
                );
                assert_eq!(lines.len(), expected_rows, "{width}x{height}");
                for line in lines {
                    assert!(line.width() <= usize::from(width));
                }
            }
        }
    }

    /// The colours the logo's spans draw, after the leading padding.
    fn ink_colors(lines: &[Line<'static>]) -> Vec<Color> {
        lines
            .iter()
            .flat_map(|line| line.spans.iter().skip(1))
            .map(|span| {
                assert_eq!(span.style.bg, None, "{:?}", span.content);
                span.style.fg.unwrap()
            })
            .collect()
    }

    #[test]
    fn gradient_runs_tip_to_far_end() {
        let palette = Palette::default();
        let lines = preview_lines(
            LogoChoice::WispBraille,
            80,
            30,
            palette,
            false,
            Entrance::Finished,
        );
        // The flame's tip, the top row's last ink, takes the first stop.
        assert_eq!(
            lines[0].spans.last().unwrap().style.fg,
            Some(palette.heading_1)
        );
        let colors = ink_colors(&lines);
        assert!(colors.contains(&palette.heading_3), "{colors:?}");
        assert!(colors.contains(&palette.heading_2), "{colors:?}");
        // About a dozen steps, not one per cell.
        let mut distinct = colors.clone();
        distinct.dedup();
        distinct.sort_by_key(|color| format!("{color:?}"));
        distinct.dedup();
        assert!((8..=13).contains(&distinct.len()), "{distinct:?}");
    }

    #[test]
    fn gradient_follows_theme() {
        for theme in [crate::theme::default_theme(), crate::theme::paper_theme()] {
            let palette = theme.palette(false);
            for choice in [LogoChoice::WispBraille, LogoChoice::Classic] {
                let lines = preview_lines(choice, 80, 30, palette, false, Entrance::Finished);
                assert_eq!(
                    lines[0].spans.last().unwrap().style.fg,
                    Some(palette.heading_1),
                    "{} on {}",
                    choice.name(),
                    theme.slug
                );
                assert_eq!(name_color(choice, palette, false), palette.heading_1);
            }
        }
    }

    #[test]
    fn flat_without_gradients() {
        use crate::color_depth::ColorDepth;
        let palette = Palette::default();
        // 16 colours and monochrome keep one colour, the theme's primary as before.
        for (palette, no_color) in [
            (palette.for_depth(ColorDepth::Ansi16), false),
            (palette, true),
        ] {
            for choice in [LogoChoice::WispBraille, LogoChoice::Classic] {
                let colors = ink_colors(&preview_lines(
                    choice,
                    80,
                    30,
                    palette,
                    no_color,
                    Entrance::Finished,
                ));
                assert!(colors.iter().all(|color| *color == palette.primary));
                assert_eq!(name_color(choice, palette, no_color), palette.primary);
            }
        }
    }

    #[test]
    fn adal_marks_stay_pink() {
        use crate::color_depth::ColorDepth;
        let palette = Palette::default();
        for choice in [LogoChoice::AdalBraille, LogoChoice::AdalMarkBraille] {
            // Lighter pink at the tip, deeper toward the far end, never another hue.
            let colors = ink_colors(&preview_lines(
                choice,
                80,
                30,
                palette,
                false,
                Entrance::Finished,
            ));
            assert!(colors.contains(&ADAL_BACKGROUND), "{}", choice.name());
            for color in &colors {
                let Color::Rgb(red, green, blue) = *color else {
                    panic!("{color:?}");
                };
                assert!(red > green && red > blue, "{} {color:?}", choice.name());
            }
            let flat = preview_lines(
                choice,
                80,
                30,
                palette.for_depth(ColorDepth::Ansi16),
                false,
                Entrance::Finished,
            );
            assert!(
                ink_colors(&flat)
                    .iter()
                    .all(|color| *color == ADAL_BACKGROUND)
            );
            let monochrome = ink_colors(&preview_lines(
                choice,
                80,
                30,
                palette,
                true,
                Entrance::Finished,
            ));
            assert!(monochrome.iter().all(|color| *color == palette.primary));
        }
    }

    fn frame(choice: LogoChoice, entrance: Entrance) -> Vec<Line<'static>> {
        preview_lines(choice, 80, 30, Palette::default(), false, entrance)
    }

    /// Each cell's dots: braille dot bits, or 1 for any other ink character.
    fn dots(lines: &[Line<'static>]) -> Vec<u32> {
        lines
            .iter()
            .flat_map(|line| line.to_string().chars().collect::<Vec<_>>())
            .map(|cell| match u32::from(cell).checked_sub(0x2800) {
                Some(bits) if bits <= 0xFF => bits,
                _ => u32::from(is_ink(cell)),
            })
            .collect()
    }

    #[test]
    fn gather_builds_up_dots() {
        for choice in [LogoChoice::WispBraille, LogoChoice::Classic] {
            let finished = dots(&frame(choice, Entrance::Finished));
            let total = finished.iter().map(|bits| bits.count_ones()).sum::<u32>();
            let mut shown = vec![0; finished.len()];
            for step in 0..GATHER_FRAMES {
                let entrance = Entrance::Playing {
                    frame: step,
                    sheen: false,
                };
                let next = dots(&frame(choice, entrance));
                // Dots only arrive, each into its own place, and the same way every launch.
                for ((before, now), done) in shown.iter().zip(&next).zip(&finished) {
                    assert_eq!(before & now, *before, "{}", choice.name());
                    assert_eq!(now & done, *now, "{}", choice.name());
                }
                assert_eq!(next, dots(&frame(choice, entrance)));
                let count = next.iter().map(|bits| bits.count_ones()).sum::<u32>();
                let expected = f64::from(total) * f64::from(step + 1) / f64::from(GATHER_FRAMES);
                assert!(
                    (f64::from(count) - expected).abs() < f64::from(total) * 0.15,
                    "{} frame {step}: {count} of {total}",
                    choice.name()
                );
                shown = next;
            }
            // The gather's last frame is the finished logo.
            assert_eq!(shown, finished, "{}", choice.name());
        }
    }

    #[test]
    fn sheen_crosses_from_tip() {
        let finished = frame(LogoChoice::WispBraille, Entrance::Finished);
        let colors = |lines: &[Line<'static>]| -> Vec<Option<Color>> {
            lines
                .iter()
                .flat_map(|line| {
                    line.spans.iter().skip(1).flat_map(|span| {
                        span.content
                            .chars()
                            .map(move |cell| is_ink(cell).then_some(span.style.fg).flatten())
                    })
                })
                .collect()
        };
        let base = colors(&finished);
        // The cells each sheen frame lightens, by row: the band starts at the tip's row
        // and moves away from it.
        let mut first_rows = Vec::new();
        for step in 0..SHEEN_FRAMES {
            let entrance = Entrance::Playing {
                frame: GATHER_FRAMES + step,
                sheen: true,
            };
            let lit = frame(LogoChoice::WispBraille, entrance);
            assert_eq!(lit.len(), finished.len());
            let width = finished[0].width().max(1);
            let changed = colors(&lit)
                .iter()
                .zip(&base)
                .enumerate()
                .filter(|(_, (now, before))| now != before)
                .map(|(index, _)| index)
                .collect::<Vec<_>>();
            assert!(!changed.is_empty(), "step {step}");
            first_rows.push(changed.iter().map(|index| index / width).min().unwrap());
        }
        assert!(
            first_rows.windows(2).all(|pair| pair[0] <= pair[1]),
            "{first_rows:?}"
        );
        assert!(first_rows.first() < first_rows.last(), "{first_rows:?}");
        // Without the sheen, those frames are the finished logo.
        let plain = Entrance::Playing {
            frame: GATHER_FRAMES,
            sheen: false,
        };
        assert_eq!(frame(LogoChoice::WispBraille, plain), finished);
    }

    #[test]
    fn logo_names_round_trip() {
        for choice in LogoChoice::ALL {
            assert_eq!(LogoChoice::resolve(choice.name()), Some(choice));
        }
        assert_eq!(
            LogoChoice::resolve(" ADAL-BRAILLE "),
            Some(LogoChoice::AdalBraille)
        );
        assert_eq!(LogoChoice::resolve("adal-blocks"), None);
        assert_eq!(LogoChoice::resolve("adal-mark-blocks"), None);
        assert_eq!(LogoChoice::resolve("wisp-color"), None);
        assert_eq!(LogoChoice::resolve("unknown"), None);
    }

    #[test]
    fn random_logo_is_stable_for_the_process() {
        let selected = LogoChoice::choose_random();
        assert_ne!(selected, LogoChoice::Random);
        for _ in 0..LogoChoice::NAMED.len() * 2 {
            assert_eq!(LogoChoice::choose_random(), selected);
        }
    }
}
