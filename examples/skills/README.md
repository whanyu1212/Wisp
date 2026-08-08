# Example Agent Skill

This directory contains an opt-in Agent Skills example for Wisp. It is not loaded directly from
the repository's `examples/` directory.

Install it for the current trusted project:

```bash
mkdir -p .wisp/skills
cp -R examples/skills/wisp-code-review .wisp/skills/
```

Or install it for the current user:

```bash
mkdir -p ~/.wisp/skills
cp -R examples/skills/wisp-code-review ~/.wisp/skills/
```

Inspect the resulting catalog with `wisp skills`, then invoke the example with:

```text
/skill:wisp-code-review review the current changes
```

The main `SKILL.md` is complete on its own. Its `references/checklist.md` file illustrates
progressive loading: an agent with the read-only `skill` tool can request that resource for a more
detailed audit without placing the full checklist in every prompt.
