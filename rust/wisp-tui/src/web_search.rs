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

pub(crate) fn cost(value: &Value) -> Option<String> {
    if value.is_null() {
        return None;
    }
    let amount = value["estimated_usd"]
        .as_str()
        .and_then(|s| s.parse::<f64>().ok())
        .or_else(|| value["estimated_usd"].as_f64());
    let Some(amount) = amount.filter(|a| a.is_finite() && *a >= 0.0) else {
        return Some("cost unknown".into());
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
    Some(format!(
        "cost {}{amount}",
        if value["web_searches"].as_u64().unwrap_or(0) > 0 {
            "≥"
        } else {
            ""
        }
    ))
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
        assert_eq!(
            cost(&json!({"estimated_usd":"0.001", "web_searches":2})).as_deref(),
            Some("cost ≥$0.0010")
        );
        assert_eq!(
            cost(&json!({"estimated_usd":"0.02"})).as_deref(),
            Some("cost $0.020")
        );
        assert_eq!(
            cost(&json!({"estimated_usd":null, "web_searches":2})).as_deref(),
            Some("cost unknown")
        );
        assert_eq!(cost(&Value::Null), None);
    }
}
