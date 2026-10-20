import { fileURLToPath } from "node:url";

declare const __WISP_REPO_ROOT__: string;

// Astro bundles endpoints into dist; preserve the build-time repository location.
export const repoRoot =
  typeof __WISP_REPO_ROOT__ !== "undefined"
    ? __WISP_REPO_ROOT__
    : fileURLToPath(new URL("../../../", import.meta.url));
export const bookRoot = fileURLToPath(
  new URL("../../../book/src/", import.meta.url),
);
export const metadataPath = fileURLToPath(
  new URL("../../content-map.json", import.meta.url),
);
export const baselinePath = fileURLToPath(
  new URL("../../../book/migration/astro/routes.json", import.meta.url),
);
export const base = "/Wisp/";

export function href(route: string): string {
  return base + route.replace(/(^|\/)index\.html$/, "$1");
}
