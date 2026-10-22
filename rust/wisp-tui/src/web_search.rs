//! Bounded display-only provider search and cost metadata.
use serde_json::Value;

/// Remote text as one inert line: the transcript draws searches as tool rows, so only
/// controls and bidi overrides need removing.
fn text(value: &str, limit: usize) -> String {
    value
        .chars()
        .filter(|c| !c.is_control() && !crate::is_bidi_control(*c))
        .take(limit)
        .collect()
}

/// A provider-native web search, as the reply's tool row shows it.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WebSearch {
    /// What was searched: the query, the page opened, or the pattern and its page.
    pub argument: String,
    /// The sites it returned, in order, each once.
    pub hosts: Vec<String>,
    pub failed: bool,
}

pub(crate) fn search(value: &Value) -> WebSearch {
    let query = text(value["query"].as_str().unwrap_or(""), 512);
    let mut hosts = Vec::new();
    for source in value["sources"].as_array().into_iter().flatten().take(8) {
        let host = text(host(source["url"].as_str().unwrap_or("")), 160);
        if !hosts.contains(&host) {
            hosts.push(host);
        }
    }
    let argument = match value["action"].as_str().unwrap_or("search") {
        // An opened page is named by its site; its own source lists nothing new.
        "open_page" => format!("open {}", hosts.drain(..).next().unwrap_or(query)),
        "find_in_page" if !hosts.is_empty() => format!("{query} in {}", hosts.remove(0)),
        _ => query,
    };
    WebSearch {
        argument,
        hosts,
        failed: value["error"].as_str().is_some(),
    }
}

/// No clickable remote URLs: show only an HTTP(S) authority without userinfo.
fn host(url: &str) -> &str {
    url.strip_prefix("https://")
        .or_else(|| url.strip_prefix("http://"))
        .map(|rest| {
            rest.split(['/', '?', '#'])
                .next()
                .unwrap_or("")
                .rsplit('@')
                .next()
                .unwrap_or("")
        })
        .unwrap_or("invalid URL")
}

/// Do not create a cost-only placeholder before tools when no estimate is available.
/// Live completions and restored messages use the same display policy; the underlying
/// usage and unavailable reason remain untouched.
pub(crate) fn message_cost(message: &Value) -> Option<MessageCost> {
    let tool_only = message["content"].as_str() == Some("")
        && message["tool_calls"]
            .as_array()
            .is_some_and(|calls| !calls.is_empty());
    cost(&message["cost"], !tool_only)
}

/// A reply's cost: the label closing it, and the estimate a run's total adds up.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MessageCost {
    pub label: String,
    /// The estimate in billionths of a dollar, so totals add exactly; `None` when the
    /// provider's usage could not be priced.
    pub nano_usd: Option<u64>,
    /// Web searches are billed apart from tokens, so the estimate is a floor.
    pub lower_bound: bool,
}

/// Replies' costs added up, as a run's total.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct CostTotal {
    pub nano_usd: u64,
    /// Whether any reply was priced; a total of no priced replies shows nothing.
    pub priced: bool,
    /// A reply searched the web, or had text that could not be priced, so the total
    /// is a floor.
    pub floor: bool,
}

impl CostTotal {
    pub(crate) fn of<'a>(costs: impl IntoIterator<Item = &'a MessageCost>) -> Self {
        let mut total = Self::default();
        for cost in costs {
            match cost.nano_usd {
                Some(amount) => {
                    total.nano_usd = total.nano_usd.saturating_add(amount);
                    total.priced = true;
                }
                None => total.floor = true,
            }
            total.floor |= cost.lower_bound;
        }
        total
    }
}

fn cost(value: &Value, show_unknown: bool) -> Option<MessageCost> {
    if value.is_null() {
        return None;
    }
    let lower_bound = value["web_searches"].as_u64().unwrap_or(0) > 0;
    let usd = value["estimated_usd"]
        .as_str()
        .and_then(|s| s.parse::<f64>().ok())
        .or_else(|| value["estimated_usd"].as_f64())
        .filter(|a| a.is_finite() && *a >= 0.0);
    let Some(amount) = usd else {
        return show_unknown.then(|| MessageCost {
            label: "cost unknown".into(),
            nano_usd: None,
            lower_bound,
        });
    };
    let amount = if amount == 0.0 {
        "$0.00".into()
    } else if amount < 0.0001 {
        "<$0.0001".into()
    } else if amount < 0.01 {
        format!("${amount:.4}")
    } else if amount < 1.0 {
        format!("${amount:.3}")
    } else {
        format!("${amount:.2}")
    };
    Some(MessageCost {
        label: format!("cost {}{amount}", if lower_bound { "≥" } else { "" }),
        nano_usd: usd.map(|usd| (usd * 1e9).round() as u64),
        lower_bound,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn hostile_search_metadata_is_inert() {
        let value = json!({"query":"\u{1b}[2J\u{202e}[click](javascript:bad)",
            "error":"\u{7}failed", "sources": vec![json!({"title":"x".repeat(10000),
            "url":"https://user:secret@example.com/path"}); 100]});
        let search = search(&value);
        assert_eq!(search.argument, "[2J[click](javascript:bad)");
        assert_eq!(search.hosts, ["example.com"]);
        assert!(search.failed);
    }

    #[test]
    fn opened_pages_name_their_site() {
        let page = |action, query| {
            search(&json!({"action": action, "query": query,
                "sources": [{"url": "https://docs.rs/serde"}, {"url": "https://serde.rs"}]}))
        };
        let opened = page("open_page", "https://docs.rs/serde");
        assert_eq!(opened.argument, "open docs.rs");
        assert!(opened.hosts.is_empty());
        let found = page("find_in_page", "derive");
        assert_eq!(found.argument, "derive in docs.rs");
        assert_eq!(found.hosts, ["serde.rs"]);
    }

    #[test]
    fn costs_distinguish_lower_bounds_and_unknown_estimates() {
        let cost = |value: &Value, show_unknown| cost(value, show_unknown).map(|cost| cost.label);
        assert_eq!(
            cost(&json!({"estimated_usd":"0.001", "web_searches":2}), true).as_deref(),
            Some("cost ≥$0.0010")
        );
        assert_eq!(
            cost(&json!({"estimated_usd":"0.02"}), true).as_deref(),
            Some("cost $0.020")
        );
        assert_eq!(
            cost(&json!({"estimated_usd":null, "web_searches":2}), true).as_deref(),
            Some("cost unknown")
        );
        assert_eq!(cost(&Value::Null, true), None);
    }
}
