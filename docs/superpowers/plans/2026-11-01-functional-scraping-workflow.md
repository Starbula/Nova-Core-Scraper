# Functional scraping workflow

User explicitly requests a functional workflow redesign and direct execution. Keep the existing dark styling, scan safety and portable deployment.

1. Replace the screen with Library, Downloads, Downloaded and Settings tabs. Console dropdown chooses exactly one console; row text click also chooses exactly that console. Only explicit checkbox/multi-select actions add multiple consoles. Default selection is empty; Select All/Deselect All are global and clearly named. Search normalizes spacing and matches all partial words, so GameBoy and Game Advance find Game Boy Advance.
2. Show selected console names, selected games, existing covers and missing covers before Start. Submit only games from the checked selection and freeze that scope during an active run. Show a persistent progress bar for required/resolved missing artwork, plus a preparation phase for checking selected games.
3. Engine exposes paginated per-game job snapshots with checking/queued/finding/downloading/completed/skipped/missing/failed/cancelled states, individual cancellation and bulk cancellation. Store completed paths/titles in SQLite; expose paginated persistent history. All UI I/O uses background tasks and bounded tables (200 rows/page).
4. Settings is an embedded tab. Normal ScreenScraper setup shows user account fields. Application developer credentials come only from application configuration/environment, and readiness is checked before Start. If application credentials are unavailable, explain the missing NovaCore integration upfront and offer Libretro directly; do not borrow another app's credentials or pretend account-only access works.
5. Test real Tk row/dropdown selection and GBA-only submission, flexible search, queue changes/cancellation/history, progress arithmetic, same-window settings and unavailable-auth preflight. Run full fixture suite, frozen self-test and source/frozen desktop checks in Windows CI, then push reviewed changes to main.

Shared engine contract:
- JobSnapshot(key,title,console,status,artwork='',detail='') frozen dataclass. Status values lower-case.
- ScrapeEngine.jobs_page(query='', statuses=None, offset=0, limit=200) -> (list[JobSnapshot], int).
- ScrapeEngine.downloaded_page(query='', offset=0, limit=200) -> (list[JobSnapshot], int), background-call only.
- ScrapeEngine.cancel_jobs(keys) -> None; cancel_all() -> None. Completed artwork is preserved.
- ScrapeEngine.jobs_revision property -> int for throttled UI refresh.
- Progress retains old fields and adds checked, needed, resolved, cancelled. processed includes cancelled. needed/resolved refer only to missing-artwork work after preflight, while checked/total track selected games checked.
- Existing start/pause/resume/stop/wait/apply_pending and active/snapshot contracts remain.
- auth.screen_scraper_app_credentials() -> (str,str) or None; auth.screen_scraper_status() -> explanatory string. Reads NOVACORE_SS_DEVID/NOVACORE_SS_DEVPASSWORD without network or writes.
