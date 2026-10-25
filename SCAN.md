# Security and personal-data scan

Scan date: 2026-10-25. Scope: the `agent-watch` working tree and all git history
(1 commit at the time of the initial scan). Values are intentionally not reproduced
here; only file, line, and category are recorded.

## Tools

- `gitleaks detect --no-git --source .` — working tree. Result: no leaks found.
- `gitleaks detect --source .` — git history. Result: 1 commit scanned, no leaks found.
- `git grep` and `git log --all -p | grep` for the categories below.
- `trufflehog` was not installed, so it was not used.

## Categories checked

`kudi68`, `kug6868`, `/Users/`, hook/webhook URLs, `token`, `secret`, `api_key`,
`Bearer`, `@gmail`, `telegram`, plus a generic email-address pattern.

## Findings

| File | Line(s) | Category | Assessment |
| --- | --- | --- | --- |
| `arch.json` | 53, 88 | `telegram` (display label) | Benign. "Telegram" is the `model` label of the Hermes node in the example architecture, not a bot token or chat id. |
| `shots/once-80.txt` | 244, 261, 271, 284, 297, 311 | `telegram` (display label) | Benign. Rendered text snapshot of the same example node. |
| `shots/once-120.txt` | 203, 217, 226, 238, 250, 262 | `telegram` (display label) | Benign. Rendered text snapshot of the same example node. |
| `README.md` | 23 | `kudi68` | Intentional. Clone URL for the planned public repository `kudi68/agent-watch`. |
| `LICENSE` | 3 | `kudi68` | Intentional. MIT copyright holder for the public repository. |
| `test_watch.py` | 800 | email pattern | Benign. `agent-watch-test@example.invalid`, a throwaway identity for a temporary test repo. |
| `tests/test_night_watch.sh` | 35, 38, 44 | email pattern | Benign. `test@example.invalid`, a throwaway identity for temporary test repos. |

No occurrences of `/Users/`, `kug6868`, webhook URLs, bearer tokens, API keys, secrets,
Gmail addresses, or Telegram chat ids were found in the working tree or history.

## Personal path removal

Hardcoded personal locations in the source were replaced with environment variables:

| Original location | Replacement |
| --- | --- |
| `watch.py` run-directory root | `AGENT_WATCH_RUNS_ROOT` (default `~/Projects/night`) |
| `watch.py` runs registry | `AGENT_WATCH_RUNS` |
| `watch.py` archive registry | `AGENT_WATCH_DONE` |
| `watch.py` Codex sessions | `AGENT_WATCH_CODEX_SESSIONS` |
| `watch.py` architecture file | `AGENT_WATCH_ARCH` |
| `watch.py` quota helper | `AGENT_WATCH_MAGPIE` |
| `scripts/night-watch.sh` example log root | `AGENT_WATCH_RUNS_ROOT` |
| `tests/test_night_watch.sh` helper location | repo-relative default, `NIGHT_WATCH_SCRIPT` override |
| `shots/once-*.txt` captured absolute paths | replaced with `/home/user/...` placeholders |

## Repository state

- Default branch: `main`.
- Remotes: none.
- Ignored: `.venv/`, `__pycache__/`, `*.pyc`, `*.bak`, `*.log`, `.env*`, `.DS_Store`,
  generated `shots/*.svg`.
