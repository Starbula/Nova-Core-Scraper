# NovaCore Artwork Scraper

A portable Windows 10/11 desktop tool for filling in missing RetroBat box artwork. Choose a RetroBat installation and the systems you want to scan, then start scraping. The default Libretro provider needs no account. The desktop interface and Settings use a readable dark theme.

Box artwork is the only supported media in this version. Manuals, ratings and pad-to-key are unavailable and disabled.

## Run on Windows

Download the portable app artifact from a successful **Windows executable** run on this repository's **Actions** page. Extract the ZIP into a writable folder and run `NovaCore-Artwork-Scraper.exe`. Keep `LICENSE` with redistributed copies.

For source use, install Python 3.11 or newer with Tcl/Tk support, extract or clone this repository, and double-click **Launch NovaCore Scraper.cmd**. The launcher creates a local `.venv` and installs the runtime requirements; the initial setup needs internet access. Errors stay visible in the launch window.

1. Open **Library**, click **Browse**, and choose the folder containing `retrobat.exe` and `roms`, or use **Autodetect**. Scanning runs in the background.
2. Choose a console from the dropdown or click its name. This selects **only that console**. Use the large checkbox column for several consoles, or the global **Select All / Deselect All** buttons. Every new session starts with no consoles selected, so an old bulk selection cannot silently queue the whole library.
3. Check the selected console names, game count and missing-cover count above the list, then click **Start scraping**. The app switches to **Downloads** and freezes that run's console scope.

The optional console search matches partial words regardless of spaces: `GameBoy`, `Game Boy`, `Game Advance` and `GBA` can find Game Boy Advance. Choosing a console hidden by a search clears that search and scrolls its row into view.

| Tab | Purpose |
| --- | --- |
| Library | Choose consoles and see games, existing covers and missing covers. |
| Downloads | See each queued game's state, search/filter the queue, cancel selected games or cancel all, and pause/resume. |
| Downloaded | Inspect completed artwork and its saved path, including downloads from earlier sessions. |
| Settings | Choose the provider, region, request limits and optional ScreenScraper account in the same window. |

Download tables show 200 rows per page, with Previous/Next controls, so large queues do not create tens of thousands of UI rows. Cancelling work keeps already completed artwork. The progress bar stays visible in every tab: first it shows selected-game checks, then missing-artwork jobs processed. Downloaded, existing, missing, failed and cancelled counts stay separate; 100% processed does not mean every game had a matching cover.

The application reads the installation's system configuration where available and scans supported ROM files recursively. Scanning uses cached directory information, skips unrelated video/manual folders, and processes up to two consoles at a time. Autodetection stops as soon as it finds a valid installation. It honours existing artwork metadata and avoids linked directories. Ambiguous or unsupported library layouts may need manual artwork.

## Providers and matching

**Libretro** uses the public [libretro-thumbnails collections](https://github.com/libretro-thumbnails/libretro-thumbnails) and `Named_Boxarts` images; see the [official thumbnail naming guide](https://docs.libretro.com/guides/roms-playlists-thumbnails/). Matching is deliberately conservative: a game title must match an available artwork title after the documented provider normalization. The app reports missing matches rather than selecting a similar title. A ROM filename, region label or custom game title that differs from the provider's naming may need a manual correction.

**ScreenScraper** needs both a registered application integration and your personal account. Ordinary users enter only username and password in **Settings**. NovaCore does **not** ship an approved developer credential pair, so a personal login alone cannot currently enable this provider. Settings explains this upfront, and Start remains unavailable for an unconfigured ScreenScraper setup. Use the visible **Use Libretro** button to scrape without an account.

For maintainers configuring an authorized NovaCore integration, the application pair is supplied through `NOVACORE_SS_DEVID` and `NOVACORE_SS_DEVPASSWORD`; account fields may be prefilled through `NOVACORE_SS_USERNAME` and `NOVACORE_SS_PASSWORD`. Local configuration is not proof of provider approval: ScreenScraper verifies the application and account on use. No other application's credentials are borrowed, and no credentials are saved in settings or logs. Account fields apply to the current session.

See [ScreenScraper's API documentation](https://www.screenscraper.fr/webapi2.php) and the [provider notes](docs/providers.md). NovaCore needs its own application registration before it can offer the same account-only user setup as an integrated frontend.

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

Tests use temporary libraries, mocked process checks and local or fixture provider responses. They check library scanning, exact console scope, dropdown and row selection, flexible search, per-game cancellation, durable history, metadata preservation and provider behavior without proving live provider availability or account access. The Windows Actions workflow runs the fixture suite, opens a short UI smoke test, builds the executable, checks its headless self-test and uploads a portable ZIP. Check the workflow's actual result before relying on a build; a workflow definition alone is not evidence of a successful Windows run.

## License

MIT, copyright (c) 2026 Lu Lu. See [LICENSE](LICENSE). System configuration discovery adapts MIT-licensed ideas from NovaCore Downloader; this project does not modify that application.
