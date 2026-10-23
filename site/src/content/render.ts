import { load } from "cheerio";
import { posix } from "node:path";
import { changelog, type PageMetadata } from "./metadata.ts";
import { base, href } from "./paths.ts";

export interface LegacyHeading {
  id: string;
  level: number;
  text: string;
  destination: string;
}
export interface LegacyPage {
  source: string;
  legacy_url: string;
  destination: string;
  anchors: LegacyHeading[];
  aliases: string[];
}
export interface Heading {
  depth: number;
  slug: string;
  text: string;
}

/** Resolve authored Markdown paths against source identities, never output directories. */
export function resolveLink(
  url: string,
  source: string,
  pages: PageMetadata[],
): string {
  if (!url || url.startsWith("#")) return url;
  const changelogUrl =
    "https://github.com/whanyu1212/Wisp/blob/main/CHANGELOG.md";
  if (url === changelogUrl || url.startsWith(`${changelogUrl}#`))
    return href(changelog.route) + url.slice(changelogUrl.length);
  // The changelog is authored at the repository root. Older releases retain
  // links using the former site/ source directory; both resolve to current pages.
  if (source === changelog.source)
    url = url.replace(/^(?:\.\/)?(?:book\/src\/|site\/)/, "");
  const publicPrefix = "https://whanyu1212.github.io/Wisp/";
  if (url.startsWith(publicPrefix)) url = base + url.slice(publicPrefix.length);
  if (/^(?:[a-z][\w+.-]*:|\/\/)/i.test(url)) return url;
  const pathname = url.split(/[?#]/, 1)[0] ?? "";
  const suffix = url.slice(pathname.length);
  if (!pathname) return url;
  const normalized = pathname.startsWith(base)
    ? pathname.slice(base.length)
    : posix.normalize(posix.join(posix.dirname(source), pathname));
  const candidates = new Set([
    normalized,
    normalized.replace(/\.html$/, ".md"),
    `${normalized}.md`,
    posix.join(normalized, "index.md"),
  ]);
  const target = pages.find(
    (page) => candidates.has(page.source) || page.route === normalized,
  );
  if (target) return href(target.route) + url.slice(pathname.length);
  // Source links to repository files become explicit repository links, never broken site URLs.
  if (normalized.startsWith("../../"))
    return `https://github.com/whanyu1212/Wisp/blob/main/${normalized.slice(6)}${suffix}`;
  throw new Error(`Unresolved link in ${source}: ${url}`);
}

export function finishHtml(
  html: string,
  page: PageMetadata,
  pages: PageMetadata[],
  legacy?: LegacyPage,
): { html: string; headings: Heading[] } {
  const $ = load(html, null, false);
  const headings: Heading[] = [];
  const known = new Map<string, LegacyHeading[]>();
  for (const heading of legacy?.anchors ?? []) {
    const key = `${heading.level}:${heading.text}`;
    known.set(key, [...(known.get(key) ?? []), heading]);
  }
  const ids = new Set<string>();
  $("h1,h2,h3,h4,h5,h6").each((_i, element) => {
    const depth = Number(element.tagName[1]);
    const raw = $(element).text().trim();
    const explicit = /\s+\{#([a-zA-Z0-9_-]+)\}$/.exec(raw);
    if (explicit)
      $(element).html(
        ($(element).html() ?? "").replace(/\s+\{#[a-zA-Z0-9_-]+\}$/, ""),
      );
    const text = $(element).text().trim();
    const old = known.get(`${depth}:${text}`)?.shift();
    const slug = explicit?.[1] ?? old?.id ?? $(element).attr("id");
    if (!slug || ids.has(slug))
      throw new Error(
        `Missing or duplicate heading ID in ${page.source}: ${text}`,
      );
    ids.add(slug);
    $(element).attr("id", slug);
    headings.push({ depth, slug, text });
  });
  $("a[href]").each((_i, element) => {
    $(element).attr(
      "href",
      resolveLink($(element).attr("href")!, page.source, pages),
    );
  });
  $("img").each((_i, element) => {
    const src = $(element).attr("src") ?? "";
    if (
      src ===
      "https://raw.githubusercontent.com/whanyu1212/Wisp/main/assets/wisp-banner-v2.png"
    ) {
      $(element).attr("src", `${base}assets/wisp-banner-v2.png`);
    } else if (!/^(?:https?:|data:|\/)/.test(src)) {
      throw new Error(`Unregistered local image in ${page.source}: ${src}`);
    }
    $(element).attr("loading", "lazy");
  });
  $("blockquote").each((_i, element) => {
    const first = $(element).children("p").first();
    const type = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]/.exec(
      first.text(),
    );
    if (type) {
      first.html((first.html() ?? "").replace(/^\[![A-Z]+\]\s*/, ""));
      $(element).addClass(`notice notice-${type[1]!.toLowerCase()}`);
      $(element).prepend(
        $('<strong class="notice-label"></strong>').text(type[1]!),
      );
    }
  });
  $('pre code.language-mermaid, pre[data-language="mermaid"] code').each(
    (_i, element) => {
      const source = $(element).text();
      const figure = $('<figure class="diagram"></figure>');
      figure.append(
        $(
          '<div class="mermaid" tabindex="0" role="region" aria-label="Diagram"></div>',
        ).attr("data-source", source),
      );
      const fallback = $(
        '<details class="diagram-source" open><summary>Diagram source</summary></details>',
      );
      fallback.append($("<pre></pre>").append($("<code></code>").text(source)));
      figure.append(fallback);
      $(element).parent("pre").replaceWith(figure);
    },
  );
  $("pre").attr("tabindex", "0").attr("aria-label", "Code listing");
  $("table").each((index, table) => {
    // Give keyboard users a named scroll region even when the table has no caption.
    const label =
      $(table).find("caption").text().trim() || `Table ${index + 1}`;
    $(table).wrap(
      $('<div class="table-scroll" tabindex="0" role="region"></div>').attr(
        "aria-label",
        label,
      ),
    );
  });
  return { html: $.html(), headings };
}
