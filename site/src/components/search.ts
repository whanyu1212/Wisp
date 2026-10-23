import { searchResults } from "./search-client";

const opener = document.querySelector<HTMLButtonElement>("#search-open")!;
const dialog = document.querySelector<HTMLDialogElement>("#search-dialog")!;
const close = document.querySelector<HTMLButtonElement>("#search-close")!;
const form = document.querySelector<HTMLFormElement>("#search-form")!;
const input = document.querySelector<HTMLInputElement>("#search-query")!;
const status = document.querySelector<HTMLElement>("#search-status")!;
const results = document.querySelector<HTMLOListElement>("#search-results")!;
const more = document.querySelector<HTMLButtonElement>("#search-more")!;
const bundle = new URL(dialog.dataset.searchBundle!, location.origin).href;
const baseUrl = new URL("../", bundle);
let revision = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let loaded = 0;

function failed(current: number): void {
  if (current !== revision) return;
  results.replaceChildren();
  more.hidden = true;
  status.textContent =
    "Search is unavailable. Reload the page to retry, or keep browsing the documentation.";
}

async function showMore(current: number): Promise<void> {
  const restoreFocus = document.activeElement === more;
  more.disabled = true;
  results.setAttribute("aria-busy", "true");
  status.textContent = "Loading search results…";
  try {
    const { results: batch, total } = await searchResults(
      bundle,
      input.value.trim(),
      loaded,
    );
    // A slow query or fragment fetch must never overwrite a newer query/close.
    if (current !== revision) return;
    const fragment = document.createDocumentFragment();
    for (const result of batch) {
      const url = new URL(result.url, location.origin);
      if (
        url.origin !== location.origin ||
        !url.pathname.startsWith(baseUrl.pathname)
      )
        throw new Error("Search result is outside the documentation site");
      const item = document.createElement("li");
      const section = document.createElement("p");
      section.className = "search-section";
      section.textContent = result.meta.section ?? "Documentation";
      const link = document.createElement("a");
      link.href = url.href;
      link.textContent = result.meta.title ?? "Documentation";
      const excerpt = document.createElement("p");
      // Pagefind escapes source HTML, adding only its own <mark> highlights.
      excerpt.innerHTML = result.excerpt;
      item.append(section, link, excerpt);
      fragment.append(item);
    }
    const firstNewLink = fragment.querySelector("a");
    results.append(fragment);
    loaded += batch.length;
    more.hidden = loaded >= total;
    more.disabled = false;
    status.textContent = total
      ? `Showing ${loaded} of ${total} results for “${input.value.trim()}”.`
      : `No results for “${input.value.trim()}”. Try another term.`;
    // Disabling the button during loading can drop focus to the document body.
    // Restore it only if the reader hasn't moved to another control meanwhile.
    if (restoreFocus && document.activeElement === document.body)
      (more.hidden ? firstNewLink : more)?.focus();
  } catch {
    failed(current);
  } finally {
    if (current === revision) {
      more.disabled = false;
      results.setAttribute("aria-busy", "false");
      if (restoreFocus && document.activeElement === document.body)
        input.focus();
    }
  }
}

function search(delay: number): void {
  clearTimeout(timer);
  const current = ++revision;
  const query = input.value.trim();
  results.replaceChildren();
  loaded = 0;
  more.hidden = true;
  results.setAttribute("aria-busy", "false");
  if (!query) {
    status.textContent = "Type to search the documentation.";
    return;
  }
  status.textContent = "Loading search results…";
  timer = setTimeout(async () => {
    results.setAttribute("aria-busy", "true");
    try {
      await showMore(current);
    } catch {
      failed(current);
    } finally {
      if (current === revision) results.setAttribute("aria-busy", "false");
    }
  }, delay);
}

opener.addEventListener("click", () => {
  dialog.showModal();
  input.focus();
  search(0);
});
close.addEventListener("click", () => dialog.close());
dialog.addEventListener("keydown", (event) => {
  // Search inputs can consume Escape to clear themselves before native dialog
  // dismissal. Always close, and keep Tab cycling inside the modal's controls.
  if (event.key === "Escape") {
    event.preventDefault();
    dialog.close();
  } else if (event.key === "Tab") {
    const controls = dialog.querySelectorAll<HTMLElement>(
      "button:not([hidden]):not(:disabled), input, a[href]",
    );
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }
});
dialog.addEventListener("close", () => {
  clearTimeout(timer);
  revision++;
  opener.focus();
});
input.addEventListener("input", () => search(200));
form.addEventListener("submit", (event) => {
  event.preventDefault();
  search(0);
});
more.addEventListener("click", () => void showMore(revision));
// No dead entry point when JS is disabled or native modal dialogs are unavailable.
if (typeof dialog.showModal === "function") opener.hidden = false;
