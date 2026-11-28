import diffs from "../../../assets/docs/tui-diff-previews.png";
import approval from "../../../assets/docs/tui-approval.png";
import mcp from "../../../assets/docs/tui-mcp.png";
import working from "../../../assets/docs/tui-new-turn.png";
import showcase from "../../../assets/docs/tui-showcase.png";
import sessionWelcome from "../../../assets/docs/tui-session-welcome.png";
import sessionEditing from "../../../assets/docs/tui-session-editing.png";
import sessionFiles from "../../../assets/docs/tui-session-files.png";
import sessionBrowser from "../../../assets/docs/tui-session-browser.png";
import sessionPermissions from "../../../assets/docs/tui-session-permissions.png";

export const tuiCaptures = {
  sessionWelcome: {
    image: sessionWelcome,
    alt: "Wisp 0.16.12 welcome screen with the Braille mascot, session shortcuts, prompt composer, two-row telemetry deck, and Session | Files | Browser side panel.",
    caption:
      "Start a task in your terminal, resume a session, or browse available commands.",
    provenance: "Real session screenshot (v0.16.12) · full interface.",
  },
  sessionEditing: {
    image: sessionEditing,
    alt: "Wisp 0.16.12 live editing turn showing inline syntax-highlighted diffs, codemode and tool calls, an inline browser screenshot preview, the docked Session panel with context sparkline and turn timeline, and the two-row telemetry deck.",
    caption:
      "Watch inline diffs, tool activity, and captured previews stream beside context growth, turn timelines, and the two-row telemetry deck.",
    provenance: "Real session screenshot (v0.16.12) · full workspace.",
  },
  sessionFiles: {
    image: sessionFiles,
    alt: "Wisp 0.16.12 workspace with the Files side panel open, showing a project file tree with Nerd Font Material Design file and folder icons and an inline filter bar.",
    caption:
      "Browse and filter the project tree with Nerd Font icons, inline file previews, and quick editor handoff (e).",
    provenance: "Real session screenshot (v0.16.12) · full workspace.",
  },
  sessionBrowser: {
    image: sessionBrowser,
    alt: "Wisp 0.16.12 workspace with the Browser side panel rendering a live Chrome/Chromium page inside the terminal alongside navigation buttons and address bar.",
    caption:
      "Open a live Chrome/Chromium tab right inside the terminal side panel with click-to-interact takeover.",
    provenance: "Real session screenshot (v0.16.12) · full workspace.",
  },
  sessionPermissions: {
    image: sessionPermissions,
    alt: "Wisp 0.16.12 workspace with the docked Project permissions panel above the composer and two-row telemetry deck, offering Ask before changes, Auto edits, macOS sandboxed Auto commands, and YOLO.",
    caption:
      "Switch between Ask, Auto edits, macOS sandboxed Auto commands, and YOLO from the docked /permissions panel.",
    provenance: "Real session screenshot (v0.16.12) · full workspace.",
  },
  showcase: {
    image: showcase,
    alt: "Wisp terminal with a finished turn of tool calls and an inline diff, beside a panel listing context use, MCP servers, skills, changed files, and recent turns.",
    caption:
      "Follow each tool call and edit inline while the side panel tracks context, connections, changed files, and turns.",
  },
  diffs: {
    image: diffs,
    alt: "Wisp terminal showing edit and write cards with added and removed lines.",
    caption:
      "Inspect changes in inline diff cards; open a retained edit for detailed review.",
  },
  approval: {
    image: approval,
    alt: "Wisp approval panel showing a cargo test command and four keyboard choices before execution.",
    caption:
      "Review the proposed command before allowing it once, granting a broader scope, or denying it.",
  },
  mcp: {
    image: mcp,
    alt: "Wisp MCP panel showing the synthetic demo-docs server connected with two tools.",
    caption:
      "Use /mcp to inspect servers and tools, add a connection, or manage sign-in.",
  },
  working: {
    image: working,
    alt: "Wisp working row beneath a submitted prompt, with the composer still available for input.",
    caption:
      "The working row shows activity while the composer stays available for steering and follow-ups.",
  },
};
export type TuiCapture = keyof typeof tuiCaptures;
