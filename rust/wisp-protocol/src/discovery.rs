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
    Connected,
    Disconnected,
    Unavailable,
    Disabled,
}

impl McpServerStatus {
    pub fn as_str(self) -> &'static str {
        match self {
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
    #[serde(default)]
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
    pub error: Option<String>,
    pub launch: Option<McpServerLaunch>,
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
