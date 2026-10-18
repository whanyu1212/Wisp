# Security policy

Wisp runs shell commands, edits files and stores provider credentials on your machine, so we treat
the boundaries that control those actions as security boundaries.

## Reporting a vulnerability

Please report vulnerabilities privately. Don't open a public issue, pull request or discussion.

- **Preferred:** use GitHub's private vulnerability reporting: open
  [Report a vulnerability](https://github.com/whanyu1212/Wisp/security/advisories/new) from the
  repository's **Security** tab.
- **Or email** whanyu1212@hotmail.com with "Wisp security" in the subject.

Please include:

- the Wisp version (`wisp --version`) and how you installed it: native wheel, pure wheel or a
  source checkout;
- your operating system;
- steps to reproduce or a proof of concept, and what an attacker gains.

Remove API keys, tokens and private file contents from any logs you attach.

Wisp has one maintainer. Expect an acknowledgement within 7 days. Once a report is confirmed, the
fix lands on `main` and ships in the next release, followed by a published GitHub security
advisory. The advisory credits you unless you ask not to be named.

## Supported versions

| Version | Security fixes |
| --- | --- |
| 0.2 pre-releases (the newest `0.2.0rcN`) | Yes, in the next release |
| 0.1.x | No; upgrade to the newest 0.2 release |

## Scope

In scope are flaws that let a model, a project's files, an MCP server, or another local process get
past a boundary Wisp promises to enforce. For example:

- running a mutating tool (`write`, `edit`) or `bash` without the required approval, or approving
  something other than what the prompt showed;
- the file tools reading or writing outside the working directory, or reaching a protected path such
  as `.env`, including through symlinks;
- an untrusted project's files changing Wisp's behavior, or trust and YOLO being saved for the
  home folder or a broader folder;
- leaking stored credentials or OAuth tokens, or creating auth, session or trust files that other
  users can read;
- processes that keep running after cancellation or shutdown;
- the RPC transport or the TUI accepting malformed or mismatched input in a way that bypasses the
  checks above;
- tampering with release artifacts that our checksums, SBOMs or provenance attestations should
  catch.

Out of scope:

- actions you approved, or anything done in YOLO mode, that do what you asked;
- a model choosing a harmful action that still requires your approval;
- vulnerabilities in model providers, third-party MCP servers, or tools Wisp runs on your behalf;
- issues that need an attacker who already controls your user account.

[Tools & safety](https://whanyu1212.github.io/Wisp/guide/tools-and-safety.html) describes how
approvals, protected paths and project trust work.
