# Brand images

Each image exists exactly once in the repository. `tests/repository/test_images.py` fails if two
tracked image files have identical bytes, so link to the existing file instead of copying it.

| File | Size | Used by |
| --- | --- | --- |
| `assets/wisp-banner-v2.png` | 2400 × 600 | The README banner, the book's home page, and the README of every published release on PyPI. Rendered by `site/scripts/render-banner.ts` from the TUI captures and the `12.58.53` screenshot |
| `assets/wisp-avatar.png` | 1024 × 1024 | Master mascot image for avatars and social profiles; upload copies of it, nothing links it |
| `assets/favicon.png` | 256 × 256 | Browser-tab icon emitted by Astro's static asset endpoint |
| `assets/apple-touch-icon.png` | 180 × 180 | iOS home-screen icon linked by the Astro document layout |
| `assets/Screenshot 2026-10-25 at 12.58.*PM.png` | 2254 × 1236 | Reference screenshots of the TUI in the Ember theme that the synthetic captures were modeled on; nothing links them, and the banner script reads `12.58.53` |

Documentation UI captures live in [`docs/`](docs/README.md), with their synthetic-data provenance
and regeneration commands. Each master is stored once; Astro generates responsive derivatives.

## Rules

- **Never rename or delete `wisp-banner-v2.png`.** Every published README, `0.1.0rc1` onward, loads
  it from `main` through `raw.githubusercontent.com`. To change the banner, replace this file's
  contents in place; the README, the book and old PyPI pages all pick it up.
- **Regenerate the banner, don't edit it.** `site/scripts/render-banner.ts` takes the Braille
  mascot from the synthetic captures in [`docs/`](docs/README.md#regenerate) and shows the
  terminal of the `12.58.53` session screenshot in a tilted window, in the Ember palette, so it
  stays in step with the TUI. The wordmark uses the local Georgia font, as the site's headings do.
- **Markdown links the banner here** by its `raw.githubusercontent.com` URL; Astro rewrites that URL
  to its local asset endpoint. Do not add another tracked copy.
- **Sizes are not copies.** The favicon and touch icon are smaller renderings of the mascot that
  browsers need at those sizes. When the mascot changes, regenerate them from
  `wisp-avatar.png`.
