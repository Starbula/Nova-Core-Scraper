# Multiple sources and throughput implementation plan

> **For agentic workers:** use subagent-driven development. Root coordinates integration, review, publication and Windows verification; workers own disjoint files.

**Goal:** Retry missing artwork across two usable account-free sources and increase safe parallel throughput.
**Architecture:** LaunchBox streams its official ZIP/XML into an atomic indexed cache; a source chain lazily yields matches and routes downloads. The existing engine consumes candidates, preserves failure distinctions and records source status. Shared provider networking retains authentication, redirects, retries and image safety.
**Tech stack:** existing Python 3.11+, Tk, SQLite, requests, Pillow; no new dependency.
**Spec:** docs/superpowers/specs/2026-11-01-multiple-sources-design.md

## Global constraints
- No ROM writes/deletes/downloads, no artwork overwrites, no borrowed credentials or access bypasses.
- Use temporary fixture libraries and recorded schema fixtures for write tests; preserve responsive background work and cancellation.
- Current official export is XML. Source matching must constrain platform and reject ambiguous titles, hacks and sequels.
- Safe public request floors: Libretro 0.25 seconds, LaunchBox 0.5 seconds; user interval and server limits can increase these.

## Tasks
- [ ] Source implementation: new launchbox.py and sources.py plus tests. LaunchBoxProvider(cache_dir, region='eu', interval=.25, workers=8), prepare(checkpoint), find(game, checkpoint), download(artwork, destination, checkpoint), close(), set_status_callback(callback). FallbackProvider(providers, workers=8) exposes prepare, candidates(game,checkpoint), find, download, close and set_status_callback. Yield matching Artwork lazily; preserve lookup errors and continue other sources; downloads route by Artwork.provider. Parse only actual catalogue rows; write the cache atomically and retain old valid data on failed/cancelled refresh. Test platform/title/alias/region, unsafe filenames, invalid ZIP/XML, cancellation, cache reuse, stale fallback and partial outages.
- [ ] Throughput implementation: providers.py and provider tests. Raise shared worker clamp to16, Libretro cap8/floor.25, add bounded64MiB in-memory image bytes cache and cooperative URL lock coalescing. Cache only validated images; preserve independent output files and no-overwrite publication. Test distinct parallel requests, one request for simultaneous identical URLs, invalid image rejection, bounded eviction, cancellation while waiting and existing retry/rate behavior.
- [ ] Settings/UI: settings.py, ui.py and their tests. Add automatic and launchbox modes; default automatic/8/.25,16-worker maximum,.25 minimum, version2 settings persistence and legacy defaults migration. Add Fast preset; describe source order and the initial catalogue preparation. Keep ScreenScraper preflight truthful; preserve console scope and four tabs. Source imports use launchbox.LaunchBoxProvider and sources.FallbackProvider.
- [ ] Root integration: engine.py and engine/workflow tests. Raise engine worker limit16, consume candidates when available with fallback on failed downloads; retain provider failure versus missing distinction. Set source status callback to progress.message; keep per-game source detail through completion. Expose artwork_counts()->dict[str,int], counting actual covered games once per system (existing plus newly completed), for live UI coverage updates. Exercise real Tk console selection through both source fallbacks, cancellation, live totals and metadata integrity.
- [ ] Final verification: whole fixture suite with real Tk, source self-test, real official catalogue parse and selected live cover downloads if allowed; throughput measurements; independent review; README/provider docs, commit/push main and successful Windows executable CI.

## Review focus
- Catalogue interruption preserves a valid previous cache and publishes no partial index.
- Same title across platforms or multiple game IDs never silently chooses another game.
- One broken source cannot block a healthy fallback or inflate Missing.
- Cached/coalesced downloads respect cancellation and never overwrite another completed file.
- UI and persisted settings honor chosen scope, concurrency and authenticated provider permissions.

## Ledger
- Baseline: 210 tests and90 subtests passed with real Tk; clean repository on remote f588253a1b6596e239d1040c7f4ef379acef1dee, tree28fefd28e905b7626137f5b95ebb480139504f80.
- Ruling: continue directly in the existing work checkout and publish main; user explicitly authorized implementation choices and direct main updates.
- Source/cache, throughput and UI/settings tasks implemented with independent review. Round1 fixes: separate catalogue refusal latches from image-host access refusals; include existing coverage for cancelled preflight jobs. Both scoped re-reviews approved.
- Local verification: 288 tests and98 subtests passed with real Tk; source self-test, compile and diff checks passed. Current official export indexed successfully; live LaunchBox Tetris cover and production automatic fallback for Super Mario Land (Europe) succeeded. The latter preserved ROM bytes, deferred/applied metadata and skipped the repeated run. The packaged self-test now exercises the SAX parser, local catalogue and fallback lookup. Windows build verification follows publication.
