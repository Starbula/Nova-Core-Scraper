# Artwork sources and unattended behavior

NovaCore downloads front covers only. It never uploads a ROM, downloads a ROM, launches an emulator or alters a ROM. Matching, sources and local publishing are separate from RetroBat's internal scraper. All changes to `gamelist.xml` still pass the existing process gate, atomic merger and backup rules.

## Sources actually supported

| Adapter | Ready without an account | Scope and setup |
| --- | --- | --- |
| Libretro | Yes | Public `Named_Boxarts` collections. |
| LaunchBox | Yes | Public catalogue plus listed front images; no LaunchBox installation required. |
| GameTDB | Yes | Verified Wii/GameCube IDs and catalogue titles. Other GameTDB platforms are not implemented. |
| TheGamesDB | No | User's personal API key. |
| IGDB | No | User's Twitch application Client ID and Client Secret; OAuth token is memory-only. |
| MobyGames | No | User's own subscription API key. **Data by MobyGames.com**. |
| ScreenScraper | No | Authorized NovaCore application pair plus personal account. No approved NovaCore pair is shipped. |

Automatic uses Libretro, LaunchBox and GameTDB, then adds complete configured account sources. Blank optional credentials are not constructed into failing adapters. The UI distinguishes supported sources from currently configured accounts and waits for remembered credentials before creating a run. A saved credential result cannot overwrite typing or environment-provided values.

## Shared matching and image alternatives

`matching.py` provides `MatchPolicy`, canonical title keys, title/region/packaging information and ranked matches. Names normalize Unicode compatibility, Latin accents, case, punctuation, spacing and explicit trailing articles. Japanese voicing and other non-Latin combining marks are preserved. Digits and sequel numbers are preserved; no edit-distance or substring guess picks a game.

Meaningful parenthetical text, editions, hacks, prototypes, demos and unknown qualifiers remain part of identity. A plain alternate name cannot hide a qualified canonical game or another conflicting variant sharing an ID. Full title equivalence can match a parenthetical subtitle against equivalent plain punctuation. Where the source supplies IDs, ambiguity is resolved before cover availability or regional preferences; multiple conflicting matching IDs remain unresolved.

Default `MatchPolicy(allow_other_regions=True, allow_base_cover=True)` allows an identified base game's box to represent a plain revision/version/disc packaging variant, and permits another region's cover after preferred choices. The actual source, region, matched title and fallback note are retained. Both options have strict opt-outs in Settings. These options do not authorize substituting a sequel or unrelated hack.

Every adapter exposes ranked, lazy `candidates()` while `find()` remains first-result compatible. The engine can try another front image from the same verified game after a removed, corrupt or unavailable preferred asset, then continue to another source. HTTP404 artwork absence is distinguished from a temporary network failure.

## Libretro

