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

/// Where context tokens come from, in the order a breakdown lists them.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ContextSource {
    Messages,
    BuiltInTools,
    McpTools,
    CustomPrompt,
    SystemPrompt,
    Skills,
    ProjectInstructions,
    ProjectContext,
}

/// One named part of a source, such as an MCP server or an instruction file.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ContextSourceItem {
    pub label: String,
    pub tokens: u64,
    pub count: Option<u64>,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ContextSourceUsage {
    pub source: ContextSource,
    pub tokens: u64,
    pub count: Option<u64>,
    pub children: Vec<ContextSourceItem>,
}

/// One large single message in the active context.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ContextItem {
    pub label: String,
    pub tokens: u64,
    pub turn: u64,
}

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ContextTurnGrowth {
    pub turn: u64,
    pub tokens: u64,
}

/// Per-source split of the budget's effective tokens. The sources, each source's
/// children, and the turns each sum exactly to that total; `scaled` marks a total
/// the provider observed rather than one the backend estimated.
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ContextBreakdown {
    pub sources: Vec<ContextSourceUsage>,
    pub scaled: bool,
    pub turns: Vec<ContextTurnGrowth>,
    pub largest: Vec<ContextItem>,
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
    pub context_breakdown: Option<ContextBreakdown>,
    #[serde(default)]
    pub branch: SessionBranchSummary,
    #[serde(default)]
    pub used_skill_names: Vec<String>,
}

