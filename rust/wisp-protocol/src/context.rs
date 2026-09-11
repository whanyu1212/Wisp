//! Presentation projections of the existing context and compaction contracts.

use serde::Deserialize;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ContextAccountingMethod {
    FullyEstimated,
    ProviderObserved,
    ProviderObservedPlusEstimate,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ContextEstimate {
    pub total_tokens: u64,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ContextBudget {
    pub estimate: ContextEstimate,
    pub observed_tokens: Option<u64>,
    pub observed_is_current: bool,
    pub effective_tokens: Option<u64>,
    pub accounting_method: ContextAccountingMethod,
    pub context_window: Option<u64>,
    pub reserve_tokens: u64,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct SessionTokenUsage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub total_tokens: u64,
    pub cache_read_input_tokens: Option<u64>,
    pub cache_write_input_tokens: Option<u64>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct SessionCostSummary {
    /// Preserve the backend's exact decimal; the frontend does not price usage.
    pub known_usd: String,
    pub complete: bool,
    pub priced_record_count: u64,
    pub unpriced_record_count: u64,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct CompactionPolicyStatus {
    pub auto_compaction_enabled: bool,
    pub threshold_eligible: bool,
    pub threshold_ineligible_reason: Option<String>,
    pub overflow_recovery_enabled: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct SessionStats {
    pub session_id: Option<String>,
    pub active_message_count: u64,
    pub compaction_count: u64,
    pub usage: SessionTokenUsage,
    pub context: ContextBudget,
    pub compaction: Option<CompactionPolicyStatus>,
    pub cost: SessionCostSummary,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ContextEstimated {
    pub provider: String,
    pub model: Option<String>,
    pub budget: ContextBudget,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum CompactionReason {
    Manual,
    Threshold,
    Overflow,
}

impl CompactionReason {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Manual => "manual",
            Self::Threshold => "threshold",
            Self::Overflow => "overflow",
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum CompactionOutcome {
    Completed,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct CompactionStarted {
    pub session_id: String,
    pub reason: CompactionReason,
    pub trigger_budget: Option<ContextBudget>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct CompactionCompleted {
    pub session_id: String,
    pub reason: CompactionReason,
    pub outcome: CompactionOutcome,
    pub replaced_entry_count: u64,
    pub retained_entry_count: u64,
    pub error: Option<String>,
    pub will_retry: bool,
}
