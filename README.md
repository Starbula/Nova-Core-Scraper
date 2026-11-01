# NovaCore Artwork Scraper

A portable Windows 10/11 desktop tool for filling in missing RetroBat box artwork. Select your consoles, click **Start scraping**, and leave the run working independently of RetroBat. **Automatic** tries every configured source, with account-free sources available immediately. The desktop interface and Settings use a readable dark theme.

Box artwork is the only supported media in this version. Manuals, ratings and pad-to-key are unavailable and disabled.

## Run on Windows

Download the portable app artifact from a successful **Windows executable** run on this repository's **Actions** page. Extract the ZIP into a writable folder and run `NovaCore-Artwork-Scraper.exe`. Keep `LICENSE` with redistributed copies.

For source use, install Python 3.11 or newer with Tcl/Tk support, extract or clone this repository, and double-click **Launch NovaCore Scraper.cmd**. The launcher creates a local `.venv` and installs the runtime requirements; the initial setup needs internet access. Errors stay visible in the launch window.

1. Open **Library**, click **Browse**, and choose the folder containing `retrobat.exe` and `roms`, or use **Autodetect**. Scanning runs in the background.
2. Choose a console from the dropdown or click its name. This selects **only that console**. Use the large checkbox column for several consoles, or the global **Select All / Deselect All** buttons. Every new session starts with no consoles selected, so an old bulk selection cannot silently queue the whole library.
3. Check the selected console names, game count and missing-cover count above the list, then click **Start scraping**. The app switches to **Downloads** and freezes that run's console scope.

For an unattended run, keep **Automatic**, **Retry temporary failures**, **Keep PC awake**, and both cover-fallback options enabled in Settings. Optional provider accounts can be configured once before starting. Start waits for remembered credentials to load so a quick click cannot silently omit saved sources. You can play games in RetroBat while NovaCore downloads; metadata is applied after RetroBat closes.

Console coverage totals, the selected-game summary and Start button update as covers finish. You do not need to close the application or rescan to see its downloads reflected. Artwork changes made by other programs still require **Scan library**.

The optional console search matches partial words regardless of spaces: `GameBoy`, `Game Boy`, `Game Advance` and `GBA` can find Game Boy Advance. Choosing a console hidden by a search clears that search and scrolls its row into view.

| Tab | Purpose |
| --- | --- |
| Library | Choose consoles and see games, existing covers and missing covers. |
| Downloads | See each game's state, source, match notes and unresolved reason; filter, cancel, pause/resume, or export results to CSV. |
| Downloaded | Inspect completed artwork and its saved path, including downloads from earlier sessions. |
| Settings | Configure providers, credentials, matching, retries and power options in a scrollable form inside the same window. |

Download tables show 200 rows per page, with Previous/Next controls, so large queues do not create tens of thousands of UI rows. Cancelling work keeps already completed artwork. The progress bar stays visible in every tab: first it shows selected-game checks, then missing-artwork jobs processed. Downloaded, existing, missing, failed and cancelled counts stay separate; 100% processed does not mean every game had a matching cover.

The application reads the installation's system configuration where available and scans supported ROM files recursively. Scanning uses cached directory information, skips unrelated video/manual folders, and processes up to two consoles at a time. Autodetection stops as soon as it finds a valid installation. It honours existing artwork metadata and avoids linked directories. Ambiguous or unsupported library layouts may need manual artwork.

### Faster scraping

Settings provides a **Fast preset** of 8 workers and 0.25 seconds between request starts, with up to 16 configurable workers. Libretro caps active requests at 8, LaunchBox/GameTDB enforce at least 0.5-second spacing, IGDB enforces its 4-per-second/8-open-request limits, and MobyGames conservatively uses one request every 10 seconds. User settings, server responses and account quotas can slow requests further. More workers overlap network waits and image processing; they cannot guarantee a particular download rate.

Catalogue lookups happen locally after caching. Recent validated image bytes are reused within a run, with a bounded 64 MiB cache per source: simultaneous ROM entries using the same cover share a request while each receives its own safely published artwork file. Existing files continue to prevent repeat downloads across sessions.

Older default settings migrate to automatic sources and the fast defaults. Saved custom rates and worker counts are retained within the supported bounds; explicit choices saved by this version are preserved.