/// Where the active path sits in the session tree. `branched_from_turn` is set when
/// the path takes a branch, with `siblings` other branches at that point; on the
/// original line `branches` counts the branches that split off it.
#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq)]
pub struct SessionBranchSummary {
    pub branched_from_turn: Option<u64>,
    #[serde(default)]
    pub siblings: u64,
    #[serde(default)]
    pub branches: u64,
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

impl From<&crate::events::wire::ContextEstimate> for ContextEstimate {
    fn from(wire: &crate::events::wire::ContextEstimate) -> Self {
        Self {
            total_tokens: wire.total_tokens,
        }
    }
}

impl From<&crate::events::wire::ContextBudgetAccountingMethod> for ContextAccountingMethod {
    fn from(wire: &crate::events::wire::ContextBudgetAccountingMethod) -> Self {
        match wire {
            crate::events::wire::ContextBudgetAccountingMethod::FullyEstimated => {
                Self::FullyEstimated
            }
            crate::events::wire::ContextBudgetAccountingMethod::ProviderObserved => {
                Self::ProviderObserved
            }
            crate::events::wire::ContextBudgetAccountingMethod::ProviderObservedPlusEstimate => {
                Self::ProviderObservedPlusEstimate
            }
        }
    }
}

impl From<&crate::events::wire::ContextBudget> for ContextBudget {
    fn from(wire: &crate::events::wire::ContextBudget) -> Self {
        Self {
            estimate: ContextEstimate::from(&wire.estimate),
            observed_tokens: wire.observed_tokens,
            observed_is_current: wire.observed_is_current,
            effective_tokens: Some(wire.effective_tokens),
            accounting_method: ContextAccountingMethod::from(&wire.accounting_method),
            context_window: wire.context_window.map(std::num::NonZeroU64::get),
            reserve_tokens: wire.reserve_tokens,
        }
    }
}

impl From<&crate::events::wire::TokenUsage> for SessionTokenUsage {
    fn from(wire: &crate::events::wire::TokenUsage) -> Self {
        Self {
            input_tokens: wire.input_tokens,
            output_tokens: wire.output_tokens,
            total_tokens: wire.total_tokens,
            cache_read_input_tokens: wire.cache_read_input_tokens,
            cache_write_input_tokens: wire.cache_write_input_tokens,
        }
    }
}

impl From<&crate::events::wire::SessionCostSummary> for SessionCostSummary {
    fn from(wire: &crate::events::wire::SessionCostSummary) -> Self {
        Self {
            known_usd: wire.known_usd.to_string(),
            complete: wire.complete,
            priced_record_count: wire.priced_record_count,
            unpriced_record_count: wire.unpriced_record_count,
        }
    }
}

impl From<&crate::events::wire::CompactionPolicyStatus> for CompactionPolicyStatus {
    fn from(wire: &crate::events::wire::CompactionPolicyStatus) -> Self {
        Self {
            auto_compaction_enabled: wire.auto_compaction_enabled,
            threshold_eligible: wire.threshold_eligible,
            threshold_ineligible_reason: wire.threshold_ineligible_reason.clone(),
            overflow_recovery_enabled: wire.overflow_recovery_enabled,
        }
    }
}

impl From<&crate::events::wire::ContextSourceUsageSource> for ContextSource {
    fn from(wire: &crate::events::wire::ContextSourceUsageSource) -> Self {
        match wire {
            crate::events::wire::ContextSourceUsageSource::Messages => Self::Messages,
            crate::events::wire::ContextSourceUsageSource::BuiltInTools => Self::BuiltInTools,
            crate::events::wire::ContextSourceUsageSource::McpTools => Self::McpTools,
            crate::events::wire::ContextSourceUsageSource::CustomPrompt => Self::CustomPrompt,
            crate::events::wire::ContextSourceUsageSource::SystemPrompt => Self::SystemPrompt,
            crate::events::wire::ContextSourceUsageSource::Skills => Self::Skills,
            crate::events::wire::ContextSourceUsageSource::ProjectInstructions => {
                Self::ProjectInstructions
            }
            crate::events::wire::ContextSourceUsageSource::ProjectContext => Self::ProjectContext,
        }
    }
}

impl From<&crate::events::wire::ContextSourceItem> for ContextSourceItem {
    fn from(wire: &crate::events::wire::ContextSourceItem) -> Self {
        Self {
            label: wire.label.clone(),
            tokens: wire.tokens,
            count: wire.count,
        }
    }
}

impl From<&crate::events::wire::ContextSourceUsage> for ContextSourceUsage {
    fn from(wire: &crate::events::wire::ContextSourceUsage) -> Self {
        Self {
            source: ContextSource::from(&wire.source),
            tokens: wire.tokens,
            count: wire.count,
            children: wire.children.iter().map(ContextSourceItem::from).collect(),
        }
    }
}

impl From<&crate::events::wire::ContextItem> for ContextItem {
    fn from(wire: &crate::events::wire::ContextItem) -> Self {
        Self {
            label: wire.label.clone(),
            tokens: wire.tokens,
            turn: wire.turn.get(),
        }
    }
}

impl From<&crate::events::wire::ContextTurnGrowth> for ContextTurnGrowth {
    fn from(wire: &crate::events::wire::ContextTurnGrowth) -> Self {
        Self {
            turn: wire.turn.get(),
            tokens: wire.tokens,
        }
    }
}

impl From<&crate::events::wire::ContextBreakdown> for ContextBreakdown {
    fn from(wire: &crate::events::wire::ContextBreakdown) -> Self {
        Self {
            sources: wire.sources.iter().map(ContextSourceUsage::from).collect(),
            scaled: wire.scaled,
            turns: wire.turns.iter().map(ContextTurnGrowth::from).collect(),
            largest: wire.largest.iter().map(ContextItem::from).collect(),
        }
    }
}

impl From<&crate::events::wire::SessionBranchSummary> for SessionBranchSummary {
    fn from(wire: &crate::events::wire::SessionBranchSummary) -> Self {
        Self {
            branched_from_turn: wire.branched_from_turn,
            siblings: wire.siblings,
            branches: wire.branches,
        }
    }
}

impl From<&crate::events::wire::SessionStats> for SessionStats {
    fn from(wire: &crate::events::wire::SessionStats) -> Self {
        Self {
            session_id: wire.session_id.clone(),
            active_message_count: wire.active_message_count,
            compaction_count: wire.compaction_count,
            usage: SessionTokenUsage::from(&wire.usage),
            context: ContextBudget::from(&wire.context),
            compaction: wire.compaction.as_ref().map(CompactionPolicyStatus::from),
            cost: SessionCostSummary::from(&wire.cost),
            context_breakdown: wire.context_breakdown.as_ref().map(ContextBreakdown::from),
            branch: SessionBranchSummary::from(&wire.branch),
            used_skill_names: wire.used_skill_names.clone(),
        }
    }
}

impl From<&crate::events::wire::ContextEstimated> for ContextEstimated {
    fn from(wire: &crate::events::wire::ContextEstimated) -> Self {
        Self {
            provider: wire.provider.clone(),
            model: wire.model.clone(),
            budget: ContextBudget::from(&wire.budget),
        }
    }
}

impl From<&crate::events::wire::CompactionStartedReason> for CompactionReason {
    fn from(wire: &crate::events::wire::CompactionStartedReason) -> Self {
        match wire {
            crate::events::wire::CompactionStartedReason::Manual => Self::Manual,
            crate::events::wire::CompactionStartedReason::Threshold => Self::Threshold,
            crate::events::wire::CompactionStartedReason::Overflow => Self::Overflow,
        }
    }
}

impl From<&crate::events::wire::CompactionCompletedReason> for CompactionReason {
    fn from(wire: &crate::events::wire::CompactionCompletedReason) -> Self {
        match wire {
            crate::events::wire::CompactionCompletedReason::Manual => Self::Manual,
            crate::events::wire::CompactionCompletedReason::Threshold => Self::Threshold,
            crate::events::wire::CompactionCompletedReason::Overflow => Self::Overflow,
        }
    }
}

impl From<&crate::events::wire::CompactionCompletedOutcome> for CompactionOutcome {
    fn from(wire: &crate::events::wire::CompactionCompletedOutcome) -> Self {
        match wire {
            crate::events::wire::CompactionCompletedOutcome::Completed => Self::Completed,
            crate::events::wire::CompactionCompletedOutcome::Failed => Self::Failed,
            crate::events::wire::CompactionCompletedOutcome::Cancelled => Self::Cancelled,
        }
    }
}

impl From<&crate::events::wire::CompactionStarted> for CompactionStarted {
    fn from(wire: &crate::events::wire::CompactionStarted) -> Self {
        Self {
            session_id: wire.session_id.clone(),
            reason: CompactionReason::from(&wire.reason),
            trigger_budget: wire.trigger_budget.as_ref().map(ContextBudget::from),
        }
    }
}

impl From<&crate::events::wire::CompactionCompleted> for CompactionCompleted {
    fn from(wire: &crate::events::wire::CompactionCompleted) -> Self {
        Self {
            session_id: wire.session_id.clone(),
            reason: CompactionReason::from(&wire.reason),
            outcome: CompactionOutcome::from(&wire.outcome),
            replaced_entry_count: wire.replaced_entry_count,
            retained_entry_count: wire.retained_entry_count,
            error: wire.error.clone(),
            will_retry: wire.will_retry,
        }
    }
}
