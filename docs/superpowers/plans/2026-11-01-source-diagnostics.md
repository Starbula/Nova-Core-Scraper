# Source Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make source participation and failures observable while preserving successful fallbacks and paid in-flight responses.

**Architecture:** Keep one frozen FallbackProvider chain per run. Add lock-protected bounded aggregate snapshots and worker-local per-game traces; persist traces using the existing SQLite migration mechanism. Render only seven source rows on Tk.

**Tech Stack:** Existing Python 3.11+, requests, Tk/ttk, SQLite and pytest.

**Spec:** `docs/superpowers/specs/2026-11-01-source-diagnostics-design.md`

## Global Constraints

- Never persist credentials or request URLs in diagnostic output.
- Preserve ROMs, existing artwork and pending metadata.
- Keep network/filesystem work off Tk and obey provider quotas/rate limits.
- Existing user authorization covers implementation and publication to main.

## Review Focus

- A quota-disabled source must not hide healthy successful/no-match attempts.
- Concurrent workers must not mix another game's source trace.
- Cached preparation must not imply verified account credentials.
- Old recovery databases must migrate without losing pending artwork.
- A paid HTTP200 must remain readable after another worker is denied by local quota.

### Task 1: Observable provider chain and durable traces

**Files:** `providers.py`, `sources.py`, `engine.py`, `state.py`; focused source/engine/state/provider tests.

**Interfaces:** `SourceSnapshot(name,state,detail,lookups,matches,downloads,requests,api_requests,remaining)`, `FallbackProvider.source_statuses()`, `FallbackProvider.attempt_summary()`, `_Provider.request_stats()`, `JobSnapshot.source_trace`.

- [x] Write and run failing tests for quota plus no-match plus healthy fallback, actual dispatch counts, concurrent trace isolation and legacy-state migration.
- [x] Add bounded snapshots, safe trace formatting, actual dispatch counters, engine recording and CSV/history preservation.
- [x] Run source, provider, engine and state tests.

### Task 2: Sources UI and matching/quota regressions

**Files:** `ui.py`, `api_sources.py`, `launchbox.py`, matching/UI/workflow/API tests, README/provider notes.

- [x] Reproduce in-flight quota denial before fixing it; prove real authentication failures still stop responses.
- [x] Add only verified equivalent RetroBat aliases and focused mapping tests.
- [x] Add Sources tab, frozen order, visible configuration exclusions and expandable complete game details; try IGDB before TheGamesDB in Automatic.
- [x] Test source snapshots, five tabs, selected-console scope, failure-to-healthy-source downloads, source-trace export and responsive updates with actual Tk.
- [ ] Review the changes, run full fixtures/source and packaged checks on Windows CI, then publish the portable artifact and main commit.