Temporary source/network failures receive up to **two additional passes**, with a cooperative 30-second wait before each pass. Pause freezes the countdown; Stop and per-game cancellation interrupt it. Source cooldowns avoid repeating expensive calls to an offline service for every game. Missing matches, ambiguous titles, invalid images, rejected credentials and exhausted quotas are not blindly retried. Progress counts unique games rather than adding retries to the total.

**Keep PC awake** prevents automatic Windows idle sleep during the run, including retry waits, while allowing the display to turn off. Normal power behavior is restored when the run ends. It does not prevent manual sleep, shutdown or a laptop lid action.

## Providers and matching

Seven provider adapters are supported. **Automatic** uses the three account-free adapters, then adds complete configured accounts. It tries safe regional/image alternatives within each source before giving up; a broken preferred image does not hide another listed cover for that same identified game. A failed source does not stop healthy fallback sources. Unsupported platforms are skipped, so GameTDB does not perform Wii catalogue work for a Game Boy run.

| Source | Setup | Coverage notes |
| --- | --- | --- |
| Libretro | None | Public Named_Boxarts collections. |
| LaunchBox | None | Public metadata catalogue and listed front covers. First catalogue download is about 110 MB. |
| GameTDB | None | Verified Wii/GameCube catalogue or local disc-ID cover lookup. Other GameTDB platforms are not implemented. |
| TheGamesDB | Your own API key | Platform-specific game records and front box images. |
| IGDB | Your own Twitch application Client ID and Client Secret | Game covers and available regional localizations; physical packaging for a particular port is unverified and labelled. |
| MobyGames | Your own subscription API key | Platform-specific front scans. **Data by MobyGames.com**. |
| ScreenScraper | Authorized NovaCore application integration plus your user account | Existing API adapter; NovaCore still does not ship an approved application pair. |

Availability in Settings distinguishes supported services from accounts ready for this run. There are no bundled or borrowed API credentials. Missing optional keys do not block the public sources.

