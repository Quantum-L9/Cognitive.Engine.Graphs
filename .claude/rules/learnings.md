# Agent Learnings

This file captures non-obvious patterns and gotchas discovered by agents working in this repo.
Agents should append new learnings here when they discover patterns that would help future sessions.
Review periodically and promote stable learnings to CLAUDE.md or relevant rule files.

## Format
- **[date] Category: description** — brief explanation

## Learnings
<!-- Agents: append new entries below this line -->
- **[2026-08-03] Governance/git: the L9 memory-lock PreToolUse gate denies a *compound* `git add && git commit` as a single unit** — when the gate blocks the Bash call, `git add` never runs either, so only previously-staged changes get committed and the commit silently diverges from your working tree. Run `git add` in a Bash call separate from the (lock-gated) `git commit`, and verify with `git show HEAD:<path>` after every governed commit — "validated locally" reflects the working tree, not necessarily what landed.
- **[2026-08-03] Governance/memory-lock: a lock acquired by the `memory_lock.py` CLI must match the gate's session + state root** — the gate compares `lock.session_id` against the real hook `CLAUDE_CODE_SESSION_ID` and reads state from `CLAUDE_PROJECT_DIR` (=`/home/user`). Running the CLI manually with no hook event writes `session_id="unknown-session"` and anchors state at the cwd fallback, so the gate ignores it. Run `memory_prefetch.py` with `{"session_id": "$CLAUDE_CODE_SESSION_ID"}` on stdin, then acquire with `CLAUDE_PROJECT_DIR=/home/user`. Locks also have a short TTL — re-acquire before each governed write.
