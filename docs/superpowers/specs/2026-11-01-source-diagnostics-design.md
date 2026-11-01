# Source diagnostics and quota recovery

The current final error names failed services but omits successful searches with no match. A saved API field is configuration, not verified access. Users need evidence of which sources participated and why a game remains unresolved.

Add a Sources tab with the frozen run order, preparation/lookup/outage state, game checks, matches, saved covers, actual HTTP/API dispatch counts and remaining quota when reported. Show all supported services, explicitly distinguishing unavailable ScreenScraper application registration from a configured personal login. Cache reads must not increment network counts. Keep Tk updates bounded to seven rows, at most twice per second.

Preserve a bounded per-game source trace in recovery state, download history, details and CSV. An exhausted service remains isolated; a later healthy source must still download. Keep incomplete searches Failed rather than falsely asserting no artwork exists. Automatic should try configured IGDB before spending TheGamesDB's limited monthly allowance.

Fix the reproduced race in which local TheGamesDB quota exhaustion aborts another worker's already paid successful response. Genuine authentication failures remain fatal. Add only verified equivalent RetroBat system aliases.

Retain rate limits, matching safeguards, credentials isolation, independent downloads, existing artwork protection and deferred/atomic metadata writes. No account secrets, request URLs, ROM changes or quota bypasses. No live account verification is implied by configuration or fixtures.

Approach: extend the existing provider chain and recovery diagnostics rather than introducing another download scheduler or issuing redundant searches to every service after a successful cover. Games remain concurrent; each game's sources are tried in order until its cover saves.
