"""Distinguish XML indentation from literal Windows filename spaces."""
from __future__ import annotations


def xml_path_text(value: str) -> str:
    """Remove explicit XML formatting, without collapsing spaced filenames.

    Tabs/newlines cannot be Windows filenames. Indentation before an explicit
    ./ or ../ reference is formatting too. Plain leading/trailing spaces may
    name a different file, even if it is currently missing, and stay intact.
    This only resolves text; callers still enforce their path boundaries.
    """
    if any(character in value for character in '\r\n\t'):
        return value.strip()
    if value != value.lstrip() and value.lstrip().startswith(('./', '../', '.\\', '..\\')):
        return value.strip()
    return value
