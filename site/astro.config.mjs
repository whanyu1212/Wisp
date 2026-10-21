import { fileURLToPath } from "node:url";
import { defineConfig } from "astro/config";
import { unified } from "@astrojs/markdown-remark";

export default defineConfig({
  site: "https://whanyu1212.github.io",
  base: "/Wisp/",
  output: "static",
  vite: {
    define: {
      __WISP_REPO_ROOT__: JSON.stringify(
        fileURLToPath(new URL("../", import.meta.url)),
      ),
    },
  },
  trailingSlash: "ignore",
  build: { format: "preserve" },
  markdown: {
    processor: unified(),
    syntaxHighlight: { type: "shiki", excludeLangs: ["mermaid"] },
    shikiConfig: {
      themes: { light: "github-light-high-contrast", dark: "github-dark" },
    },
  },
});
