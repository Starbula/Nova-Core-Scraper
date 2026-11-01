"""Build the portable desktop executable with the current Python interpreter."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--console",
        action="store_true",
        help="keep a console window for diagnostics (default: desktop window only)",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console" if args.console else "--windowed",
        "--name",
        "NovaCore-Artwork-Scraper",
        "--add-data",
        f"{root / 'LICENSE'}{os.pathsep}.",
        "main.py",
    ]
    try:
        subprocess.run(command, cwd=root, check=True)
    except FileNotFoundError:
        print("Python could not be started. Reinstall Python and try again.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print("Build failed. See the PyInstaller output above.", file=sys.stderr)
        return exc.returncode or 1
    print(f"Executable: {root / 'dist' / ('NovaCore-Artwork-Scraper.exe' if os.name == 'nt' else 'NovaCore-Artwork-Scraper')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
