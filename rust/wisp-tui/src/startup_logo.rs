use crate::theme::Palette;
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use std::sync::OnceLock;
use std::time::{SystemTime, UNIX_EPOCH};
use unicode_width::UnicodeWidthStr;

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

const WISP_BRAILLE_MINI: [&str; 5] = [
    "⠀⠀⠀⠐⠈⠈⢁⡅⠂⠠⡀",
    "⡐⠁⣀⢀⣠⣤⣤⡿⢳⠀⠀⠆",
    "⡇⠸⣅⣞⡉⠉⡉⠹⣯⡀⠀⢸",
    "⠱⡀⠈⠫⣁⣈⣁⣴⣣⡼⢀⠆",
    "⠀⠈⠐⠠⢀⣀⣀⡀⠄⠂⠁",
];

const WISP_BRAILLE_NORMAL: [&str; 10] = [
    "⠀⠀⠀⠀⠀⣠⠤⠒⠐⠒⠒⠐⠲⠤⣀",
    "⠀⠀⢀⠔⠉⠀⠀⠀⠀⠀⠀⢰⣎⠀⠀⠁⢢⡀",
    "⠀⡰⠁⠀⠀⠀⠀⠀⠀⠀⢀⢸⣿⣷⣄⠀⠀⠙⢆",
    "⢰⠁⠀⣠⣤⠤⣤⣤⣶⣾⣿⣿⡿⠋⣽⠀⠀⠀⠘⡄",
    "⡇⠀⣼⡉⠉⣴⠿⠋⠉⠉⠉⠛⢿⣿⡇⠄⠀⠀⠀⢷",
    "⣇⠀⠈⢷⣴⡏⣰⡆⠀⢀⡀⠀⠀⣿⡷⣄⠀⠀⠀⡿",
    "⠸⡀⠀⠀⠜⣧⠉⠁⠀⠺⠏⠀⢀⣿⠁⠈⣧⠀⢠⠃",
    "⠀⠱⣄⠀⠀⠘⠳⣤⣀⣀⣠⣴⣿⣵⣟⡶⠃⣠⠎",
    "⠀⠀⠈⠣⣀⠀⠀⠀⠉⠉⠁⠀⠀⠀⠉⡀⠜⠁",
    "⠀⠀⠀⠀⠀⠙⠒⠦⠤⠤⠤⠤⠴⠒⠉",
];

