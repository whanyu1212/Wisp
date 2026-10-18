# Brand images

Each image exists exactly once in the repository. `tests/repository/test_images.py` fails if two
tracked image files have identical bytes, so link to the existing file instead of copying it.

| File | Size | Used by |
| --- | --- | --- |
| `assets/wisp-banner-v2.png` | 2400 × 600 | The README banner, the book's home page, and the README of every published release on PyPI |
| `assets/wisp-avatar.png` | 1024 × 1024 | Master mascot image for avatars and social profiles; upload copies of it, nothing links it |
| `book/theme/favicon.png` | 256 × 256 | The book's browser-tab icon (mdBook picks it up automatically) |
| `book/src/apple-touch-icon.png` | 180 × 180 | The book's iOS home-screen icon, linked from `book/theme/head.hbs` |

## Rules

- **Never rename or delete `wisp-banner-v2.png`.** Every published README, `0.1.0rc1` onward, loads
  it from `main` through `raw.githubusercontent.com`. To change the banner, replace this file's
  contents in place; the README, the book and old PyPI pages all pick it up.
- **The book links the banner here** by its `raw.githubusercontent.com` URL instead of keeping a
  copy, because mdBook only serves files inside `book/src/`.
- **Sizes are not copies.** The favicon and touch icon are smaller renderings of the mascot that
  browsers need at those sizes. When the mascot changes, regenerate them from
  `wisp-avatar.png`.
