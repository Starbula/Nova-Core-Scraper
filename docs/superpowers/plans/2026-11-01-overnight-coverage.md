# Overnight artwork coverage implementation plan

> Agentic workers use test-first implementation with disjoint ownership; root integrates, reviews, publishes main and verifies Windows CI.

**Goal:** maximize safe cover recovery in an unattended selected-console run.
**Architecture:** shared canonical matching and explicit policy feed seven documented provider adapters; the source chain handles alternatives/outages and the engine handles bounded retry/reporting/power. Optional credentialed sources are configured once and do not block account-free sources.
**Tech stack:** existing Python/Tk/SQLite/requests/Pillow, Windows DPAPI and power APIs via ctypes; no new dependency.
**Spec:** docs/superpowers/specs/2026-11-01-overnight-coverage-design.md

## Ownership and interfaces
- Matching worker: matching.py, matching sections of providers.py/launchbox.py and their tests. MatchPolicy; TitleInfo; TitleMatch; title_key/title_info/requested_info/match_title/region_rank/match_note. Root only edits _Provider in providers.py.
- API worker: api_sources.py/tests, TheGamesDBProvider(api_key) and IGDBProvider(client_id,client_secret), standard cache/region/interval/workers/match_policy keyword options; primary-source auth/platform/front-cover schemas.
- GameTDB worker: gametdb.py/tests, verified Wii/GameCube catalogue and documented covers from catalogue or 32-byte ISO/GCM IDs; reject invalid/nonunique IDs. No access bypass.
- MobyGames worker: mobygames.py/tests only if documented subscription API is feasible; conservative quota and visible Data by MobyGames.com credit.
- Overnight worker: engine.py,state.py,sources.py,power.py and tests. Append JobSnapshot reason/source/region/match_note/attempts/retryable defaults; migrate durable jobs/download history safely. State.export_report(root,destination)->int rows, streaming read-only SQLite to atomic CSV. ScrapeEngine(...,retry_passes=2,retry_delay=30,keep_awake=True). Preserve status counts, coverage and cancellation during retry; nested candidates, source circuits and sanitized failure classification.
- Root: models.py adds optional Artwork match_note/matched_title/provider_game_id; _Provider request adds method/data/headers/accepted_status and optional missing diagnostics; new credentials.py Windows DPAPI helper. settings.py/ui.py integrate configured sources/policy/unattended toggles, source readiness, clear reasons/export and one Start path. Extend packaged main.py self-test to cover new standard-library/Windows paths.

## Shared contracts
- Artwork(url,source,region='',match_note='',matched_title='',provider_game_id='').
- _Provider._request(url,checkpoint,params=None,missing_ok=False,*,method='GET',data=None,headers=None,accepted_status=()). Prepare/env merge before rate/quota admission and dispatch; secrets never logged. accepted statuses return a response for provider-specific handling. ArtworkUnavailable(ProviderError) identifies permanent HTTP404 cover absence.
- _Provider._unmatched(code,message)->None stores per-thread reason; missing_reason(game=None)->(code,message).
- All providers have name, optional match_policy, prepare/find/download/close; status callbacks where preparation is lengthy. Automatic includes account-free adapters and only configured credentialed adapters. No missing optional credentials exception should disable the whole automatic chain.
- Credential environment names: NOVACORE_TGDB_API_KEY, NOVACORE_IGDB_CLIENT_ID, NOVACORE_IGDB_CLIENT_SECRET, NOVACORE_MOBYGAMES_API_KEY, existing NOVACORE_SS_USERNAME/PASSWORD and application-only NOVACORE_SS_DEVID/DEVPASSWORD. Never store the application pair in user settings.

## Tasks
- [ ] Verify provider docs and response schemas; fixtures before source code; implement adapters with quotas, caching, trust and explicit missing reasons.
- [ ] Implement shared matching with red/green punctuation, accents, article, revision/disc, cross-region, ambiguity, sequel and hack tests; indexed lookups remain efficient.
- [ ] Implement engine/report/power with deterministic retry/cancel/circuit tests, unique counts and migration preservation.
- [ ] Integrate credentials/settings/UI, source construction and row/export details with actual-Tk workflow tests.
- [ ] Independent source/matching/security and engine/UI reviews, full local tests and allowed live probes, README/provider docs, meaningful commit(s), push main and successful Windows build/parser/GUI checks.

## Review focus
- Optional missing API credentials versus enabled-but-rejected credentials must be distinguishable.
- Broader names/regions/revisions must never select a sequel, different platform or ambiguous ID.
- Offline sources must not impose retries on every game or turn true failures into Missing.
- Retries/cancellations must not double count, overwrite artwork or lose metadata pending state.
- Secrets remain out of reports/settings/logs and DPAPI failure must leave the app usable.

## Ledger
- Baseline remote a382040d14f6a0c4895a5f2a4cc8ff389d0a48ed; tree8ab89d28468a41e3b22047b4a09df8fbcf662204. Last full local suite295passed; Windows290passed/5skipped and executable checks successful.
- User requests maximum reliable unattended coverage; no design approval pause needed under explicit implementation autonomy. Authentication registration/purchases remain outside this work and will not be fabricated.
- Implemented all source/matching, engine/report/power and settings/credential/UI tasks. Independent reviews found and verified fixes for canonical alias guards, alternate-image recovery, IGDB locale geography, echoed credentials, protected export, mixed-failure retry loss, publication collisions and startup credential readiness. A final reviewed GameTDB extension supports bounded single-file/single-disc Wii WBFS metadata recovery.
- Final local verification:535passed,1native-Windows-DPAPI skip,121subtests with real Tk; source self-test, compile and diff checks clean. Current LaunchBox catalogue schema2 indexed in20.51s, recovered a revision-named verified GB cover, and rejected unrelated sequel/hack inputs. Public GameTDB disc-ID engine download succeeded without ROM changes or metadata catalogue access. No live authenticated API download is claimed. Commit/push and native Windows build verification follow.
