# NovaCore Artwork Scraper

Implement the user's supplied specification as a portable Windows 10/11 Python/Tk desktop application. Standalone workers continue scraping while RetroBat runs. Box artwork only; manuals, ratings and pad-to-key remain unavailable and off in v1.

Use Libretro's public Named_Boxarts as the no-account default, with conservative exact-title matching; optionally support ScreenScraper with legitimately configured developer and user credentials. Never use another application's credentials. Cache provider indexes, limit request concurrency/rate, retry transient errors and report unmatched titles without guessing.

Adapt local es_systems.cfg validation/path parsing from NovaCore Downloader (MIT); do not modify that repository. Discover installed configured systems and use conservative fallback definitions if no config exists. Scan recursively without following links, collapse launch manifests where possible, honour gamelist entries and existing image/thumbnail metadata. Keep all ROM access read-only.

Write downloaded validated images exclusively into a dedicated images/novacore directory, with collision-resistant deterministic names. Persist per-game status and pending metadata in SQLite under RetroBat/.novacore-artwork so drive-letter changes do not destroy progress. Protect each installation against multiple writers.

EmulationStation owns in-memory metadata: while its process or RetroBat is present, download images but defer ALL gamelist writes. Automatically apply queued entries after it exits, merging against freshly read XML, preserving unknown fields, comments, attributes and existing artwork. Use backups, revision checks and atomic replacement. Arbitrary non-cooperating writers cannot be locked universally; detect modifications and leave pending rather than overwrite.

Python/Tk reduces deployment/UI complexity compared with the related PySide application. No web UI or downloader dependencies. UI changes happen only on its main thread, at throttled intervals; no widget per game. Test with temporary libraries, local HTTP fixtures and simulated process gates. Windows Actions builds and smoke-tests a PyInstaller executable; no claim of real provider or Windows testing without evidence.