Primary sources: [official thumbnail guide](https://docs.libretro.com/guides/roms-playlists-thumbnails/) and [official collections](https://github.com/libretro-thumbnails/libretro-thumbnails).

One public HTML catalogue per mapped console is cached for seven days and indexed locally by normalized title. Game Boy, Game Boy Color and Game Boy Advance remain separate mappings. GoodTools region abbreviations and verified-good `[!]` markers are recognized. Unknown systems return an explicit unsupported-platform diagnostic without guessing another console. Transfers remain on `thumbnails.libretro.com`.

## LaunchBox

Primary sources: [administrator's permission for application use](https://forums.launchbox-app.com/topic/30050-tips-and-suggestions-for-games-database/) and [exported image filename guidance](https://forums.launchbox-app.com/topic/54163-is-there-a-public-way-to-get-images-from-the-launchbox-games-database/).

The adapter downloads `https://gamesdb.launchbox-app.com/Metadata.zip`, reads the known `Metadata.xml` member, and requests only actual `GameImage` records with `Type = Box - Front` from `images.launchbox-app.com`. It does not scrape game web pages or distribute covers in this repository.

The checked 31 October 2026 export was 109,050,298 bytes compressed, with a 517,287,128-byte Metadata.xml. SAX parsing retains only game identities, verified alternate names and front images in a local SQLite index. The updated matching index built in 20.51 seconds, produced a 49,950,720-byte database and used about 53 MiB peak process memory in the Linux check. These are measurements, not Windows guarantees. The actual export contained 190,154 games and 206,648 front-cover records.

Preparation is background work with progress messages. Limits are 256 MiB compressed, 1 GiB XML, 4,096 characters per retained field and bounded archive member counts. DTDs/entities, unsupported formats and unsafe filenames are rejected. Index creation closes, commits and syncs before atomic replacement; the older usable index survives failure/cancellation. Read connections close explicitly on Windows. Freshness is seven days; a schema change rebuilds the index. A refused catalogue is not requested repeatedly in that session, while an already available index can still identify covers on the separate public image host. Refused image access remains fatal to that source.

## GameTDB: Wii and GameCube

Primary source: [GameTDB FAQ](https://www.gametdb.com/Main/FAQ), which permits software use and downloading databases/covers. GameTDB attribution and links are included in the application documentation. Cover address behavior is verified against [Dolphin's GameFile implementation](https://github.com/dolphin-emu/dolphin/blob/master/Source/Core/UICommon/GameFile.cpp); disc identity follows Dolphin's volume reader and WBFS reader plus [the original libwbfs format](https://github.com/kwiirk/wbfs).

The official WiiTDB XML provides platform, verified ID, localized titles and region. A seven-day bounded atomic index supports exact title matching. Preparation is lazy: selecting Game Boy does not request a Wii catalogue. The metadata host currently refuses requests from this test environment, so catalogue refresh is not claimed as live-tested here. The separate documented public artwork host does work.

Local `.iso` and `.gcm` identity reads only 32 header bytes, validating the GameCube/Wii magic and six-character game ID against the selected platform. Single-disc, single-file `.wbfs` recovery reads a bounded metadata sector plus the copied 32-byte disc header, using the documented WBFS geometry. It validates shifts, declared file length, disc-table flags/slot count, metadata offsets, Wii magic and ID. It does not traverse game blocks, decompress content, search split `.wbf1` parts, upload or modify anything. Invalid, split/multi-disc or unsupported containers require a catalogue title match instead. Known nonunique Datel IDs are rejected.

Candidates use only documented `https://art.gametdb.com/wii/cover/{region}/{verifiedID}.png` addresses. Every candidate retains that same proven ID; countries are ranked under the selected policy. A cover404 advances to the next permitted cover. Public live checks verified `RMGE01` artwork and production engine publication from a temporary Wii header fixture.

## TheGamesDB

Primary sources: [API specification](https://api.thegamesdb.net/), [official specification repository](https://github.com/TheGamesDB/TheGamesDBv2/blob/master/API/spec.yaml), and [personal API key page](https://api.thegamesdb.net/key.php).

The adapter uses documented platform lists, platform-filtered `v1.1/Games/ByGameName`, verified alternate names and included front box images. Platform/game IDs are verified before artwork selection. Pagination must be complete within its safety budget before claiming a unique game. A listed image is not evidence that a different matching ID should be ignored.

The API's documented box-image schema does not prove a region, so generic images are not assigned a fabricated country. Account allowance responses constrain API calls; credential/quota failures are isolated from other sources. Public image downloads do not receive the API key. Cached metadata and cache filenames exclude credentials and reject echoed secrets.

## IGDB

Primary sources: [IGDB API documentation](https://api-docs.igdb.com/) and [Twitch client-credentials flow](https://dev.twitch.tv/docs/authentication/getting-tokens-oauth/#client-credentials-flow-example).

Setup requires a Twitch account with two-factor authentication, a registered Confidential application, Client ID and Client Secret. Tokens are obtained through a form-body POST and refreshed once after an expired-token response; tokens are never persisted. Read queries use authenticated POST requests. The adapter enforces the documented four requests per second and eight open requests.

Requested platform membership and unique game identity are verified. Regional `game_localizations` covers can be used, but a language-only locale such as `fr`, `de` or `en` is not proof of a country. Country-bearing locales and geographic categories are handled separately. IGDB cover objects have no physical platform-packaging dimension: each returned candidate is labelled **Game-level cover; platform-specific packaging is unverified**. This limitation remains visible in results rather than being represented as a verified scan for a particular port.

## MobyGames

Primary sources: [official API documentation](https://www.mobygames.com/info/api/) and [subscription permissions](https://www.mobygames.com/api/subscribe/).

Requires the user's own authorized subscription key. This integration does not purchase or register a subscription. The documented API covers platform lists, bounded game search and platform-specific cover groups. Only `Front Cover` scans from a uniquely verified game/platform are yielded. Cover-group countries are mapped to actual regions and labelled under the shared policy. Pagination exhaustion raises an error before claiming uniqueness.

The conservative default is one request every ten seconds, one active request, compatible with the legacy 360-per-hour allowance; server allowances may restrict it further. Metadata is cached for seven days. Keys appear only in authenticated API requests, not image requests, cached filenames or stored payloads. Raw and URL-encoded credential echoes are rejected before memory/disk caching. No copyrighted data is sold or bundled with the executable. Required credit: **Data by MobyGames.com**.

## ScreenScraper

Primary sources: [official API](https://www.screenscraper.fr/webapi2.php) and [RetroBat user setup](https://wiki.retrobat.org/navigation/scraping-and-metadata).

Personal username/password alone cannot enable an unregistered application. NovaCore does not ship an approved application credential pair. Authorized maintainers supply `NOVACORE_SS_DEVID` and `NOVACORE_SS_DEVPASSWORD`; normal Settings exposes personal account fields only. No registration or live authenticated access is implied by the code. Never borrow another frontend's application pair.

The adapter negotiates thread/request/download allowances through `ssuserInfos.php`, discovers system IDs, queries names within the verified system and ranks listed `box-2D` images. Server quota counters remain enforced. Multiple verified game IDs or conflicting qualifier identities are not silently merged. It does not hash full disc images or call the hash-based game endpoint, so renamed/ambiguous ROMs can remain unresolved.

## Credentials, transport and output protection

Normal settings contain no API secrets. Optional remembered values use current-user Windows DPAPI in `data/credentials.dat`; moving to another account/computer can require re-entry. UI saves/loads happen in a background lane; unread saved keys cannot be erased by an early save and late loads cannot replace typed or environment values. There is no plaintext fallback. Windows DPAPI is exercised by native Windows tests and the packaged self-test. Other operating systems retain values in the session only.

Only trusted HTTPS endpoints are accepted and each redirect is checked. POST redirects and authenticated cross-host GET redirects are rejected. Request preparation/environment merging precedes rate/quota admission and dispatch, preserving requests' cookies, proxy, TLS, timeout and streaming behavior without bunching delayed preparations. Remote bodies, credential URLs and arbitrary exception text do not appear in reports/logs.

The coordinator supports up to sixteen workers. Shared source spacing/slots, bounded HTTP429/server retries and Retry-After remain enforced. ScreenScraper's account-supplied positive Ko/s limit shares a conservative 1,000-byte-per-Ko budget. Libretro caps at eight transfers; LaunchBox/GameTDB apply at least half-second spacing. Recently verified image bytes share a bounded 64 MiB cache per source and concurrent identical URLs coalesce. These limits are throughput controls, not a promise of a completion time.

Each image is limited to 32 MiB compressed/40 million decoded pixels, fully verified and normalized with Pillow, then fsynced and published without replacing an existing destination. A competing valid image becomes skipped coverage with pending metadata, rather than a failed job. Temporary files are cleaned up. ROMs and prior artwork remain untouched.

## Overnight results

Offline-source circuits cool down instead of repeating full failed requests for every game. Authentication/quota failures stay disabled for that chain; healthy sources continue. A temporary failure from an earlier image remains retryable even when a later source refuses credentials. At most two extra passes follow thirty-second cooperative waits. Pause freezes waits, and Stop/per-game cancel interrupts them. No-art404, missing/ambiguous identities, invalid images and local permission problems are not blindly retried.

Progress counts unique jobs across retries. Durable history records source, actual region, match note and attempts. Downloads displays detailed outcomes; Active includes retry-wait entries and supports their cancellation. CSV export streams from a separate read-only connection, requires `.csv`, rejects protected ROM/system/media/metadata/recovery folders and linked destinations, then atomically publishes the report. Existing gamelist pending updates survive another failed or interrupted run.

Windows keep-awake requests are made and reset on the coordinator thread through `finally`, using `ES_CONTINUOUS | ES_SYSTEM_REQUIRED` without forcing the display on. This prevents automatic idle sleep, not manual sleep or shutdown. Artwork downloading remains independent of RetroBat's process; only gamelist writes are deferred.

## Verification limits

Source diagnostics retain each game's search trace and show per-source game, match, saved-cover and actual outgoing-request counters. Explicit TheGamesDB/IGDB checks bypass cached platform data to verify authenticated API access; ordinary cached preparation alone does not verify an account. The last server allowance is distinct from the conservative local request budget. Local budget denial cannot discard an already paid successful response or block its public cover download; genuine authentication failures still terminate rejected access. Network errors do not justify silently replenishing an uncertain request budget.

Verified RetroBat system aliases now route MSU console variants, WonderSwan folder names, Amiga 4000, BBC Micro and the supported arcade subdivisions to equivalent platform catalogues. This fixes platform routing only; it does not fabricate title matches for short arcade ROM IDs or guarantee that a catalogue supplies a front cover.

Automated tests use temporary libraries, deterministic encoded images/HTTP fixtures, threads and actual SQLite/filesystem operations. They cover source schemas, auth isolation, quotas, cache guards, ID/qualifier ambiguity, alternate-image recovery, retries/cancellation, reports, DPAPI races, native path aliases and metadata integrity.

Public live checks on 2026-11-01 parsed the official LaunchBox export and downloaded regional covers. A current-index check matched `Super Mario Land 2 - 6 Golden Coins (USA) (Rev 1)` to its verified base game's US cover with a revision note, while `Tetris 999` and an unrelated hack remained unmatched. `Pokemon - Red Version` was honestly ambiguous across multiple catalogue IDs, rather than assigned an arbitrary cover. A GameTDB Wii disc-header engine check downloaded and validated a cover while retaining fixture ROM bytes and deferring metadata.

TheGamesDB, IGDB, MobyGames and ScreenScraper have fixture coverage only; no live authenticated account/download test is claimed. Public access or artwork availability in this environment does not prove coverage of the user's entire library. Windows build/parser/GUI and native DPAPI results must be checked in the actual Actions run.
