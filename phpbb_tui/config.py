"""Welche Foren kennt das Programm, und wo liegen Cookies und Downloads.

Konfiguration: ~/.config/phpbb-tui/boards.toml

    downloads = "~/Downloads"     # optional
    browser   = "xdg-open"        # optional

    [[board]]
    name = "example"
    url  = "https://forum.example.org/forum"
    user = "myname"               # optional, spart Tippen beim Anmelden
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "phpbb-tui"
CONFIG_FILE = CONFIG_DIR / "boards.toml"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "phpbb-tui"


@dataclass
class BoardConfig:
    name: str
    url: str
    user: str = ""

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or "board"

    @property
    def cookie_path(self) -> Path:
        return STATE_DIR / f"{self.slug}.cookies.txt"


@dataclass
class Config:
    boards: list[BoardConfig]
    downloads: Path
    browser: str

    def finde(self, name: str) -> BoardConfig | None:
        name = name.lower()
        for b in self.boards:
            if b.name.lower() == name or b.slug == name:
                return b
        treffer = [b for b in self.boards if name in b.name.lower()]
        return treffer[0] if len(treffer) == 1 else None


def laden() -> Config:
    daten: dict = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "rb") as f:
            daten = tomllib.load(f)

    boards = [
        BoardConfig(name=b.get("name") or b.get("url", ""),
                    url=b["url"],
                    user=b.get("user", ""))
        for b in daten.get("board", []) if b.get("url")
    ]
    downloads = Path(os.path.expanduser(
        daten.get("downloads") or os.environ.get("PHPBB_TUI_DOWNLOADS")
        or (Path.home() / "Downloads")))
    # Absichtlich nicht $BROWSER: das ist auf vielen Systemen ein
    # Terminal-Browser, und genau den will man hier ja nicht aufrufen.
    browser = (os.environ.get("PHPBB_TUI_BROWSER") or daten.get("browser")
               or "xdg-open")
    return Config(boards=boards, downloads=downloads, browser=browser)


def board_hinzufuegen(name: str, url: str, user: str = "") -> BoardConfig:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    text = CONFIG_FILE.read_text(encoding="utf-8") if CONFIG_FILE.exists() else ""
    eintrag = f'\n[[board]]\nname = "{name}"\nurl  = "{url.rstrip("/")}"\n'
    if user:
        eintrag += f'user = "{user}"\n'
    CONFIG_FILE.write_text(text + eintrag, encoding="utf-8")
    return BoardConfig(name=name, url=url.rstrip("/"), user=user)
