//! Compact transcript labels. Keep raw bounded arguments separately for expansion.

use serde_json::{Map, Value};

use super::{
    GENERIC_VALUE_MAX_CHARS, ToolCallInput, clip_chars, generic_argument_items, mcp_subject,
    one_line,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Verb {
    Open,
    Read,
    Click,
    Type,
    Capture,
    Search,
    List,
    Propose,
    Load,
    Request,
    Call,
}

impl Verb {
    pub(super) fn forms(self) -> (&'static str, &'static str, &'static str) {
        match self {
            Self::Open => ("Opening", "Opened", "Open"),
            Self::Read => ("Reading", "Read", "Read"),
            Self::Click => ("Clicking", "Clicked", "Click"),
            Self::Type => ("Typing", "Typed", "Type"),
            Self::Capture => ("Capturing", "Captured", "Capture"),
            Self::Search => ("Searching", "Searched", "Search"),
            Self::List => ("Listing", "Listed", "List"),
            Self::Propose => ("Proposing", "Proposed", "Propose"),
            Self::Load => ("Loading", "Loaded", "Load"),
            Self::Request => ("Requesting", "Requested", "Request"),
            Self::Call => ("Calling", "Called", "Call"),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ActionLabel {
    pub verb: Verb,
    pub subject: String,
}

/// Both live and history projection can wrap a generic tool's arguments in a bounded record.
struct Args<'a> {
    items: Option<&'a Map<String, Value>>,
    omitted: bool,
}
impl<'a> Args<'a> {
    fn new(value: &'a Value) -> Self {
        let object = value.as_object();
        match object.and_then(generic_argument_items) {
            Some((items, omitted)) => Self {
                items: Some(items),
                omitted: omitted != 0,
            },
            None => Self {
                items: object,
                omitted: false,
            },
        }
    }
    fn get(&self, key: &str) -> Option<&Value> {
        self.items?.get(key)
    }
    fn text(&self, key: &str) -> Option<String> {
        let safe: String = one_line(&clip_chars(
            self.get(key)?.as_str()?,
            GENERIC_VALUE_MAX_CHARS,
        ))
        .chars()
        .filter(|c| !c.is_control() && !crate::is_bidi_control(*c))
        .collect();
        (!safe.is_empty()).then_some(safe)
    }
    fn named(&self, prefix: &str, key: &str) -> String {
        self.text(key)
            .map_or_else(|| prefix.to_owned(), |s| format!("{prefix} {s}"))
    }
}

impl ActionLabel {
    pub(super) fn from_call(input: &ToolCallInput) -> Option<Self> {
        let args = Args::new(&input.arguments);
        let (verb, subject) = match input.name.as_str() {
            "tool_search" | "mcp_search" => search(&args, input.name == "mcp_search"),
            "tool_describe" => (
                Verb::Read,
                args.text("name")
                    .map_or_else(|| "tool schema".into(), |s| format!("tool schema for {s}")),
            ),
            "mcp_describe" => (
                Verb::Read,
                args.text("name").map_or_else(
                    || "MCP tool description".into(),
                    |s| format!("MCP tool description for {s}"),
                ),
            ),
            "list_mcp_servers" => (Verb::List, "saved MCP servers".into()),
            "propose_mcp_server" | "propose_mcp_server_change" => (
                Verb::Propose,
                proposal(&args, input.name == "propose_mcp_server_change"),
            ),
            "skill" => skill(&args),
            "read_output" => saved_output(&args),
            name if name.starts_with("browser_") => browser(name, &args)?,
            _ => (Verb::Call, mcp_subject(input)?),
        };
        Some(Self { verb, subject })
    }
}

fn search(args: &Args<'_>, mcp: bool) -> (Verb, String) {
    let tools = if mcp { "MCP tools" } else { "tools" };
    if args
        .get("query")
        .and_then(Value::as_str)
        .is_some_and(|q| q.trim().is_empty())
    {
        return (Verb::List, if mcp { "MCP servers" } else { tools }.into());
    }
    (
        Verb::Search,
        args.text("query")
            .map_or_else(|| tools.into(), |q| format!("{tools} for “{q}”")),
    )
}

fn proposal(args: &Args<'_>, change: bool) -> String {
    let operation = if change {
        match args.get("action").and_then(Value::as_str) {
            Some("disable") => Some("disabling"),
            Some("remove") => Some("removing"),
            _ => None,
        }
    } else {
        match args.get("replace") {
            Some(Value::Bool(true)) => Some("replacing"),
            Some(Value::Bool(false)) => Some("adding"),
            None if !args.omitted => Some("adding"),
            _ => None,
        }
    };
    match (operation, args.text("name")) {
        (Some(op), Some(name)) => format!("{op} MCP server {name}"),
        (_, Some(name)) => format!("MCP server change for {name}"),
        _ => "MCP server change".into(),
    }
}

fn skill(args: &Args<'_>) -> (Verb, String) {
    if let Some(resource) = args.text("resource") {
        let subject = args.text("name").map_or_else(
            || format!("skill resource {resource}"),
            |s| format!("skill resource {s} / {resource}"),
        );
        (Verb::Read, subject)
    } else {
        (Verb::Load, args.named("skill", "name"))
    }
}

fn saved_output(args: &Args<'_>) -> (Verb, String) {
    let subject = args.named("saved output", "output_id");
    let chars = args.get("character_offset").is_some() || args.get("character_limit").is_some();
    let lines = args.get("offset").is_some() || args.get("limit").is_some();
    let (unit, offset, limit, default) = match (chars, lines) {
        (true, false) => ("characters", "character_offset", "character_limit", 0),
        (false, true) => ("lines", "offset", "limit", 1),
        _ => return (Verb::Read, subject),
    };
    let Some(start) = args
        .get(offset)
        .map_or(Some(default), Value::as_u64)
        .filter(|n| *n >= default)
    else {
        return (Verb::Read, subject);
    };
    let Some(limit) = args.get(limit) else {
        return (Verb::Request, format!("{unit} from {start} of {subject}"));
    };
    let end = limit
        .as_u64()
        .and_then(|n| n.checked_sub(1))
        .and_then(|n| start.checked_add(n));
    match end {
        Some(end) => (Verb::Request, format!("{unit} {start}–{end} of {subject}")),
        None => (Verb::Read, subject),
    }
}

fn browser(name: &str, args: &Args<'_>) -> Option<(Verb, String)> {
    let target = || args.text("ref").or_else(|| args.text("selector"));
    Some(match name {
        "browser_navigate" => {
            let place = args.get("window").and_then(Value::as_bool).map(|window| {
                if window {
                    "browser window"
                } else {
                    "browser pane"
                }
            });
            (
                Verb::Open,
                match (args.text("url"), place) {
                    (Some(url), Some(place)) => format!("{url} in the {place}"),
                    (Some(url), None) => url,
                    (None, place) => place.unwrap_or("browser").to_owned(),
                },
            )
        }
        "browser_read" => (Verb::Read, "browser page".into()),
        "browser_screenshot" => (Verb::Capture, "browser screenshot".into()),
        "browser_click" => (
            Verb::Click,
            target()
                .or_else(|| {
                    Some(format!(
                        "({}, {})",
                        args.get("x")?.as_i64()?,
                        args.get("y")?.as_i64()?
                    ))
                })
                .unwrap_or_else(|| "browser page".into()),
        ),
        "browser_type" => (
            Verb::Type,
            format!(
                "into {}",
                target().unwrap_or_else(|| "focused element".into())
            ),
        ),
        _ => return None,
    })
}
