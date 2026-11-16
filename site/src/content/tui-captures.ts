import diffs from "../../../assets/docs/tui-diff-previews.png";
import approval from "../../../assets/docs/tui-approval.png";
import mcp from "../../../assets/docs/tui-mcp.png";
import working from "../../../assets/docs/tui-new-turn.png";
import showcase from "../../../assets/docs/tui-showcase.png";
import sessionWelcome from "../../../assets/docs/tui-session-welcome.png";
import sessionEditing from "../../../assets/docs/tui-session-editing.png";
import sessionApproval from "../../../assets/docs/tui-session-approval.png";

export const tuiCaptures = {
  sessionWelcome: {
    image: sessionWelcome,
    alt: "Wisp welcome screen with the Braille mascot, session shortcuts, prompt composer, and side panel showing context, MCP servers, skills, and changed files.",
    caption:
      "Start a task in your terminal, resume a session, or browse the available commands.",
    provenance: "Real session screenshot · full interface.",
  },
  sessionEditing: {
    image: sessionEditing,
    alt: "Wisp editing its own documentation, with inline diffs and a side panel tracking context, skills, changed files, and recent turns.",
    caption:
      "Follow repository exploration and edits inline, with context and changed files alongside the conversation.",
    provenance: "Real session screenshot · window chrome cropped.",
  },
  sessionApproval: {
    image: sessionApproval,
    alt: "Wisp session awaiting approval for git status --short, with the conversation, side panel, and choices to allow once, allow bash for this session, enable YOLO for this session, or deny.",
    caption:
      "Inspect the proposed command and choose whether to allow it once, allow a broader scope for the session, or deny it.",
    provenance:
      "Real session screenshot · zoomed view; full session available in the image link.",
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
