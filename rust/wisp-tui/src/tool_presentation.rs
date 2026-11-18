//! Reasons and outcome counts derived from execution facts, never from diagnostic text.

use crate::tool_cards::ToolStatus;

pub(crate) fn failure_reason(
    is_error: bool,
    exit_code: Option<i64>,
    failure_code: Option<&str>,
    process_state: Option<&str>,
) -> Option<String> {
    if process_state == Some("cancelled")
        || !(is_error
            || exit_code.is_some_and(|code| code != 0)
            || matches!(process_state, Some("failed" | "timed_out")))
    {
        return None;
    }
    let reason = match failure_code {
        Some("approval_denied") => "Approval denied",
        Some("internal_error") => "Tool execution error",
        Some("invalid_arguments") => "Invalid arguments",
        Some("invalid_pattern") => "Invalid search pattern",
        Some("invalid_result") => "Invalid tool result",
        Some("not_found") => "Path or resource not found",
        Some("path_outside_workspace") => "Path outside workspace",
        Some("policy_denied") => "Blocked by policy",
        Some("stale_input") => "Stale input",
        Some("timeout") => "Timed out",
        Some("unknown_tool") => "Unknown tool",
        _ if process_state == Some("timed_out") => "Timed out",
        _ if exit_code.is_some_and(|code| code != 0) => {
            return Some(format!("Command exited {}", exit_code?));
        }
        _ if process_state == Some("failed") => "Process failed",
        _ => "Tool reported an error",
    };
    Some(reason.to_owned())
}

/// A group's members as observed, not inferred from its worst status or later retries.
#[derive(Clone, Debug, Default, Hash, PartialEq, Eq)]
pub(crate) struct OutcomeCounts {
    succeeded: usize,
    failed: usize,
    denied: usize,
    cancelled: usize,
    running: usize,
    pending: usize,
    awaiting: usize,
}

impl OutcomeCounts {
    pub fn record(&mut self, status: ToolStatus) {
        let count = match status {
            ToolStatus::Done => &mut self.succeeded,
            ToolStatus::Error => &mut self.failed,
            ToolStatus::Denied => &mut self.denied,
            ToolStatus::Cancelled => &mut self.cancelled,
            ToolStatus::Running => &mut self.running,
            ToolStatus::Requested => &mut self.pending,
            ToolStatus::AwaitingApproval => &mut self.awaiting,
        };
        *count += 1;
    }

    pub fn is_open(&self) -> bool {
        self.running + self.pending + self.awaiting > 0
    }

    pub fn note(&self, budget: usize) -> Option<String> {
        if !self.noteworthy() {
            return None;
        }
        let full = self.summary();
        Some(if full.chars().count() <= budget {
            full
        } else {
            self.compact()
        })
    }

    pub fn noteworthy(&self) -> bool {
        self.failed + self.denied + self.cancelled + self.running + self.pending + self.awaiting > 0
    }

    pub fn summary(&self) -> String {
        [
            (self.succeeded, "succeeded"),
            (self.failed, "failed"),
            (self.denied, "denied"),
            (self.cancelled, "cancelled"),
            (self.running, "running"),
            (self.pending, "pending"),
            (self.awaiting, "awaiting approval"),
        ]
        .into_iter()
        .filter(|(count, _)| *count > 0)
        .map(|(count, label)| format!("{count} {label}"))
        .collect::<Vec<_>>()
        .join(" · ")
    }

    /// Keep the most important outcome and the denominator when full counts will not fit.
    /// This does not claim that the remaining members succeeded.
    pub fn compact(&self) -> String {
        let total = self.succeeded
            + self.failed
            + self.denied
            + self.cancelled
            + self.running
            + self.pending
            + self.awaiting;
        let (count, label) = [
            (self.failed, "failed"),
            (self.denied, "denied"),
            (self.cancelled, "cancelled"),
            (self.awaiting, "awaiting approval"),
            (self.running, "running"),
            (self.pending, "pending"),
            (self.succeeded, "succeeded"),
        ]
        .into_iter()
        .find(|(count, _)| *count > 0)
        .unwrap_or((0, "finished"));
        format!("{count}/{total} {label}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct Case {
        is_error: bool,
        exit_code: Option<i64>,
        failure_code: Option<String>,
        process_state: Option<String>,
        expected: Option<String>,
    }

    #[test]
    fn failure_reasons_match_python() {
        let cases: Vec<Case> = serde_json::from_str(include_str!(
            "../../../tests/fixtures/tool_failure_reasons.json"
        ))
        .unwrap();
        for case in cases {
            assert_eq!(
                failure_reason(
                    case.is_error,
                    case.exit_code,
                    case.failure_code.as_deref(),
                    case.process_state.as_deref()
                ),
                case.expected
            );
        }
    }

    #[test]
    fn counts_do_not_call_unfinished_or_denied_members_successful() {
        let mut counts = OutcomeCounts::default();
        for status in [
            ToolStatus::Done,
            ToolStatus::Error,
            ToolStatus::Denied,
            ToolStatus::Cancelled,
            ToolStatus::Running,
            ToolStatus::Requested,
            ToolStatus::AwaitingApproval,
        ] {
            counts.record(status);
        }
        assert_eq!(
            counts.summary(),
            "1 succeeded · 1 failed · 1 denied · 1 cancelled · 1 running · 1 pending · 1 awaiting approval"
        );
        assert_eq!(counts.compact(), "1/7 failed");
        assert!(counts.noteworthy());
        let mut success = OutcomeCounts::default();
        success.record(ToolStatus::Done);
        assert!(!success.noteworthy());
    }
}
