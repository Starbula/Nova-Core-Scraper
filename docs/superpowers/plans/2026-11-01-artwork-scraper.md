# NovaCore Artwork Scraper Implementation Plan

> **For agentic workers:** use focused parallel implementation tasks with shared model contracts, then independent whole-project review. User explicitly authorised routine design decisions, implementation and direct main push.

**Goal:** Select consoles and download missing box artwork independently of RetroBat.
**Architecture:** Tk UI, bounded background executor, SQLite recovery state, provider adapters, read-only scanner and deferred transactional gamelist merger.
**Tech Stack:** Python 3.11+, Tk, Pillow, psutil, requests (provider transport), unittest, PyInstaller.
**Spec:** docs/superpowers/specs/2026-11-01-artwork-scraper-design.md

## Global Constraints
- Windows 10/11, portable executable, preserve MIT copyright (c) 2026 Lu Lu.
- Never write or delete ROMs or overwrite existing artwork; preserve unrelated XML.
- Download box artwork only, no video/ROM downloads or invented matching.
- Do not modify NovaCore Downloader. Credentials never enter logs or Git.
- Defer metadata while RetroBat/EmulationStation runs; resume without duplicate downloads.

## Review Focus
- Drive-letter changes, custom paths and links: portable keys and safe boundaries.
- Existing thumbnail/image paths and nested/disc games: no duplicate or wrong artwork.
- Another writer, malformed XML, interruption: preserve originals and retain pending updates.
- HTTP auth/quota/unavailable provider: bounded safe retry, no secrets in errors.
- Thousands of items and UI closure: bounded futures, throttled updates, durable state.

### Task 1: library discovery (library.py, tests/test_library.py)
- [x] Write failing fixtures for config/custom overrides, fallback, nested games, launch manifests and existing artwork.
- [x] Implement validate_retrobat, detect_installations, discover_systems, scan_system with models.py contracts.
- [x] Run unittest discovery, report exact coverage and limitations.

### Task 2: safe metadata (metadata.py, tests/test_metadata.py)
- [x] Write failing fixtures for preservation, backup, conflicts, malformed XML, existing images and process deferral.
- [x] Implement merge_artwork(system, updates, running) -> accepted game-relative paths; fresh parse plus revision check, atomic replacement and backup.
- [x] Test batch efficiency and interruption safety.

### Task 3: providers (providers.py, tests/test_providers.py)
- [x] Research current primary API/source documentation and record in docs/providers.md.
- [x] Write fixture tests for exact/system/region matching, missing data, auth/quota, transient failures, image validation.
- [x] Implement LibretroProvider and ScreenScraperProvider returning Artwork or None, cancellable rate limiting and verified downloads.
- [x] Test using local HTTP fixtures; no credentialed real-world download claim.

### Task 4: controller and UI (engine.py, state.py, settings.py, ui.py, main.py, tests)
- [x] Write failing controller/state fixtures covering pause/resume/stop, recovery, missing/failure and metadata deferral.
- [x] Implement durable SQLite ledger, installation lock, bounded futures, process gate and background Tk event polling.
- [x] Test integration and 10,000-game fixture scanning; headless UI smoke test where supported.

### Task 5: delivery (requirements, launch/build scripts, Actions, README)
- [x] Add reproducible Windows scripts and build/test workflow with executable self-test.
- [x] Document actual authentication, matching limits, pending metadata, source/build usage.
- [x] Independent code review, fix findings with regression tests, run full suite and build checks.
- [ ] Commit reviewed tree through GitHub API with original remote parent, update main with expected SHA, verify workflow and report result.
