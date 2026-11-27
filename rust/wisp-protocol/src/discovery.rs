//! Presentation values for the existing skill and MCP inspection contracts.

use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
pub enum SkillSource {
    #[serde(rename = "project:wisp")]
    ProjectWisp,
    #[serde(rename = "project:agents")]
    ProjectAgents,
    #[serde(rename = "user:wisp")]
    UserWisp,
    #[serde(rename = "user:agents")]
    UserAgents,
    #[serde(rename = "package:wisp")]
    PackageWisp,
}

impl SkillSource {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::ProjectWisp => "project:wisp",
            Self::ProjectAgents => "project:agents",
            Self::UserWisp => "user:wisp",
            Self::UserAgents => "user:agents",
            Self::PackageWisp => "package:wisp",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
pub struct SkillCatalogEntry {
    pub name: String,
    pub description: String,
    pub source: SkillSource,
}

impl SkillCatalogEntry {
    /// Only insert intact names accepted by Python's explicit skill directive grammar.
    pub fn is_invocable(&self) -> bool {
        !self.name.is_empty()
            && self.name.len() <= 64
            && self.name.split('-').all(|part| {
                !part.is_empty()
                    && part
                        .bytes()
                        .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit())
            })
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum SkillDiagnosticSeverity {
    Warning,
    Error,
}

impl SkillDiagnosticSeverity {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Warning => "warning",
            Self::Error => "error",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
pub struct SkillDiagnostic {
    pub code: String,
    pub severity: SkillDiagnosticSeverity,
    pub message: String,
    pub source: SkillSource,
    pub path: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
pub struct SkillCatalogSnapshot {
    pub entries: Vec<SkillCatalogEntry>,
    pub diagnostics: Vec<SkillDiagnostic>,
    pub project_trusted: bool,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum McpServerStatus {
    /// Not started yet: the backend connects servers after it begins serving.
    Connecting,
    Connected,
    Disconnected,
    Unavailable,
    Disabled,
}

impl McpServerStatus {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Connecting => "connecting",
            Self::Connected => "connected",
            Self::Disconnected => "disconnected",
            Self::Unavailable => "unavailable",
            Self::Disabled => "disabled",
        }
    }
}

/// What one MCP server runs or connects to, with environment variables named only.
///
/// This is what a user must see before confirming a server, and what
/// `set_mcp_server_enabled` sends back as the confirmed settings.
#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct McpServerLaunch {
    // Commands omit absent optional fields; events send them as null.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub command: Option<String>,
    pub args: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub url: Option<String>,
    pub env_names: Vec<String>,
    pub env_from: Vec<String>,
    /// An HTTP server's headers with stored values, by name only.
    #[serde(default)]
    pub header_names: Vec<String>,
    /// An HTTP server's headers built from environment variables: each sends
    /// that variable's value to the server's URL.
    #[serde(default)]
    pub headers_from: Vec<McpHeaderFrom>,
}

/// One HTTP header whose value Wisp builds from an environment variable,
/// sent as `"{scheme} {value}"` or the bare value.
#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct McpHeaderFrom {
    pub header: String,
    pub variable: String,
    // Commands omit an absent scheme; their schema does not accept null.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scheme: Option<String>,
}

impl McpHeaderFrom {
    /// The `headers_from` settings value this header is saved as.
    pub fn template(&self) -> String {
        match &self.scheme {
            Some(scheme) => format!("{scheme} ${{{}}}", self.variable),
            None => format!("${{{}}}", self.variable),
        }
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
pub struct McpServerSnapshot {
    pub name: String,
    pub status: McpServerStatus,
    pub tool_names: Vec<String>,
    /// The tools among `tool_names` that `tool_safety` marks read-only, in the same
    /// order: the only ones a codemode script, a subagent or plan mode can call.
    pub read_only_tool_names: Vec<String>,
    /// The tools among `tool_names` the server itself says are read-only and
    /// `tool_safety` leaves unset. They stay unscriptable until the user marks them
    /// with `mark_mcp_tools_read_only`.
    pub read_only_hint_tool_names: Vec<String>,
    pub error: Option<String>,
    pub launch: Option<McpServerLaunch>,
    /// OAuth sign-in for an HTTP server; None when the server cannot sign in.
    #[serde(default)]
    pub sign_in: Option<McpSignIn>,
}

/// Whether an HTTP MCP server is signed in with OAuth.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum McpSignIn {
    /// The server refused Wisp, or a saved sign-in no longer works.
    Required,
    /// The server started with a saved sign-in.
    SignedIn,
    /// The user could sign in.
    Available,
}

/// One server parsed from a pasted config snippet, not yet saved.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
pub struct McpServerPreview {
    pub name: String,
    pub launch: McpServerLaunch,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
pub struct McpStatusSnapshot {
    pub servers: Vec<McpServerSnapshot>,
}

impl From<&crate::events::wire::SkillSource> for SkillSource {
    fn from(wire: &crate::events::wire::SkillSource) -> Self {
        match wire {
            crate::events::wire::SkillSource::ProjectWisp => Self::ProjectWisp,
            crate::events::wire::SkillSource::ProjectAgents => Self::ProjectAgents,
            crate::events::wire::SkillSource::UserWisp => Self::UserWisp,
            crate::events::wire::SkillSource::UserAgents => Self::UserAgents,
            crate::events::wire::SkillSource::PackageWisp => Self::PackageWisp,
        }
    }
}

impl From<&crate::events::wire::RpcSkillCatalogEntry> for SkillCatalogEntry {
    fn from(wire: &crate::events::wire::RpcSkillCatalogEntry) -> Self {
        Self {
            name: wire.name.clone(),
            description: wire.description.clone(),
            source: SkillSource::from(&wire.source),
        }
    }
}

impl From<&crate::events::wire::SkillDiagnosticSeverity> for SkillDiagnosticSeverity {
    fn from(wire: &crate::events::wire::SkillDiagnosticSeverity) -> Self {
        match wire {
            crate::events::wire::SkillDiagnosticSeverity::Warning => Self::Warning,
            crate::events::wire::SkillDiagnosticSeverity::Error => Self::Error,
        }
    }
}

impl From<&crate::events::wire::RpcSkillDiagnostic> for SkillDiagnostic {
    fn from(wire: &crate::events::wire::RpcSkillDiagnostic) -> Self {
        Self {
            code: wire.code.to_string(),
            severity: SkillDiagnosticSeverity::from(&wire.severity),
            message: wire.message.clone(),
            source: SkillSource::from(&wire.source),
            path: wire.path.clone(),
        }
    }
}

impl From<&crate::events::wire::RpcSkillCatalogSnapshot> for SkillCatalogSnapshot {
    fn from(wire: &crate::events::wire::RpcSkillCatalogSnapshot) -> Self {
        Self {
            entries: wire.entries.iter().map(SkillCatalogEntry::from).collect(),
            diagnostics: wire.diagnostics.iter().map(SkillDiagnostic::from).collect(),
            project_trusted: wire.project_trusted,
        }
    }
}

impl From<&crate::events::wire::RpcMcpServerSnapshotStatus> for McpServerStatus {
    fn from(wire: &crate::events::wire::RpcMcpServerSnapshotStatus) -> Self {
        match wire {
            crate::events::wire::RpcMcpServerSnapshotStatus::Connecting => Self::Connecting,
            crate::events::wire::RpcMcpServerSnapshotStatus::Connected => Self::Connected,
            crate::events::wire::RpcMcpServerSnapshotStatus::Disconnected => Self::Disconnected,
            crate::events::wire::RpcMcpServerSnapshotStatus::Unavailable => Self::Unavailable,
            crate::events::wire::RpcMcpServerSnapshotStatus::Disabled => Self::Disabled,
        }
    }
}

impl From<&crate::events::wire::RpcMcpServerSnapshotSignIn> for McpSignIn {
    fn from(wire: &crate::events::wire::RpcMcpServerSnapshotSignIn) -> Self {
        match wire {
            crate::events::wire::RpcMcpServerSnapshotSignIn::Required => Self::Required,
            crate::events::wire::RpcMcpServerSnapshotSignIn::SignedIn => Self::SignedIn,
            crate::events::wire::RpcMcpServerSnapshotSignIn::Available => Self::Available,
        }
    }
}

impl From<&crate::events::wire::RpcMcpHeaderFrom> for McpHeaderFrom {
    fn from(wire: &crate::events::wire::RpcMcpHeaderFrom) -> Self {
        Self {
            header: wire.header.to_string(),
            variable: wire.variable.to_string(),
            scheme: wire.scheme.clone(),
        }
    }
}

impl From<&crate::events::wire::RpcMcpServerLaunch> for McpServerLaunch {
    fn from(wire: &crate::events::wire::RpcMcpServerLaunch) -> Self {
        Self {
            command: wire.command.clone(),
            args: wire.args.clone(),
            url: wire.url.clone(),
            env_names: wire.env_names.clone(),
            env_from: wire.env_from.clone(),
            header_names: wire.header_names.clone(),
            headers_from: wire.headers_from.iter().map(McpHeaderFrom::from).collect(),
        }
    }
}

impl From<&crate::events::wire::RpcMcpServerSnapshot> for McpServerSnapshot {
    fn from(wire: &crate::events::wire::RpcMcpServerSnapshot) -> Self {
        Self {
            name: wire.name.to_string(),
            status: McpServerStatus::from(&wire.status),
            tool_names: wire.tool_names.clone(),
            read_only_tool_names: wire.read_only_tool_names.clone(),
            read_only_hint_tool_names: wire.read_only_hint_tool_names.clone(),
            error: wire.error.clone(),
            launch: wire.launch.as_ref().map(McpServerLaunch::from),
            sign_in: wire.sign_in.as_ref().map(McpSignIn::from),
        }
    }
}

impl From<&crate::events::wire::RpcMcpServerPreview> for McpServerPreview {
    fn from(wire: &crate::events::wire::RpcMcpServerPreview) -> Self {
        Self {
            name: wire.name.to_string(),
            launch: McpServerLaunch::from(&wire.launch),
        }
    }
}

impl From<&crate::events::wire::RpcMcpStatusSnapshot> for McpStatusSnapshot {
    fn from(wire: &crate::events::wire::RpcMcpStatusSnapshot) -> Self {
        Self {
            servers: wire.servers.iter().map(McpServerSnapshot::from).collect(),
        }
    }
}