**Libretro** uses the public [libretro-thumbnails collections](https://github.com/libretro-thumbnails/libretro-thumbnails) and `Named_Boxarts`; see the [official naming guide](https://docs.libretro.com/guides/roms-playlists-thumbnails/). Catalogue lookups are cached and indexed locally.

**LaunchBox** uses its [official public metadata export](https://gamesdb.launchbox-app.com/Metadata.zip) and front-box images listed in that catalogue. Its administrator [permits other applications to use the data](https://forums.launchbox-app.com/topic/30050-tips-and-suggestions-for-games-database/) and [documents using exported image filenames](https://forums.launchbox-app.com/topic/54163-is-there-a-public-way-to-get-images-from-the-launchbox-games-database/). No LaunchBox installation or account is needed. On first use, NovaCore downloads a catalogue of about 110 MB and builds a compact SQLite index in the background; preparation messages show download/index progress. The index is cached for seven days. A failed refresh retains a previously usable index, and an interrupted first preparation can be retried. Keep roughly 300 MB free in the app's data drive for catalogue preparation, in addition to your artwork storage.

Shared matching normalizes punctuation, spacing, case, Latin accents and trailing articles. GoodTools region abbreviations and verified-good `[!]` markers are recognized. This recovers names such as a dash versus a colon without using a partial-name or edit-distance guess. Digits, sequels, meaningful editions, hacks, demos, prototypes and unknown qualifiers remain part of identity; ambiguous game IDs are left for review.

For Game Boy, Game Boy Color, Game Boy Advance and Nintendo DS libraries, a leading four-digit archive index such as `0002 - Super Mario Advance (J)` is excluded from the search title. Actual numeric game titles and provider catalogue names retain their digits. ROM filenames are never changed.

Computer libraries also recognize validated TOSEC date/publisher names. Libretro can use a publisher-preserving title without the date or verified Spectrum memory labels. LaunchBox can omit those fields from a title only when its official publisher and known release year corroborate the same game. Unknown modifiers and hacked filenames remain constraints. A terminal `(C64)` label is redundant only in a selected C64 library. The updated LaunchBox index requires a one-time catalogue refresh.

By default, an identified base game may supply a cover for a plain revision/version/disc variant, and another region's cover can be used if no preferred cover succeeds. The actual region and fallback note are recorded in results and history. Disable **Use base-game covers for revisions/discs** or **Use another-region covers for the same game** for strict matching. These options do not authorize assigning an unrelated game's artwork. Some covers and identifications remain unavailable; no source count can guarantee 100% of an arbitrary ROM collection.

**GameTDB** uses its [public database and documented cover service](https://www.gametdb.com/Main/FAQ). Eligible local disc headers can identify an exact Wii/GameCube release without uploading ROM data. Public metadata access can be unavailable; verified header recovery remains independent of that catalogue. Compressed/container formats without a supported header reader require a catalogue title match. See [provider notes](docs/providers.md) for the exact supported formats and limitations.

### Optional accounts, configured once

Enter the relevant fields in Settings. [TheGamesDB key page](https://api.thegamesdb.net/key.php) requires your account login. [IGDB's setup instructions](https://api-docs.igdb.com/#account-creation) require a Twitch account with two-factor authentication, a registered Confidential application, its Client ID and generated Client Secret. [MobyGames API access](https://www.mobygames.com/api/subscribe/) requires your own subscription key. These services' setup requirements cannot be replaced by the scraper.

On Windows, **Remember credentials for this Windows account** protects the values with DPAPI in `data/credentials.dat`; they are not written into `settings.json`. Uncheck it and Save to remove remembered values. Saved credentials normally require the same Windows account and computer; moving the portable app elsewhere may require re-entry. On other operating systems values stay in the current session. Provider tokens are not persisted. Environment variables override remembered values:

```text
NOVACORE_TGDB_API_KEY
NOVACORE_IGDB_CLIENT_ID
NOVACORE_IGDB_CLIENT_SECRET
NOVACORE_MOBYGAMES_API_KEY
NOVACORE_SS_USERNAME
NOVACORE_SS_PASSWORD
```

**ScreenScraper** needs both a registered application integration and your personal account. Ordinary users enter only username and password in **Settings**. NovaCore does **not** ship an approved developer credential pair, so a personal login alone cannot currently enable this provider. Settings explains this upfront, and Start remains unavailable for an unconfigured ScreenScraper setup. Use the visible **Use automatic sources** button to scrape without an account.

For maintainers configuring an authorized NovaCore integration, the application pair is supplied through `NOVACORE_SS_DEVID` and `NOVACORE_SS_DEVPASSWORD`. That application pair is never stored with personal account values. Local configuration is not proof of provider approval: ScreenScraper verifies actual access on use. Automatic adds this adapter only when both application integration and personal account are configured.

See [ScreenScraper's API documentation](https://www.screenscraper.fr/webapi2.php) and the [provider notes](docs/providers.md). NovaCore needs its own application registration before it can offer the same account-only user setup as an integrated frontend.

### Morning results and unresolved games

The Downloads filters distinguish **Missing**, **Failed** and **Retry wait**. Details explain ambiguous names, unsupported platforms, unavailable covers, account/quota problems and temporary failures. **Export results** writes a durable CSV report with console, game, status, reason, source, actual region, match notes, attempts and saved artwork path. Export runs in the background, including during downloads. Choose a `.csv` report destination outside ROM, media, metadata and recovery folders; protected library files cannot be replaced by export.

### See which sources are actually working

The **Sources** tab shows the frozen order used by the current/last run, each source's last result, game checks, cover candidates, saved covers, outgoing HTTP/API attempts and its last reported allowance where available. These counts are independent: a title search that finds no eligible cover can spend API allowance while saving zero images. Cached lookups can check games with zero network requests. The last server allowance may lag requests still in flight; a conservative local budget being exhausted after network errors is labelled **Budget unconfirmed**, rather than claiming the server has confirmed zero allowance.

TheGamesDB can return HTTP 403 when its allowance is exhausted. NovaCore reads the bounded allowance response: a confirmed zero is **Quota exhausted**, HTTP 401 is **Authentication rejected**, and an inconclusive HTTP 403 is **Access refused**. A refusal alone is not proof that a saved key is wrong. The affected source is suspended for that run while healthy sources continue.

Click **Check sources** while idle to check the current configuration in the background. TheGamesDB and IGDB perform authenticated platform API requests even when a catalogue is cached, so rejected credentials cannot appear verified merely because cached preparation succeeded. This check consumes API requests and can use allowance. Other adapters report catalogue preparation separately from authenticated verification. A configured value is not proof of working access. ScreenScraper remains visibly blocked without an authorized NovaCore application registration, even if personal username/password fields are filled.

In **Downloads**, select a game and click **Show details** (or double-click) for its complete source trace. No-match, unsupported platform, rejected authentication, quota suspension and successful downloads remain distinct. The trace is retained in recovery/history and included as `source_trace` in CSV export. A partial source outage keeps an unresolved game **Failed**, since the unavailable source could not be searched, while healthy fallback sources continue.

Failure counts are game results, not outgoing request counts. One exhausted account can leave many otherwise unresolved games marked Failed without sending another API call for each one. Inspect **Sources** for actual request counts. After updating the app or restoring source access, select the consoles and start again; existing covers are retained and skipped.

Some C64 collections use opaque disk labels such as `sb141167` or `0001a`. Those labels alone do not establish a game title, and a disk can contain several programs. NovaCore leaves these unresolved rather than assigning an invented cover. Supply trustworthy game names in your existing library metadata to improve identification; the scraper never renames ROMs.

Each game's sources are tried in order until a cover saves; several games run concurrently. Automatic tries configured IGDB before TheGamesDB to preserve TheGamesDB's limited monthly allowance. After changing credentials, start a new run to use them. No credential values or request URLs are included in diagnostics.

Existing artwork remains untouched. Starting the same consoles again checks completed files and works on remaining gaps. Missing means no acceptable cover was found under the selected policy; it is not proof that no cover exists anywhere. There is no manual game-identity picker in this version, and ambiguous records are reported rather than guessed.

## RetroBat can stay open

Artwork downloads can continue while RetroBat or EmulationStation is running. All `gamelist.xml` changes are queued while either process is detected. NovaCore automatically attempts to apply those pending changes after they close, while NovaCore remains open. Pending entries also survive a restart.

The merger reads the current XML again before applying changes, preserves unrelated metadata and existing artwork, and creates a backup before replacing a gamelist. If the file is malformed, changes unexpectedly or cannot be written, the artwork remains pending for a later attempt. Process detection and revision checks reduce conflicts; another program can still change files outside NovaCore's control. Close other metadata editors before applying pending changes.

## Files and portability

- ROM files are read only. NovaCore does not download, modify or delete ROMs.
- New, validated boxart files go into a dedicated `images/novacore` directory for their system. Existing artwork is never overwritten. Box covers populate `thumbnail`; an existing screenshot in `image` is preserved. If `image` is absent or broken, the box is also linked there for theme compatibility.
- Progress, queued metadata and installation locking live under the RetroBat folder's `.novacore-artwork` directory. Game identities use relative paths so moving the same installation to another drive can preserve progress.
- Application settings and provider cache live in the `data` folder beside the executable, or beside `main.py` when running from source. Use a writable app folder. The data stays on that computer or drive; it is not synced to an account.
- Original gamelists are backed up once as `gamelist.xml.novacore.bak`. Downloaded covers and backups use additional disk space. Failures are logged in `data/logs/scraper.log`.
- Conventional artwork added by another tool after a scan is detected on the next **Scan library**; live XML references and NovaCore download destinations are rechecked during scraping.

## Build and test

On Windows, double-click **Build NovaCore Scraper.cmd**. It creates or reuses `.venv`, installs the build requirements and produces `dist/NovaCore-Artwork-Scraper.exe`. PyInstaller builds for the operating system running it, so a Windows executable must be built on Windows. For a diagnostic console build, run `python build.py --console` from an environment with the build requirements installed.

From a terminal with Python available:

```text
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-build.txt -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python main.py --self-test self-test-result.json
.venv\Scripts\python main.py --smoke-test-ui
.venv\Scripts\python build.py
```

The headless self-test writes a JSON result containing a `success` boolean and returns a nonzero exit code on failure. The UI smoke test briefly opens and closes the desktop window and needs a graphical desktop. Neither option scans your real ROM library.

Tests use temporary libraries, mocked process checks and local/fixture HTTP responses. They cover source schemas, authentication isolation, quotas, matching/alternative images, source cooldowns, retry/pause/cancel, protected CSV export, credential persistence races, live counts, ROM/metadata preservation and large queues. Windows checks exercise native DPAPI, the desktop UI and the packaged provider/parser/report paths. Public live checks are documented in the provider notes; credentialed APIs have fixture coverage only until authorized real accounts are supplied. Check the workflow's actual result before relying on a build; its definition alone is not proof of a successful Windows run.

## License

MIT, copyright (c) 2026 Lu Lu. See [LICENSE](LICENSE). System configuration discovery adapts MIT-licensed ideas from NovaCore Downloader; this project does not modify that application.
