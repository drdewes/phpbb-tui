"""Einstieg: Foren verwalten, anmelden, Oberfläche starten."""

from __future__ import annotations

import getpass
import sys

from . import config as cfgmod
from .board import Board, BoardError
from .i18n import hilfe as hilfetext
from .i18n import t

def _board(bc: cfgmod.BoardConfig) -> Board:
    return Board(bc.url, bc.cookie_path, bc.name)


def anmelden(b: Board, user: str = "") -> bool:
    print(t("login_kopf", name=b.name, url=b.base))
    print(t("login_hinweis") + "\n")
    frage = t("benutzername") + (f" [{user}]" if user else "") + ": "
    eingabe = input(frage).strip() or user
    if not eingabe:
        print(t("kein_benutzername"))
        return False
    try:
        b.login(eingabe, getpass.getpass(t("passwort")))
    except BoardError as e:
        print("\n" + t("fehlgeschlagen", fehler=e))
        return False
    print("\n" + t("angemeldet_als", name=b.username or eingabe))
    return True


def auswahl(cfg: cfgmod.Config) -> cfgmod.BoardConfig | None:
    if len(cfg.boards) == 1:
        return cfg.boards[0]
    print(t("cli_welches") + "\n")
    for i, b in enumerate(cfg.boards, 1):
        print(f"  {i}. {b.name}   {b.url}")
    print()
    try:
        wahl = input(t("cli_nummer")).strip() or "1"
        return cfg.boards[int(wahl) - 1]
    except (ValueError, IndexError, EOFError, KeyboardInterrupt):
        return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Gastmodus: viele Boards sind ohne Konto lesbar. Schreiben geht dann
    # nicht, das Forum sagt es beim Versuch selbst.
    gast = "--guest" in argv or "--gast" in argv
    argv = [a for a in argv if a not in ("--guest", "--gast")]
    cfg = cfgmod.laden()

    if argv and argv[0] in ("-h", "--help", "hilfe", "help"):
        print(hilfetext())
        return 0

    if argv and argv[0] == "add":
        if len(argv) < 2:
            print(t("cli_aufruf_add"))
            return 1
        url = argv[1].rstrip("/")
        name = argv[2] if len(argv) > 2 else url.split("//")[-1].split("/")[0]
        probe = Board(url, cfgmod.STATE_DIR / "probe.cookies.txt", name)
        try:
            probe.get("index.php")
        except Exception as e:
            print(t("cli_nicht_erreichbar", fehler=e))
            return 1
        bc = cfgmod.board_hinzufuegen(name, url)
        b = _board(bc)
        if not gast and not anmelden(b):
            return 1
        print(t("cli_eingerichtet", name=name, slug=bc.slug))
        return 0

    if argv and argv[0] == "list":
        if not cfg.boards:
            print(t("cli_kein_forum") + " " + t("cli_hinzufuegen").strip())
            return 1
        for b in cfg.boards:
            zustand = t("angemeldet") if b.cookie_path.exists() else t("cli_nicht_angemeldet")
            print(f"  {b.name:<20} {b.url:<45} {zustand}")
        return 0

    if argv and argv[0] in ("login", "logout", "status"):
        befehl = argv[0]
        bc = cfg.finde(argv[1]) if len(argv) > 1 else auswahl(cfg)
        if not bc:
            print(t("cli_forum_fehlt", name=argv[1] if len(argv) > 1 else "?"))
            return 1
        b = _board(bc)
        if befehl == "logout":
            bc.cookie_path.unlink(missing_ok=True)
            print(t("cli_sitzung_weg"))
            return 0
        if befehl == "status":
            drin = b.logged_in()
            print(f"{bc.name}: " + (t("angemeldet_als", name=b.username).rstrip(".")
                                    if drin else t("cli_nicht_angemeldet")))
            return 0 if drin else 1
        return 0 if anmelden(b, bc.user) else 1

    if not cfg.boards:
        print(t("cli_kein_forum") + "\n")
        print(t("cli_hinzufuegen") + "\n")
        return 1

    gewaehlt = cfg.finde(argv[0]) if argv else None
    if argv and not gewaehlt:
        print(t("cli_forum_fehlt", name=argv[0]))
        return 1

    from .ui import starten
    while True:
        bc = gewaehlt or auswahl(cfg)
        if not bc:
            return 1
        b = _board(bc)
        if not gast and not b.logged_in():
            print(t("cli_noch_nicht", name=bc.name) + "\n")
            if not anmelden(b, bc.user):
                return 1
        if starten(b, cfg) != "wechseln":
            return 0
        gewaehlt = None      # zurück zur Auswahl


if __name__ == "__main__":
    raise SystemExit(main())
