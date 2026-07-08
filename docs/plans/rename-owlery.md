# Rename: Octopus → Owlery

## 1. What & why

The project was cloned from `archeryue/Octopus` and kept its name. It now
lives in the owner's own repo (`wz-heng/Owlery`, renamed on GitHub
2026-07-08) and gets its own identity: **Owlery** — the Hogwarts mail hub,
matching the agent naming universe (Dobby, Snape) and the platform's actual
job: routing messages, delegations and tasks between agents.

The rename is two-phase. Phase 1 (ops, no code — GitHub repo rename, local
directory renames, remotes, service restart) is done by hand outside this
plan. This document is **phase 2: the code-level rename**, one pipeline task.

## 2. Scope

Everything in the codebase that says `octopus` (72 source files reference it
as of writing) becomes `owlery`, including:

- **Package + CLI**: `pyproject.toml` name and entry point — `octopus serve`
  / `handoff` / `pull` become `owlery …`.
- **App home**: `~/.octopus` → `~/.owlery` (config defaults in
  `server/config.py`: `attachments_dir`, `large_prompts_dir`,
  `codex_home_dir`, `agents_dir`, fork/research dirs).
- **Env vars**: `OCTOPUS_*` → `OWLERY_*` (`OCTOPUS_API_BASE`,
  `OCTOPUS_SESSION_ID`, config prefixes — sweep for the full set).
- **Database file**: default `octopus.db` → `owlery.db`.
- **UI branding**: page title, login screen, default agent avatar 🐙 → 🦉,
  any user-visible "Octopus" strings.
- **Docs**: CLAUDE.md, README, docs/, comments; test fixtures/strings.

## 3. Data migration (the actual hard part)

A fresh checkout must Just Work, and an existing install must migrate
losslessly on first startup of the renamed build:

- If `octopus.db` exists and `owlery.db` doesn't → rename the file.
- If `~/.octopus` exists and `~/.owlery` doesn't → rename the directory.
  It holds real state: per-agent memory (`agents/`), attachments, codex
  credential homes (`codex/` — live auth), fork working copies, research.
- **Stored absolute paths**: the DB stores paths that may point into the
  old locations (session `working_dir` for fork sessions under
  `~/.octopus/fork/…`, credential `home_dir`-style values, attachment
  paths). Audit every column/JSON field that persists a path and rewrite
  old-prefix → new-prefix in the same migration. No compat symlink — do
  the rewrite properly.
- Migration must be idempotent and run before anything opens the DB or
  provisions agent dirs.

## 4. Out of scope

- Agent *data* names (the `Octo` default agent, session names, memory
  content) — user data, not code. The user may rename Octo separately.
- Git history rewrite, GitHub redirects (GitHub handles the old URL).
- The owner's launch script / nohup command (`octopus serve` →
  `owlery serve`, log path) — human-side, adjusted at deploy time.

## 5. Sequencing

Run **after** `feat/usage-tracking` merges — this rename touches the whole
tree and must not collide with in-flight work. Phase-1 directory renames
(`~/vibe-coding-project/Octopus{,-dev}` → `Owlery{,-dev}`) also happen
before this task starts, so the dev clone the task runs in is already
named `Owlery-dev`.
