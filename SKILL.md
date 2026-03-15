---
name: install-skill-from-github
description: Use when the user wants to inspect, safety-check, and install or sync a Codex skill from a GitHub repository or GitHub tree URL. This skill verifies that the target looks like a real skill (contains SKILL.md), performs a conservative safety scan, and only then installs it into the local Codex skills directory.
---

# Install Skill From GitHub

## Overview

Use this skill when the user provides a GitHub repo, repo path, or GitHub tree URL and wants Codex to pull a skill into the local skills directory.

The workflow is:

1. Run `scripts/sync_skill_from_github.py --check-only ...` on the candidate source.
2. Confirm that the report says `is_skill: true` and `safe_to_install: true`.
3. If safe, rerun the same command without `--check-only` to install via the system `skill-installer`.
4. Tell the user where the skill was installed and remind them to restart Codex.

## Inputs

The helper script accepts either:

- `--url https://github.com/<owner>/<repo>/tree/<ref>/<path>`
- `--repo <owner>/<repo> --path <path> [--ref <ref>]`

If the repo root itself is a skill, `--path` can be omitted.

## Safety policy

- Be conservative.
- Do not install if the report says `safe_to_install: false`.
- Treat symlinks, destructive shell patterns, `curl|bash`-style commands, and dangerous code-execution patterns as blockers.
- Summarize any findings before installing.
- If the candidate is not a skill, stop and explain why.

## Commands

Check only:

```bash
python3 scripts/sync_skill_from_github.py --check-only --url <github-tree-url>
```

Install after a clean check:

```bash
python3 scripts/sync_skill_from_github.py --url <github-tree-url>
```

Repo and path form:

```bash
python3 scripts/sync_skill_from_github.py --check-only --repo owner/repo --path skills/example-skill --ref main
```

## Notes

- The actual installation is delegated to the system `skill-installer` located under `$CODEX_HOME/skills/.system/skill-installer/`.
- The installer does not overwrite an existing destination skill directory. If the target name already exists, move or rename the old copy first.
- After installing a new skill, remind the user to restart Codex.

## Resources (optional)

### scripts/
Use `scripts/sync_skill_from_github.py` for both the conservative inspection pass and the actual install step.
