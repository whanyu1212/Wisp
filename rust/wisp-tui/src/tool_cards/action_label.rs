//! Compact transcript labels. Keep raw bounded arguments separately for expansion.

use serde_json::{Map, Value};

use super::{
    GENERIC_VALUE_MAX_CHARS, ToolCallInput, clip_chars, generic_argument_items, generic_first_url,
    mcp_subject, one_line,
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
    pub group_target: Option<&'static str>,
}

/// Both live and history projection can wrap a generic tool's arguments in a bounded record.
struct Args<'a> {
    items: Option<&'a Map<String, Value>>,
    omitted: bool,
    first_url: Option<&'a str>,
}
impl<'a> Args<'a> {
    fn new(value: &'a Value) -> Self {
        let object = value.as_object();
        match object.and_then(generic_argument_items) {
            Some((items, omitted)) => Self {
                items: Some(items),
                omitted: omitted != 0,
                first_url: object.and_then(generic_first_url),
            },
            None => Self {
                items: object,
                omitted: false,
                first_url: None,
            },
        }
    }
    fn get(&self, key: &str) -> Option<&Value> {
        self.items?.get(key)
    }
    fn sanitize(raw: &str) -> Option<String> {
        let safe: String = one_line(&clip_chars(raw, GENERIC_VALUE_MAX_CHARS))
            .chars()
            .filter(|c| !c.is_control() && !crate::is_bidi_control(*c))
            .collect();
        (!safe.is_empty()).then_some(safe)
    }
    fn text(&self, key: &str) -> Option<String> {
        Self::sanitize(self.get(key)?.as_str()?)
    }
    /// Reads an optional string field, returning `Err(())` if present with a non-string
    /// shape or when omitted keys make absence ambiguous.
    fn optional_text(&self, key: &str) -> Result<Option<String>, ()> {
        self.optional_first_text(&[key])
    }
    /// Reads the first non-empty string among `keys` in order, stopping on match.
    /// Rejects visited keys with a non-string shape or ambiguous omission when none matched.
    fn optional_first_text(&self, keys: &[&str]) -> Result<Option<String>, ()> {
        for key in keys {
            match self.get(key) {
                None | Some(Value::Null) => continue,
                Some(Value::String(raw)) => {
                    if let Some(text) = Self::sanitize(raw) {
                        return Ok(Some(text));
                    }
                }
                Some(_) => return Err(()),
            }
        }
        if self.omitted { Err(()) } else { Ok(None) }
    }
    /// Reads an optional URL string or first string element of a URL array.
    fn optional_url_or_first(
        &self,
        string_key: &str,
        array_key: &str,
    ) -> Result<Option<String>, ()> {
        match self.get(string_key) {
            None | Some(Value::Null) => {}
            Some(Value::String(raw)) => {
                if let Some(url) = Self::sanitize(raw) {
                    return Ok(Some(url));
                }
            }
            Some(_) => return Err(()),
        }
        if let Some(url) = self.first_url.and_then(Self::sanitize) {
            return Ok(Some(url));
        }
        match self.get(array_key) {
            None => {
                if self.omitted {
                    Err(())
                } else {
                    Ok(None)
                }
            }
            Some(Value::Null) => Ok(None),
            Some(Value::Array(items)) => Ok(items
                .first()
                .and_then(Value::as_str)
                .and_then(Self::sanitize)),
            Some(_) => Err(()),
        }
    }
    fn named(&self, prefix: &str, key: &str) -> String {
        self.text(key)
            .map_or_else(|| prefix.to_owned(), |s| format!("{prefix} {s}"))
    }
}

impl ActionLabel {
    pub(super) fn from_call(input: &ToolCallInput) -> Option<Self> {
        let args = Args::new(&input.arguments);
        let (verb, subject, group_target) = match input.name.as_str() {
            "tool_search" | "mcp_search" => {
                let (verb, subject) = search(&args, input.name == "mcp_search");
                (verb, subject, None)
            }
            "tool_describe" => (
                Verb::Read,
                args.text("name")
                    .map_or_else(|| "tool schema".into(), |s| format!("tool schema for {s}")),
                None,
            ),
            "mcp_describe" => (
                Verb::Read,
                args.text("name").map_or_else(
                    || "MCP tool description".into(),
                    |s| format!("MCP tool description for {s}"),
                ),
                None,
            ),
            "list_mcp_servers" => (Verb::List, "saved MCP servers".into(), None),
            "propose_mcp_server" | "propose_mcp_server_change" => (
                Verb::Propose,
                proposal(&args, input.name == "propose_mcp_server_change"),
                None,
            ),
            "skill" => {
                let (verb, subject) = skill(&args);
                (verb, subject, None)
            }
            "read_output" => {
                let (verb, subject) = saved_output(&args);
                (verb, subject, None)
            }
            name if name.starts_with("browser_") => {
                let (verb, subject) = browser(name, &args)?;
                (verb, subject, None)
            }
            _ => {
                if let Some((verb, subject, group)) = mcp_action(input, &args) {
                    (verb, subject, Some(group))
                } else {
                    (Verb::Call, mcp_subject(input)?, None)
                }
            }
        };
        Some(Self {
            verb,
            subject,
            group_target,
        })
    }
}

fn mcp_action(input: &ToolCallInput, args: &Args<'_>) -> Option<(Verb, String, &'static str)> {
    if !input.arguments.is_object() {
        return None;
    }
    let (server, tool) = input.name.strip_prefix("mcp__")?.split_once("__")?;
    if server.is_empty() || tool.is_empty() {
        return None;
    }
    match server.to_ascii_lowercase().as_str() {
        "linear" => mcp_linear(tool, args),
        "exa" => mcp_exa(tool, args),
        "openaideveloperdocs" | "openai-docs" | "openai_docs" => mcp_openai_docs(tool, args),
        "playwright" => mcp_playwright(tool, args),
        _ => None,
    }
}

