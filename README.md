# install-skill-from-gh

A Codex skill that inspects a GitHub-hosted candidate, checks whether it is a real skill, performs a conservative safety scan, and only then installs it into the local Codex skills directory.

## Files

- `SKILL.md`: skill instructions for Codex
- `scripts/sync_skill_from_github.py`: check and install helper
- `agents/openai.yaml`: UI metadata

## Example

```bash
python3 scripts/sync_skill_from_github.py --check-only --url "https://github.com/owner/repo/tree/main/path/to/skill"
python3 scripts/sync_skill_from_github.py --url "https://github.com/owner/repo/tree/main/path/to/skill"
```
