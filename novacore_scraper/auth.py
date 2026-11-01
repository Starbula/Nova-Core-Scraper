"""Local readiness for NovaCore's own ScreenScraper application integration.

User account details belong to the normal settings screen. The application pair
comes from authorized deployment configuration; never borrow another app's pair.
These helpers do not verify credentials or contact ScreenScraper.
"""
from __future__ import annotations

import os


def screen_scraper_app_credentials() -> tuple[str, str] | None:
    """Return the complete configured application pair, without I/O or logging.

    Empty/whitespace-only or partial configuration is unavailable. Preserve the
    actual values, including potentially meaningful password whitespace.
    """
    devid = os.environ.get('NOVACORE_SS_DEVID', '')
    password = os.environ.get('NOVACORE_SS_DEVPASSWORD', '')
    if not devid.strip() or not password.strip():
        return None
    return devid, password


def screen_scraper_status() -> str:
    """Explain local application readiness without exposing credential values."""
    if screen_scraper_app_credentials() is None:
        return (
            "ScreenScraper is unavailable: NovaCore's application registration "
            'is not configured. Use Libretro to download covers without an account.'
        )
    return (
        "NovaCore's ScreenScraper application credentials are configured. "
        'Enter your ScreenScraper username and password; the service verifies '
        'application and account access when you start.'
    )
