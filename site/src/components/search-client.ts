export interface SearchResult {
  url: string;
  excerpt: string;
  meta: { title?: string; section?: string };
}
export interface SearchRequest {
  id: number;
  query: string;
  offset: number;
  bundle: string;
}
export interface SearchResponse {
  id: number;
  results: SearchResult[];
  total: number;
  failed?: boolean;
}

let worker: Worker | undefined;
let nextId = 0;
const pending = new Map<number, (response: SearchResponse) => void>();

/** Load the search worker only after a nonempty query, never while reading. */
export function searchResults(
  bundle: string,
  query: string,
  offset: number,
): Promise<SearchResponse> {
  worker ??= new Worker(new URL("./search-worker.ts", import.meta.url), {
    type: "module",
  });
  worker.onmessage = (event: MessageEvent<SearchResponse>) => {
    pending.get(event.data.id)?.(event.data);
  };
  worker.onerror = () => {
    for (const [id, finish] of pending)
      finish({ id, results: [], total: 0, failed: true });
    worker?.terminate();
    worker = undefined;
  };
  const id = nextId++;
  return new Promise((resolve, reject) => {
    // Failed or indefinitely stalled downloads must not leave a permanent spinner.
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error("Search timed out"));
    }, 15_000);
    pending.set(id, (response) => {
      clearTimeout(timeout);
      pending.delete(id);
      if (response.failed) reject(new Error("Search download failed"));
      else resolve(response);
    });
    worker!.postMessage({ id, bundle, query, offset } satisfies SearchRequest);
  });
}
