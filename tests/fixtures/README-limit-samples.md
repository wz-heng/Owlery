# Usage-limit samples — captured live, 2026-07-14

Captured by pointing each CLI at a local HTTP upstream returning a real 429
envelope, then running the CLI's real spawn → stream-json path. No quota spent;
the model is never reached. Reproduction upstreams: see limit-auto-resume.md §4.

claude-code 2.1.209 / codex-cli 0.142.5.

| fixture | what it is |
|---|---|
| `limit_claude_user_5h.jsonl`      | the USER'S OWN 5-hour limit (429 + `anthropic-ratelimit-unified-representative-claim: five_hour`) |
| `limit_claude_transient_429.jsonl`| a SERVER-side 429, same status code, NOT the user's limit |
| `limit_codex_user.jsonl`          | codex user usage limit (429 + `usage_limit_reached` body) |
| `limit_codex_transient_429.jsonl` | codex server-side 429 (RPM throttle) |

The two claude files are the important pair: **identical HTTP status, opposite
disposition.** Any classifier keyed on "429"/"rate limit" merges them.
