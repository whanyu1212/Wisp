//! Bounded display-only provider search and cost metadata.
use serde_json::Value;

fn text(value: &str, limit: usize) -> String {
    value
        .chars()
        .filter(|c| !c.is_control() && !crate::is_bidi_control(*c))
        .take(limit)
        .flat_map(|c| {
            // Keep remote text literal inside the Markdown transcript.
            if c.is_ascii_punctuation() {
                vec!['\\', c]
            } else {
                vec![c]
            }
        })
        .collect()
}

pub(crate) fn search(value: &Value) -> String {
    let action = match value["action"].as_str().unwrap_or("search") {
        "open_page" => "Opened page",
        "find_in_page" => "Searched within page",
        _ => "Searched",
    };
    let mut result = format!(
        "{action} {}",
        text(value["query"].as_str().unwrap_or(""), 512)
    );
    if let Some(error) = value["error"].as_str() {
        result.push_str(&format!(" — failed: {}", text(error, 128)));
    }
    if let Some(sources) = value["sources"].as_array() {
        for source in sources.iter().take(8) {
            let url = source["url"].as_str().unwrap_or("");
            // No clickable remote URLs: show only an HTTP(S) authority without userinfo.
            let host = url
                .strip_prefix("https://")
                .or_else(|| url.strip_prefix("http://"))
                .map(|rest| {
                    rest.split(['/', '?', '#'])
                        .next()
                        .unwrap_or("")
                        .rsplit('@')
                        .next()
                        .unwrap_or("")
                })
                .unwrap_or("invalid URL");
            result.push_str(&format!(
                "\n\n  {} · {}",
                text(source["title"].as_str().unwrap_or("Untitled"), 160),
                text(host, 160)
            ));
        }
    }
    result
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
    fn hostile_search_metadata_is_literal_and_bounded() {
        let value = json!({"query":"\u{1b}[2J\u{202e}[click](javascript:bad)",
            "error":"\u{7}failed", "sources": vec![json!({"title":"x".repeat(10000),
            "url":"https://user:secret@example.com/path"}); 100]});
        let rendered = search(&value);
        assert!(!rendered.contains('\u{1b}'));
        assert!(!rendered.contains('\u{202e}'));
        assert!(!rendered.contains("secret"));
        assert!(rendered.contains("example\\.com"));
        assert!(rendered.contains("\\[click\\]"));
        assert!(rendered.len() < 4000);
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
