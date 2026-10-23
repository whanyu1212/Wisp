import { load } from "cheerio";
import type { PageMetadata } from "./metadata.ts";
import type { Heading } from "./render.ts";
import { href } from "./paths.ts";

export const tuiSource = "guide/tui.md";
type Part = "basics" | "keybindings" | "commands" | "sessions";

export const tuiChildren: PageMetadata[] = [
  [
    "keybindings",
    "TUI keys and keybindings",
    "Keys",
    "Keyboard shortcuts, editing keys, and user keybinding configuration.",
  ],
  [
    "commands",
    "TUI commands",
    "Commands",
    "Slash commands, provider and model selection, permissions, and MCP management.",
  ],
  [
    "sessions",
    "TUI sessions and scrollback",
    "Sessions",
    "Resume sessions, navigate branches, browse tool details, and search prompt history.",
  ],
].map(([part, title, navTitle, description], order) => ({
  source: `guide/tui/${part}.md`,
  route: `guide/tui/${part}.html`,
  title: title!,
  navTitle: navTitle!,
  description: description!,
  section: "using",
  order,
  layout: "docs",
  listed: true,
  searchable: true,
  parent: tuiSource,
}));

// Temporary adapter until HY-316 moves the Markdown sources. Boundaries partition
// the rendered source in order: every original node belongs to exactly one page.
// Paragraph boundaries account for the task instructions inside the long intro.
const boundaries: { start: string; part: Part; heading?: string }[] = [
  {
    start: "Ctrl+G lists every resolved binding.",
    part: "sessions",
    heading: "Reviewing tool output and changes",
  },
  {
    start: "A pending tool approval docks a panel",
    part: "basics",
    heading: "Approvals and trust",
  },
  {
    start: "The Rust TUI validates the live RPC handshake",
    part: "sessions",
    heading: "Resuming and starting sessions",
  },
  {
    start: "`/connect` opens",
    part: "commands",
    heading: "Connecting providers",
  },
  {
    start: "The Rust TUI also exposes direct persisted-session workflows",
    part: "sessions",
    heading: "Session branches",
  },
  {
    start: "Rust also supports `/model`",
    part: "commands",
    heading: "Choosing a model",
  },
  {
    start: "Rust pickers, help, context",
    part: "basics",
    heading: "Panels and popups",
  },
  {
    start: "The Rust composer supports `@`",
    part: "basics",
    heading: "Project file references",
  },
  {
    start: "Rust supports `/theme`",
    part: "basics",
    heading: "Themes and appearance",
  },
  {
    start: "Rust mouse navigation is",
    part: "sessions",
    heading: "Mouse navigation and scrollback",
  },
  {
    start: "A missing or non-executable binary",
    part: "basics",
    heading: "Startup errors",
  },
  { start: "#rust-keybinding-preferences", part: "keybindings" },
  { start: "#rust-large-pastes", part: "basics" },
  { start: "Unlike print mode,", part: "commands", heading: "Permissions" },
  { start: "#steering-and-follow-ups", part: "basics" },
  { start: "#slash-commands", part: "commands" },
  { start: "#completions-and-the-file-picker", part: "basics" },
  { start: "#keybindings", part: "keybindings" },
  { start: "The Rust transcript retains", part: "sessions" },
  { start: "Run `/theme` to preview", part: "basics" },
  { start: "`Ctrl+G` and `/help`", part: "keybindings" },
  { start: "#prompt-history", part: "sessions" },
  { start: "#context-skills-and-mcp-browsers", part: "commands" },
  { start: "#side-panel", part: "basics" },
];

export function prepareTui(body: string): string {
  for (const { start } of boundaries.filter(
    ({ start }) => !start.startsWith("#"),
  )) {
    const marker = `\n${start}`;
    if (body.split(marker).length !== 2)
      throw new Error(`Ambiguous TUI paragraph boundary: ${start}`);
    body = body.replace(marker, `\n${marker}`);
  }
  return body;
}

export function splitTui(html: string) {
  const $ = load(html, null, false);
  const parts: Record<Part, string[]> = {
    basics: [],
    keybindings: [],
    commands: [],
    sessions: [],
  };
  const moved: { id: string; text: string; target: string }[] = [];
  let part: Part = "basics";
  let boundary = 0;
  for (const node of $.root().children().toArray()) {
    const element = $(node);
    const next = boundaries[boundary];
    if (
      next &&
      (next.start.startsWith("#")
        ? element.attr("id") === next.start.slice(1)
        : element.text().startsWith(next.start.replaceAll("`", "")))
    ) {
      part = next.part;
      boundary++;
      if (next.heading) {
        const slug = next.heading.toLowerCase().replaceAll(" ", "-");
        parts[part].push(`<h2 id="${slug}">${next.heading}</h2>`);
      }
    }
    const id = element.attr("id");
    if (id && /^h[1-6]$/.test(node.tagName)) {
      const text = element.text().replace(/^Rust /, "");
      element.text(id === "tui" ? "TUI basics" : text);
      if (part !== "basics")
        moved.push({
          id,
          text,
          target: `${href(`guide/tui/${part}.html`)}#${id}`,
        });
    }
    parts[part].push($.html(node));
  }
  if (boundary !== boundaries.length)
    throw new Error(
      `Missing TUI split boundary: ${boundaries[boundary]!.start}`,
    );
  return (source: string): { html: string; headings: Heading[] } => {
    const part: Part =
      source === tuiSource
        ? "basics"
        : (source.slice("guide/tui/".length, -3) as Part);
    const title = tuiChildren.find((page) => page.source === source)?.title;
    const content = load(parts[part].join("\n"), null, false);
    if (part === "basics") {
      const links = tuiChildren
        .map(
          (child) =>
            `<li><a href="${href(child.route)}">${child.title}</a> — ${child.description}</li>`,
        )
        .join("");
      content("h1").after(
        `<p>Choose a focused guide for common terminal tasks:</p><ul>${links}</ul>`,
      );
    }
    if (part !== "basics") {
      content("h3").each((_index, node) => {
        node.tagName = "h2";
      });
    }
    const selected: Heading[] = content("h1,h2,h3,h4,h5,h6")
      .toArray()
      .map((node) => ({
        depth: Number(node.tagName[1]),
        slug: content(node).attr("id")!,
        text: content(node).text(),
      }));
    let output =
      (title ? `<h1 id="tui-${part}">${title}</h1>\n` : "") + content.html();
    if (part === "basics") {
      output +=
        '<section data-pagefind-ignore aria-label="Moved TUI sections"><h2>More TUI tasks</h2><ul>';
      for (const child of tuiChildren)
        output += `<li><a href="${href(child.route)}">${child.title}</a></li>`;
      output +=
        "</ul><p>Previously linked sections now live on focused pages:</p><ul>";
      for (const item of moved)
        output += `<li id="${item.id}"><a data-tui-destination href="${item.target}">${item.text}</a></li>`;
      output += "</ul></section>";
    }
    return { html: output, headings: selected };
  };
}
