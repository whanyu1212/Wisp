import diffs from "../../../assets/docs/tui-diff-previews.png";
import approval from "../../../assets/docs/tui-approval.png";
import mcp from "../../../assets/docs/tui-mcp.png";
import working from "../../../assets/docs/tui-new-turn.png";
import showcase from "../../../assets/docs/tui-showcase.png";

export const tuiCaptures = {
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
