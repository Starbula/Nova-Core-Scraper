"""Small shared contracts; paths are absolute, game keys are system-relative."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class System:
    name: str
    fullname: str
    path: Path
    extensions: frozenset[str]
    screenscraper_id: str | None = None


@dataclass(frozen=True)
class Game:
    system: System
    path: Path
    relative_path: str
    title: str
    existing_artwork: Path | None = None

    @property
    def key(self) -> str:
        return self.system.name + '/' + self.relative_path


@dataclass(frozen=True)
class Artwork:
    url: str
    source: str
    region: str = ''
