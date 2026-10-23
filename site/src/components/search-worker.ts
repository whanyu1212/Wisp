import type {
  SearchRequest,
  SearchResponse,
  SearchResult,
} from "./search-client";

interface Pagefind {
  search(
    query: string,
  ): Promise<{ results: { data(): Promise<SearchResult> }[] }>;
}
interface PagefindModule {
  createInstance(options: {
    basePath: string;
    baseUrl: string;
    language: string;
  }): Pagefind;
}

let api: Promise<Pagefind> | undefined;
let downloadFailed = false;
const fetchResource = globalThis.fetch.bind(globalThis);
// Pagefind logs and swallows index-chunk download failures. Observe downloads
// only inside this dedicated worker so failures cannot masquerade as no results,
// without wrapping fetch for the page or any other feature (e.g. diagrams).
globalThis.fetch = async (...args: Parameters<typeof fetch>) => {
  try {
    const response = await fetchResource(...args);
    if (!response.ok) throw new Error("Search download failed");
    return response;
  } catch (error) {
    downloadFailed = true;
    throw error;
  }
};

self.addEventListener("message", async (event: MessageEvent<SearchRequest>) => {
  const { id, query, offset, bundle } = event.data;
  try {
    api ??= (async () => {
      const module: PagefindModule = await import(/* @vite-ignore */ bundle);
      return module.createInstance({
        basePath: new URL("./", bundle).href,
        baseUrl: new URL("../", bundle).pathname,
        language: "en",
      });
    })();
    const response = await (await api).search(query);
    const results = await Promise.all(
      response.results.slice(offset, offset + 8).map((match) => match.data()),
    );
    if (downloadFailed) throw new Error("Search download failed");
    self.postMessage({
      id,
      results,
      total: response.results.length,
    } satisfies SearchResponse);
  } catch {
    self.postMessage({
      id,
      results: [],
      total: 0,
      failed: true,
    } satisfies SearchResponse);
  }
});
