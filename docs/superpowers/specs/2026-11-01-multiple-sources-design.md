# Multiple artwork sources and faster scraping

The user wants higher Game Boy coverage and faster simultaneous scraping, while keeping the existing console scope, pause/cancel, metadata protection and readable desktop UI.

Use automatic fallback from Libretro to LaunchBox, without an account. LaunchBox explicitly permits other applications to use its exported catalogue and image filenames. The currently downloaded official Metadata.zip (109,050,298 bytes, 31 October 2026) contains a 517,287,128-byte Metadata.xml; parse it incrementally into a small indexed local SQLite cache. Download only matched front-box images. Cache catalogue data for seven days; retain a usable old catalogue on refresh failure. ScreenScraper remains a separate configured option with its existing application-registration requirement.

Alternatives considered: asking users to switch providers manually would require repeating runs; adding several account/API-key services would repeat the setup friction they reported. OpenVGDB's Game Boy covers point to GameFAQs and do not offer sufficiently clear reuse permission for this implementation.

Match exact normalized titles and verified alternate names within the exact platform; prefer the ROM's region and then the configured region. Reject ambiguous games, sequels and mismatched hacks/editions. Missing means every enabled source returned no usable match; a source failure must remain visible instead of becoming a false missing result. No cover guesses, ROM downloads or artwork overwrites.

Increase the configurable worker limit to 16 and provide a fast preset of 8 workers / 0.25 seconds between requests. Apply per-provider floors (Libretro at least 0.25 seconds, LaunchBox at least 0.5 seconds) and honor Retry-After and ScreenScraper account limits. These floors are NovaCore's conservative traffic policy, not claimed provider-published quotas. Coalesce simultaneous identical artwork URLs and keep a bounded in-memory validated image cache. Existing output files still provide resumption across sessions.

Preserve explicit newer settings; migrate old default Libretro / 2-worker / 1-second settings to automatic sources / the fast defaults. Explain the first LaunchBox catalogue download and show progress during preparation. Keep lookups and parsing off the Tk thread.

The user also reproduced stale console coverage totals: reopening the program updates them, while successful downloads do not. Publish per-system covered-game totals from the engine and update the console rows, selected-scope summary and Start caption in the open window. Include existing recognized artwork and newly completed files, without counting a different console's downloads or requiring a fresh filesystem scan.

Primary references: https://forums.launchbox-app.com/topic/30050-tips-and-suggestions-for-games-database/ ; https://forums.launchbox-app.com/topic/54163-is-there-a-public-way-to-get-images-from-the-launchbox-games-database/ ; https://docs.libretro.com/guides/roms-playlists-thumbnails/ .