fn mcp_linear(tool: &str, args: &Args<'_>) -> Option<(Verb, String, &'static str)> {
    Some(match tool {
        "list_issues" => match args.optional_text("query").ok()? {
            Some(q) => (
                Verb::Search,
                format!("Linear issues for “{q}”"),
                "Linear issues",
            ),
            None => (Verb::List, "Linear issues".into(), "Linear issues"),
        },
        "get_issue" => match args.optional_text("id").ok()? {
            Some(id) => (Verb::Read, format!("Linear issue {id}"), "Linear issue"),
            None => (Verb::Read, "Linear issue".into(), "Linear issue"),
        },
        "list_projects" => match args.optional_text("query").ok()? {
            Some(q) => (
                Verb::Search,
                format!("Linear projects for “{q}”"),
                "Linear projects",
            ),
            None => (Verb::List, "Linear projects".into(), "Linear projects"),
        },
        "get_project" => match args.optional_first_text(&["query", "id"]).ok()? {
            Some(t) => (Verb::Read, format!("Linear project {t}"), "Linear project"),
            None => (Verb::Read, "Linear project".into(), "Linear project"),
        },
        "list_comments" => match args.optional_text("issueId").ok()? {
            Some(id) => (
                Verb::List,
                format!("comments on Linear issue {id}"),
                "Linear comments",
            ),
            None => (Verb::List, "Linear comments".into(), "Linear comments"),
        },
        "list_documents" => match args.optional_text("query").ok()? {
            Some(q) => (
                Verb::Search,
                format!("Linear documents for “{q}”"),
                "Linear documents",
            ),
            None => (Verb::List, "Linear documents".into(), "Linear documents"),
        },
        "get_document" => match args.optional_text("id").ok()? {
            Some(id) => (
                Verb::Read,
                format!("Linear document {id}"),
                "Linear document",
            ),
            None => (Verb::Read, "Linear document".into(), "Linear document"),
        },
        "search_documentation" => match args.optional_text("query").ok()? {
            Some(q) => (
                Verb::Search,
                format!("Linear docs for “{q}”"),
                "Linear docs",
            ),
            None => (Verb::Search, "Linear docs".into(), "Linear docs"),
        },
        _ => return None,
    })
}

fn mcp_exa(tool: &str, args: &Args<'_>) -> Option<(Verb, String, &'static str)> {
    Some(match tool {
        "web_search_exa" => match args.optional_text("query").ok()? {
            Some(q) => (
                Verb::Search,
                format!("the web via Exa for “{q}”"),
                "the web via Exa",
            ),
            None => (Verb::Search, "the web via Exa".into(), "the web via Exa"),
        },
        "web_fetch_exa" | "crawling_exa" => {
            match args.optional_url_or_first("url", "urls").ok()? {
                Some(url) => (Verb::Read, format!("{url} via Exa"), "web page via Exa"),
                None => (Verb::Read, "web page via Exa".into(), "web page via Exa"),
            }
        }
        _ => return None,
    })
}

fn mcp_openai_docs(tool: &str, args: &Args<'_>) -> Option<(Verb, String, &'static str)> {
    Some(match tool {
        "search_openai_docs" | "search" => match args.optional_text("query").ok()? {
            Some(q) => (
                Verb::Search,
                format!("OpenAI docs for “{q}”"),
                "OpenAI docs",
            ),
            None => (Verb::Search, "OpenAI docs".into(), "OpenAI docs"),
        },
        "fetch_openai_doc" | "fetch" => {
            match args.optional_first_text(&["url", "path", "id"]).ok()? {
                Some(t) => (Verb::Read, format!("OpenAI doc {t}"), "OpenAI doc"),
                None => (Verb::Read, "OpenAI doc".into(), "OpenAI doc"),
            }
        }
        _ => return None,
    })
}

fn mcp_playwright(tool: &str, args: &Args<'_>) -> Option<(Verb, String, &'static str)> {
    Some(match tool {
        "browser_navigate" => match args.optional_url_or_first("url", "urls").ok()? {
            Some(url) => (
                Verb::Open,
                format!("{url} in Playwright"),
                "Playwright browser",
            ),
            None => (
                Verb::Open,
                "Playwright browser".into(),
                "Playwright browser",
            ),
        },
        "browser_snapshot" => (
            Verb::Read,
            "Playwright page snapshot".into(),
            "Playwright page snapshot",
        ),
        "browser_take_screenshot" => (
            Verb::Capture,
            "Playwright screenshot".into(),
            "Playwright screenshot",
        ),
        "browser_click" => match args.optional_first_text(&["element", "ref"]).ok()? {
            Some(t) => (Verb::Click, format!("{t} in Playwright"), "Playwright page"),
            None => (Verb::Click, "Playwright page".into(), "Playwright page"),
        },
        "browser_type" => match args.optional_first_text(&["element", "ref"]).ok()? {
            Some(t) => (
                Verb::Type,
                format!("into {t} in Playwright"),
                "Playwright page",
            ),
            None => (Verb::Type, "into Playwright page".into(), "Playwright page"),
        },
        "browser_console_messages" => (
            Verb::Read,
            "Playwright console messages".into(),
            "Playwright console messages",
        ),
        "browser_network_requests" => (
            Verb::Read,
            "Playwright network requests".into(),
            "Playwright network requests",
        ),
        _ => return None,
    })
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
