import { readFile } from "node:fs/promises";
import { join } from "node:path";
import type { APIRoute } from "astro";
import { repoRoot } from "../../content/paths";

const assets = {
  "wisp-banner-v2.png": "assets/wisp-banner-v2.png",
  "favicon.png": "book/theme/favicon.png",
  "apple-touch-icon.png": "book/src/apple-touch-icon.png",
};
export function getStaticPaths() {
  return Object.entries(assets).map(([asset, source]) => ({
    params: { asset },
    props: { source },
  }));
}
export const GET: APIRoute = async ({ props }) =>
  new Response(new Uint8Array(await readFile(join(repoRoot, props.source))), {
    headers: { "Content-Type": "image/png" },
  });
