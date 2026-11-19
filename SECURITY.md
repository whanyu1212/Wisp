# Security policy

Please report security vulnerabilities privately — don't open a public issue or PR.

- **Preferred:** [Report a vulnerability](https://github.com/whanyu1212/Wisp/security/advisories/new) via GitHub's **Security** tab.
- **Or email:** `whanyu1212@hotmail.com` with `"Wisp security"` in the subject.

Include your Wisp version (`wisp --version`), OS, install method, and minimal steps to reproduce — with any API keys, tokens, or private files stripped out.

## Supported versions & scope

Wisp has one maintainer, so only the **latest published release** receives security fixes (no backports to older minor versions).

In scope are flaws that bypass Wisp's tool approvals, protected paths, project trust, credential isolation, or sandbox boundaries (see [Tools & safety](https://whanyu1212.github.io/Wisp/guide/tools-and-safety.html) for how those boundaries work).
