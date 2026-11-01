# Artwork providers

NovaCore downloads front box artwork only. ROMs and archives are never uploaded. The providers do not edit ROMs, launch emulators, or write gamelists; the engine controls local image destinations and metadata updates.

## Libretro: default, no account

[Libretro's official documentation](https://docs.libretro.com/guides/roms-playlists-thumbnails/) identifies [thumbnails.libretro.com](https://thumbnails.libretro.com/) as the server used by RetroArch. Its public `Named_Boxarts` directories contain PNG box covers. The [official thumbnail repository](https://github.com/libretro-thumbnails/libretro-thumbnails) explains its naming conventions and catalog organization.

The provider fetches one public HTML directory listing per mapped system and keeps the parsed filename catalog for seven days. It builds exact-name and base-title indexes once per system, so a large library does not scan every remote cover name for each game. Common RetroBat folder names map to the corresponding public platform directories; an unknown system is reported as unmatched without guessing a different platform.

Matching first preserves the complete ROM basename and display title, including region and disc/revision tags. A fallback requires the same normalized base title. Explicit ROM regions must overlap the artwork's region, and explicit edition/disc qualifiers must agree. Without an explicit region, the configured preferred region can select one otherwise unambiguous cover. Duplicate or ambiguous candidates remain unmatched. No edit-distance or partial-title matching is performed; a sequel is never substituted for its predecessor. Unusual ROM names and arcade abbreviations may need a useful existing gamelist title. Coverage and naming are limited by the upstream catalog.

## ScreenScraper: account settings and application integration

Normal ScreenScraper settings show only your ScreenScraper username and password. Libretro remains the working default and needs no account. Before Start, NovaCore checks whether its application integration is configured. If it is missing, the settings screen explains that ScreenScraper is unavailable and offers Libretro directly. A personal account alone cannot enable an unregistered application.

ScreenScraper has two distinct credentials: the application's developer pair and the user's account. Its [official API documentation](https://www.screenscraper.fr/webapi2.php) directs integrating developers to present their software through its forum and obtain an application ID/password. [RetroBat's user documentation](https://wiki.retrobat.org/navigation/scraping-and-metadata) asks users for their personal ScreenScraper account because the user-facing setup is separate from application integration. NovaCore must provide its own legitimate integration; it cannot use RetroBat's registration. No NovaCore registration or live access verification is implied by this code change.

For maintainers and authorized advanced deployments, `NOVACORE_SS_DEVID` and `NOVACORE_SS_DEVPASSWORD` configure NovaCore's application pair in the process environment. Both must be present and nonblank. `auth.screen_scraper_app_credentials()` reads that pair locally; `auth.screen_scraper_status()` provides a secret-free status message. These helpers make no file/network requests and do not validate registration. A configured pair only enables the normal account flow; the service verifies actual access when scraping starts. Never borrow another application's credentials, share user accounts, or evade service quotas.

Internally, construct `ScreenScraperProvider(cache_dir, devid, devpassword, username, password, region='eu', interval=1.0, workers=1)` from the application pair and the user's account fields. Constructors perform no network requests. The engine calls `prepare(checkpoint)` before launching jobs; `find` also prepares lazily if needed. Preparation negotiates thread and request allowances. Server quota responses update local counters; exhausted quotas and rejected credentials stop the session.

`systemesListe.php` supplies system IDs and frontend aliases. A local `System.screenscraper_id` is an explicit override for systems without a recognized alias. The provider uses `jeuRecherche.php` with a system and title, accepts one exact title match within that system, and selects `box-2D` media by region. Ambiguous results remain unmatched. It does not hash complete disc images or invoke the hash-based `jeuInfos.php` endpoint. This makes matching less expensive locally but cannot identify every renamed ROM.

## Transfer and file guarantees

Both providers bound worker concurrency to 1–4 and share request spacing among all workers. ScreenScraper reduces concurrency and request rate to account permissions. A positive `maxdownloadspeed` is documented in Ko/s; streaming shares a conservative budget of 1000 bytes per Ko across all workers, with cancellation-aware waits. Network calls have connection/read timeouts, bounded retries for HTTP 429 and server errors, and cancellation-aware waits that honor `Retry-After`. A cancellation checkpoint is also called while reading and before publishing.

Only HTTPS is accepted. Libretro transfers remain on `thumbnails.libretro.com`; ScreenScraper transfers remain in the `screenscraper.fr` domain family. Every redirect is checked before following it. Error messages omit remote response bodies, request URLs, and account values. Credential-bearing media URLs are used in memory and must not be logged by callers.

Downloads are limited to 32 MiB and decoded images to 40 million pixels. Pillow verifies and fully decodes media, then writes a normalized RGB/RGBA PNG. HTML error pages, truncated images, and ScreenScraper's text media-status responses are rejected. The provider fsyncs a same-directory temporary file and publishes it without replacing any existing destination: Windows uses no-clobber rename; POSIX uses an atomic hard link. An existing or concurrently created destination raises `FileExistsError` and stays untouched. A filesystem that cannot support safe publication produces an error instead of silently overwriting. Temporary files are removed on failure or cancellation.

RetroBat's [local media documentation](https://wiki.retrobat.org/navigation/scraping-and-metadata) distinguishes box thumbnails (`thumbnail`) from screenshot images (`image`). NovaCore's engine writes into its dedicated image directory and updates these links only when its process gate permits metadata edits.

## Verification scope

Tests use a deterministic replacement for the external HTTP transport with real requests response objects, actual encoded images, threads, and filesystem operations. They exercise conservative matching, catalog persistence/indexing, quotas, retries, cancellation, redirect trust, image limits, PNG conversion, and no-overwrite publication. On 2026-11-01, a Linux smoke check also matched and downloaded a real Libretro cover for NES Super Mario Bros. (World), verified a normalized 325,724-byte PNG, and cleaned the temporary output. ScreenScraper has fixture coverage only; live authentication and Windows runtime behavior remain environment-specific checks.
