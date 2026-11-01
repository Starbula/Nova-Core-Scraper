"""Portable non-secret settings. API credentials are session-only or environment supplied."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import os
import sys
import tempfile


@dataclass
class Settings:
    retrobat_folder: str = ''
    provider: str = 'libretro'
    region: str = 'eu'
    workers: int = 2
    interval: float = 1.0
    selected_systems: list[str] = field(default_factory=list)


def app_directory() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent.parent


def data_directory() -> Path:
    return app_directory()/'data'


def load_settings() -> Settings:
    try:
        raw=json.loads((data_directory()/'settings.json').read_text(encoding='utf-8'))
        if not isinstance(raw,dict):
            return Settings()
        folder=raw.get('retrobat_folder','')
        if not isinstance(folder,str):
            folder=''
        if folder and not Path(folder).is_absolute():
            folder=str((app_directory()/folder).resolve())
        selected=raw.get('selected_systems',[])
        if not isinstance(selected,list):
            selected=[]
        return Settings(
            retrobat_folder=folder,
            provider=raw.get('provider') if raw.get('provider') in ('libretro','screenscraper') else 'libretro',
            region=raw.get('region') if raw.get('region') in ('eu','us','jp','wor','fr','de','uk','es','it','br','au') else 'eu',
            workers=max(1,min(4,int(raw.get('workers',2)))),
            interval=max(.5,min(60,float(raw.get('interval',1)))),
            selected_systems=[x for x in selected if isinstance(x,str)],
        )
    except (OSError,ValueError,TypeError,OverflowError):
        return Settings()


def save_settings(settings: Settings):
    directory=data_directory()
    directory.mkdir(parents=True,exist_ok=True)
    raw=asdict(settings)
    if raw['retrobat_folder']:
        try:
            raw['retrobat_folder']=os.path.relpath(raw['retrobat_folder'],app_directory())
        except ValueError:
            pass  # Different Windows drive: keep the absolute selection.
    fd,name=tempfile.mkstemp(prefix='.settings-',suffix='.tmp',dir=directory)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(raw,f,indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name,directory/'settings.json')
    finally:
        Path(name).unlink(missing_ok=True)
