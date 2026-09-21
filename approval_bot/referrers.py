from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class ReferrerFileError(ValueError):
    """Raised when the referrers JSON file cannot be parsed."""


def clean_name(name: str) -> str:
    return " ".join(name.split())


def normalize_name(name: str) -> str:
    """Comparison key: case-insensitive, with surrounding and repeated whitespace ignored."""
    return clean_name(name).casefold()


class ReferrerList:
    """Names that are accepted as referrers, stored in a JSON file.

    The file is re-read automatically if it is edited by hand while the bot runs.
    """

    def __init__(self, path: Path):
        self.path = path
        self._names: list[str] = []
        self._index: dict[str, str] = {}
        self._mtime: int | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def parse(data: Any) -> list[str]:
        """Accepts ["Name", ...] or {"names": ["Name", ...]}; returns cleaned, de-duplicated names."""
        if isinstance(data, dict):
            data = data.get("names")
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ReferrerFileError('expected a JSON list of names, or {"names": [...]}')

        names: list[str] = []
        seen: set[str] = set()
        for raw in data:
            name = clean_name(raw)
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                names.append(name)
        return names

    def load(self) -> None:
        if not self.path.exists():
            log.info("Creating empty referrers file at %s", self.path)
            self._write([])
        try:
            names = self.parse(json.loads(self.path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ReferrerFileError) as exc:
            raise ReferrerFileError(f"{self.path}: {exc}") from exc
        self._set(names)
        self._mtime = self.path.stat().st_mtime_ns

    def _refresh(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            return
        if mtime == self._mtime:
            return
        try:
            self.load()
            log.info("Reloaded %d referrer names from %s", len(self._names), self.path)
        except ReferrerFileError as exc:
            log.warning("Ignoring invalid edit to referrers file, keeping previous list: %s", exc)
            self._mtime = mtime

    def _set(self, names: list[str]) -> None:
        self._names = names
        self._index = {name.casefold(): name for name in names}

    def _write(self, names: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"names": names}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime_ns

    @property
    def names(self) -> list[str]:
        self._refresh()
        return list(self._names)

    def match(self, name: str) -> str | None:
        """Returns the stored spelling of `name` if it is on the list."""
        self._refresh()
        return self._index.get(normalize_name(name))

    async def add(self, name: str) -> bool:
        async with self._lock:
            self._refresh()
            name = clean_name(name)
            if not name or name.casefold() in self._index:
                return False
            names = [*self._names, name]
            self._write(names)
            self._set(names)
            return True

    async def remove(self, name: str) -> str | None:
        async with self._lock:
            self._refresh()
            stored = self._index.get(normalize_name(name))
            if stored is None:
                return None
            names = [n for n in self._names if n != stored]
            self._write(names)
            self._set(names)
            return stored

    async def replace(self, names: list[str]) -> int:
        async with self._lock:
            names = self.parse(names)
            self._write(names)
            self._set(names)
            return len(names)

    def to_json(self) -> str:
        return json.dumps({"names": self.names}, indent=2, ensure_ascii=False) + "\n"
