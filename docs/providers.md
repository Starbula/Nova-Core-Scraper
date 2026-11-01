# Artwork providers

NovaCore downloads front box artwork only. ROMs and archives are never uploaded. The providers do not edit ROMs, launch emulators, or write gamelists; the engine controls local image destinations and metadata updates.

## Libretro: default, no account

[Libretro's official documentation](https://docs.libretro.com/guides/roms-playlists-thumbnails/) identifies [thumbnails.libretro.com](https://thumbnails.libretro.com/) as the server used by RetroArch. Its public `Named_Boxarts` directories contain PNG box covers. The [official thumbnail repository](https://github.com/libretro-thumbnails/libretro-thumbnails) explains its naming conventions and catalog organization.

The provider fetches one public HTML directory listing per mapped system and keeps the parsed filename catalog for seven days. It builds exact-name and base-title indexes once per system, so a large library does not scan every remote cover name for each game. Common RetroBat folder names map to the corresponding public platform directories; an unknown system is reported as unmatched without guessing a different platform.

Matching first preserves the complete ROM basename and display title, including region and disc/revision tags. A fallback requires the same normalized base title. Explicit ROM regions must overlap the artwork's region, and explicit edition/disc qualifiers must agree. Without an explicit region, the configured preferred region can select one otherwise unambiguous cover. Duplicate or ambiguous candidates remain unmatched. No edit-distance or partial-title matching is performed; a sequel is never substituted for its predecessor. Unusual ROM names and arcade abbreviations may need a useful existing gamelist title. Coverage and naming are limited by the upstream catalog.

## ScreenScraper: optional, own credentials

The [official ScreenScraper API documentation](https://www.screenscraper.fr/webapi2.php) describes API v2 and its use conditions. An integrating application needs its own developer ID and developer password, obtained from the ScreenScraper team through their forum. The user also supplies their personal username and password. Do not reuse another application's developer credentials or share user accounts. The service permits integration in free distributed applications; other uses require its prior permission.

Construct with `ScreenScraperProvider(cache_dir, devid, devpassword, username, password, region='eu', interval=1.0, workers=1)`. Constructors perform no network requests. The engine calls `prepare(checkpoint)` before launching jobs; `find` also prepares lazily if needed. `ssuserInfos.php` negotiates concurrent threads and per-minute/daily/failed-match request allowances. Server quota responses update local counters; HTTP 430/431 stop the session. HTTP 403 stops the session with an account-configuration error. Never evade these limits with extra accounts or another address.

`systemesListe.php` supplies system IDs and frontend aliases. A local `System.screenscraper_id` is an explicit override for systems without a recognized alias. The provider uses `jeuRecherche.php` with a system and title, accepts one exact title match within that system, and selects `box-2D` media by region. Ambiguous results remain unmatched. It does not hash complete disc images or invoke the hash-based `jeuInfos.php` endpoint. This makes matching less expensive locally but cannot identify every renamed ROM.

## Transfer and file guarantees

Both providers bound worker concurrency to 1–4 and share request spacing among all workers. ScreenScraper reduces concurrency and request rate to account permissions. A positive `maxdownloadspeed` is documented in Ko/s; streaming shares a conservative budget of 1000 bytes per Ko across all workers, with cancellation-aware waits. Network calls have connection/read timeouts, bounded retries for HTTP 429 and server errors, and cancellation-aware waits that honor `Retry-After`. A cancellation checkpoint is also called while reading and before publishing.

Only HTTPS is accepted. Libretro transfers remain on `thumbnails.libretro.com`; ScreenScraper transfers remain in the `screenscraper.fr` domain family. Every redirect is checked before following it. Error messages omit remote response bodies, request URLs, and account values. Credential-bearing media URLs are used in memory and must not be logged by callers.

Downloads are limited to 32 MiB and decoded images to 40 million pixels. Pillow verifies and fully decodes media, then writes a normalized RGB/RGBA PNG. HTML error pages, truncated images, and ScreenScraper's text media-status responses are rejected. The provider fsyncs a same-directory temporary file and publishes it without replacing any existing destination: Windows uses no-clobber rename; POSIX uses an atomic hard link. An existing or concurrently created destination raises `FileExistsError` and stays untouched. A filesystem that cannot support safe publication produces an error instead of silently overwriting. Temporary files are removed on failure or cancellation.

RetroBat's [local media documentation](https://wiki.retrobat.org/navigation/scraping-and-metadata) distinguishes box thumbnails (`thumbnail`) from screenshot images (`image`). NovaCore's engine writes into its dedicated image directory and updates these links only when its process gate permits metadata edits.

## Verification scope

Tests use a deterministic replacement for the external HTTP transport with real requests response objects, actual encoded images, threads, and filesystem operations. They exercise conservative matching, catalog persistence/indexing, quotas, retries, cancellation, redirect trust, image limits, PNG conversion, and no-overwrite publication. On 2026-11-01, a Linux smoke check also matched and downloaded a real Libretro cover for NES Super Mario Bros. (World), verified a normalized 325,724-byte PNG, and cleaned the temporary output. ScreenScraper has fixture coverage only; live authentication and Windows runtime behavior remain environment-specific checks.