const WISP_BRAILLE_FULL: [&str; 22] = [
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣀⡀⠤⠤⠤⠤⠤⠄⣀⣀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⠤⠒⠉⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠑⠒⠤⣀",
    "⠀⠀⠀⠀⠀⠀⠀⠀⣠⠖⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣠⡤⠂⠀⠀⠀⠙⠢⣀",
    "⠀⠀⠀⠀⠀⠀⡠⠊⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣾⣿⡄⠠⠀⠀⠀⠀⠀⠈⠳⣄",
    "⠀⠀⠀⠀⣠⠎⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢻⣿⣿⣦⡀⠀⠀⠀⠀⠀⠀⠈⠳⡀",
    "⠀⠀⠀⡴⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣦⡀⠀⠀⠀⠀⠀⠀⠙⣄",
    "⠀⠀⡼⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⡴⢂⣰⣿⣿⣿⡿⢿⣿⡄⠀⠀⠀⠀⠀⠀⠘⣆",
    "⠀⣰⠃⠀⠀⠀⠀⢀⣀⡀⠀⠀⣀⣀⣀⣀⣤⣴⣶⣾⣿⣿⣾⣿⣿⣿⣿⠟⠀⠀⣽⡇⠀⠀⠀⠀⠀⠀⠀⠸⡄",
    "⢀⡏⠀⠀⠀⠀⠀⣿⣿⣿⠏⠉⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣅⡀⣀⣴⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⢳",
    "⢸⠁⠀⠀⠀⣰⠏⠉⠛⠉⠀⢠⣾⣿⡿⠟⠋⠉⠀⠀⠀⠈⠉⠛⠿⣿⣿⣿⣿⣿⠃⠀⡀⠀⠀⠀⠀⠀⠀⠀⢸⡄",
    "⢸⠀⠀⠀⠀⣿⡄⠀⠀⠀⢠⣿⡿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⢻⣿⣿⣿⠀⠀⠁⠀⠀⠀⠀⠀⠀⠀⠸⡇",
    "⢸⠀⠀⠀⠀⠘⣿⣆⠀⠀⣾⣿⠁⠀⢀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢹⣿⣿⣧⡀⠀⠀⠄⠀⠀⠀⠀⠀⢸⡇",
    "⢸⡄⠀⠀⠀⠀⠈⠻⣷⣦⣿⡇⠀⣴⣿⣷⠀⠀⠀⠀⢀⣠⡀⠀⠀⠀⠀⠀⣿⣿⠛⢿⣦⠀⠀⠀⠀⠀⠀⠀⢸⠃",
    "⠈⣇⠀⠀⠀⠀⠀⠀⠈⠹⣿⡇⠀⢿⡿⠃⠀⠀⠀⢀⣿⣿⡇⠀⠀⠀⠀⠀⣿⡟⠀⠀⠙⣷⡄⠀⠀⠀⠀⠀⡾",
    "⠀⢹⡄⠀⠀⠀⠀⠀⠰⠀⢻⣷⡀⠀⠀⠀⠀⠀⠀⠈⠻⠛⠀⠀⠀⠀⠀⣰⣿⠃⠀⠀⠀⠈⣿⠀⠀⠀⠀⣰⠃",
    "⠀⠀⢻⡀⠀⠀⠀⠀⠀⠀⠀⢻⣷⡄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣴⣿⠋⢀⣀⡀⠀⢠⡿⠀⠀⠀⢠⠏",
    "⠀⠀⠀⠻⡄⠀⠀⠀⠀⠀⠀⠀⠙⢿⣷⣄⣀⠀⠀⠀⠀⠀⢀⣠⣴⣿⣟⣁⣠⣿⡟⢻⣷⠋⠁⠀⠀⣰⠏",
    "⠀⠀⠀⠀⠙⢦⡀⠀⠀⠀⠀⠀⠀⠀⠈⠙⠻⠿⠿⣿⡿⠿⠟⠛⠉⠉⠉⠉⠉⠻⠿⠿⠃⠀⠀⢀⡼⠁",
    "⠀⠀⠀⠀⠀⠀⠙⢦⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⡴⠋",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠙⠦⣄⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣠⠖⠉",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠙⠲⠤⣄⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⡠⠤⠒⠉",
    "⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠉⠉⠒⠒⠒⠒⠒⠊⠉⠉",
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
                width: 12,
            },
            normal: LogoSize {
                rows: &WISP_BRAILLE_NORMAL,
                width: 20,
            },
            full: LogoSize {
                rows: &WISP_BRAILLE_FULL,
                width: 42,
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

pub(crate) fn preview_lines(
    choice: LogoChoice,
    width: u16,
    height: u16,
    palette: Palette,
    no_color: bool,
) -> Vec<Line<'static>> {
    let width = usize::from(width);
    let art = art(choice);
    let Some(size) = fitting_size(art, width, usize::from(height)) else {
        return Vec::new();
    };
    let monochrome = no_color || palette.is_monochrome();
    let style = match art.treatment {
        ColorTreatment::ThemeForeground => Style::default().fg(palette.primary),
        ColorTreatment::AdalForeground if monochrome => Style::default().fg(palette.primary),
        ColorTreatment::AdalForeground => Style::default().fg(ADAL_BACKGROUND),
    };
    size.rows
        .iter()
        .map(|row| {
            let row_width = row.width();
            let left = width.saturating_sub(size.width) / 2;
            let mut content = String::with_capacity(size.width);
            content.push_str(row);
            content.push_str(&" ".repeat(size.width.saturating_sub(row_width)));
            Line::from(vec![
                Span::raw(" ".repeat(left)),
                Span::styled(content, style),
            ])
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
                let lines = preview_lines(choice, width, height, Palette::default(), false);
                assert!(!lines.is_empty(), "{} at {width}x{height}", choice.name());
                assert!(lines.len() <= usize::from(height));
                assert!(lines.iter().all(|line| line.width() <= usize::from(width)));
            }
        }
    }

    #[test]
    fn adal_marks_use_transparent_pink() {
        let palette = Palette::default();
        for choice in [LogoChoice::AdalBraille, LogoChoice::AdalMarkBraille] {
            let lines = preview_lines(choice, 80, 30, palette, false);
            let mark = &lines[0].spans[1];
            assert_eq!(mark.style.fg, Some(ADAL_BACKGROUND), "{}", choice.name());
            assert_eq!(mark.style.bg, None, "{}", choice.name());

            let monochrome = preview_lines(choice, 80, 30, palette, true);
            let mark = &monochrome[0].spans[1];
            assert_eq!(mark.style.fg, Some(palette.primary), "{}", choice.name());
            assert_eq!(mark.style.bg, None, "{}", choice.name());
        }

        let classic = preview_lines(LogoChoice::Classic, 80, 30, palette, false);
        assert_eq!(classic[0].spans[1].style.fg, Some(palette.primary));
        assert_eq!(classic[0].spans[1].style.bg, None);
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
