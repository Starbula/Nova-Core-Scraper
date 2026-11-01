# NovaCore Artwork Scraper

A portable Windows 10/11 desktop tool for filling in missing RetroBat box artwork. Choose a RetroBat installation and the systems you want to scan, then start scraping. The default Libretro provider needs no account.

Box artwork is the only supported media in this version. Manuals, ratings and pad-to-key are unavailable and disabled.

## Run on Windows

Download the portable app artifact from a successful **Windows executable** run on this repository's **Actions** page. Extract the ZIP into a writable folder and run `NovaCore-Artwork-Scraper.exe`. Keep `LICENSE` with redistributed copies.

For source use, install Python 3.11 or newer with Tcl/Tk support, extract or clone this repository, and double-click **Launch NovaCore Scraper.cmd**. The launcher creates a local `.venv` and installs the runtime requirements; the initial setup needs internet access. Errors stay visible in the launch window.

1. Click **Browse** and select the folder containing `retrobat.exe` and `roms`, or use **Autodetect**. The app scans in the background; **Scan library** refreshes it later.
2. Tick the consoles you want, or use **Select visible**. The console filter does not clear hidden selections. Keep Libretro selected unless you have your own ScreenScraper credentials.
3. Click **Start scraping**. Use **Pause / Resume** or **Stop** as needed; starting again skips downloaded box artwork. Counts show downloaded, existing, missing, failed and queued metadata.

The application reads the installation's system configuration where available and scans supported ROM files recursively. It honours existing artwork metadata and avoids linked directories. Ambiguous or unsupported library layouts may need manual artwork.

## Providers and matching

**Libretro** uses the public [libretro-thumbnails collections](https://github.com/libretro-thumbnails/libretro-thumbnails) and `Named_Boxarts` images; see the [official thumbnail naming guide](https://docs.libretro.com/guides/roms-playlists-thumbnails/). Matching is deliberately conservative: a game title must match an available artwork title after the documented provider normalization. The app reports missing matches rather than selecting a similar title. A ROM filename, region label or custom game title that differs from the provider's naming may need a manual correction.

**ScreenScraper** is optional. Configure your own ScreenScraper user account and developer credentials issued for your application. NovaCore does not contain or borrow another program's developer credentials. Access and quotas depend on your ScreenScraper account and the service. See [ScreenScraper's API documentation](https://www.screenscraper.fr/webapi2.php) and the [provider notes](docs/providers.md) for the implemented API and matching details. Credentials are used for the current session or supplied through environment variables; they are not saved in the settings file.

The optional environment variables are `NOVACORE_SS_DEVID`, `NOVACORE_SS_DEVPASSWORD`, `NOVACORE_SS_USERNAME` and `NOVACORE_SS_PASSWORD`.

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

Tests use temporary libraries, mocked process checks and local or fixture provider responses. They check library scanning, durable progress, metadata preservation and provider behavior without proving live provider availability or account access. The Windows Actions workflow runs the fixture suite, opens a short UI smoke test, builds the executable, checks its headless self-test and uploads a portable ZIP. Check the workflow's actual result before relying on a build; a workflow definition alone is not evidence of a successful Windows run.

## License

MIT, copyright (c) 2026 Lu Lu. See [LICENSE](LICENSE). System configuration discovery adapts MIT-licensed ideas from NovaCore Downloader; this project does not modify that application.
