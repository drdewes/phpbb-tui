"""Die Oberfläche: Foren → Themen → Beiträge → Schreiben.

Bewusst schlicht: eine Liste, ein Text, ein Editor. Keine Fenster, keine
Mausunterstützung, keine Fremdbibliothek.
"""

from __future__ import annotations

import curses
import os
import shlex
import subprocess
import tempfile
import textwrap
from pathlib import Path

from .board import (PM_ORDNER, SUCHEN, UCP_LISTEN, Anhang, Board, BoardError, Entwurf,
                    Forum, LoginRequired, Nachricht, PM, Thread, Topic)
from .config import Config
from .i18n import t
from . import entwuerfe, tui_submission
from .tui_pagination import navigation
from .markup import bbcode_to_md
from .attachments import attachment_fields
from .command_error import CommandError
from . import tui_attachments


C_KOPF, C_NEU, C_AUTOR, C_DATUM, C_HILFE, C_ZITAT, C_AUSWAHL, C_WARN, C_ANHANG = range(1, 10)


def _farben() -> None:
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_KOPF, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_NEU, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_AUTOR, curses.COLOR_CYAN, -1)
    curses.init_pair(C_DATUM, curses.COLOR_BLUE, -1)
    curses.init_pair(C_HILFE, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(C_ZITAT, curses.COLOR_GREEN, -1)
    curses.init_pair(C_AUSWAHL, curses.COLOR_WHITE, curses.COLOR_BLUE)
    curses.init_pair(C_WARN, curses.COLOR_RED, -1)
    curses.init_pair(C_ANHANG, curses.COLOR_MAGENTA, -1)


class Wechseln(Exception):
    """Nutzer will das Board wechseln."""


class Oberflaeche:
    def __init__(self, scr, board: Board, cfg: Config):
        self.scr = scr
        self.b = board
        self.cfg = cfg
        self.meldung = ""

    # -- Grundgerüst -------------------------------------------------------

    @property
    def hoehe(self) -> int:
        return self.scr.getmaxyx()[0]

    @property
    def breite(self) -> int:
        return self.scr.getmaxyx()[1]

    def _zeile(self, y: int, text: str, attr=0, x: int = 0) -> None:
        if 0 <= y < self.hoehe:
            try:
                self.scr.addnstr(y, x, text, max(0, self.breite - 1 - x), attr)
            except curses.error:
                pass

    def kopf(self, links: str, rechts: str = "") -> None:
        leiste = f" {links}".ljust(max(0, self.breite - len(rechts) - 2)) + rechts + " "
        self._zeile(0, leiste[: self.breite - 1], curses.color_pair(C_KOPF) | curses.A_BOLD)

    def fuss(self, hilfe: str) -> None:
        text = self.meldung or hilfe
        attr = (curses.color_pair(C_WARN) | curses.A_BOLD if self.meldung
                else curses.color_pair(C_HILFE))
        self._zeile(self.hoehe - 1, f" {text}".ljust(self.breite - 1)[: self.breite - 1], attr)
        self.meldung = ""

    def warten(self, text: str = "") -> None:
        text = text or t("laedt")
        self._zeile(self.hoehe - 1, f" {text}".ljust(self.breite - 1)[: self.breite - 1],
                    curses.color_pair(C_HILFE))
        self.scr.refresh()

    def frage(self, text: str, vorgabe: str = "") -> str:
        curses.echo()
        curses.curs_set(1)
        self._zeile(self.hoehe - 1, " " * (self.breite - 1), curses.color_pair(C_HILFE))
        self._zeile(self.hoehe - 1, f" {text}{vorgabe}", curses.color_pair(C_HILFE))
        self.scr.refresh()
        try:
            roh = self.scr.getstr(self.hoehe - 1, len(text) + len(vorgabe) + 1, 300)
            antwort = vorgabe + roh.decode("utf-8", "replace").strip()
        except Exception:
            antwort = ""
        curses.noecho()
        curses.curs_set(0)
        return antwort

    def liste(self, titel: str, zeilen: list[str], hilfe: str,
              tasten: str = "", start: int = 0) -> tuple[str, int]:
        """Allgemeine Auswahlliste. Gibt (Taste, Position) zurück."""
        pos = start
        while True:
            self.scr.erase()
            self.kopf(titel)
            sicht = self.hoehe - 2
            oben = max(0, min(pos - sicht // 2, max(0, len(zeilen) - sicht)))
            for i, z in enumerate(zeilen[oben: oben + sicht]):
                attr = curses.color_pair(C_AUSWAHL) | curses.A_BOLD if oben + i == pos else 0
                self._zeile(1 + i, z.ljust(self.breite - 1)[: self.breite - 1], attr)
            if not zeilen:
                self._zeile(2, "  " + t("nichts"), curses.A_DIM)
            self.fuss(hilfe)
            self.scr.refresh()
            k = self.scr.getch()
            # Groß- und Kleinschreibung unterscheiden: N (neues Thema) darf
            # nicht als n (nächste Seite) durchgehen.
            if 0 < k < 256 and chr(k) in tasten:
                return chr(k), pos
            # Vim-Steuerung: hjkl, gg/G, ^D/^U, Leertaste/b
            if k in (10, 13, ord("l"), curses.KEY_RIGHT):
                return "\n", pos
            if k in (ord("q"), 27, ord("h"), curses.KEY_LEFT):
                return "q", pos
            if k in (ord("j"), curses.KEY_DOWN):
                pos = min(pos + 1, max(0, len(zeilen) - 1))
            elif k in (ord("k"), curses.KEY_UP):
                pos = max(0, pos - 1)
            elif k in (ord(" "), curses.KEY_NPAGE):
                pos = min(pos + sicht, max(0, len(zeilen) - 1))
            elif k in (ord("b"), curses.KEY_PPAGE):
                pos = max(0, pos - sicht)
            elif k == 4:                                    # ^D halbe Seite vor
                pos = min(pos + sicht // 2, max(0, len(zeilen) - 1))
            elif k == 21:                                   # ^U halbe Seite zurück
                pos = max(0, pos - sicht // 2)
            elif k == ord("g"):
                pos = 0
            elif k == ord("G"):
                pos = max(0, len(zeilen) - 1)

    def seite_zeigen(self, titel: str, zeilen: list[str], hilfe: str = "",
                     tasten: str = "") -> str:
        hilfe = hilfe or t("hilfe_zurueck")
        pos = 0
        while True:
            self.scr.erase()
            self.kopf(titel)
            sicht = self.hoehe - 2
            for i, z in enumerate(zeilen[pos: pos + sicht]):
                text, attr = z if isinstance(z, tuple) else (z, 0)
                self._zeile(1 + i, text, attr)
            self.fuss(hilfe)
            self.scr.refresh()
            k = self.scr.getch()
            if 0 < k < 256 and chr(k).lower() in tasten.lower() and tasten:
                return chr(k).lower()
            if k in (ord("q"), 27, ord("h"), curses.KEY_LEFT):
                return "q"
            if k in (ord("j"), curses.KEY_DOWN):
                pos = min(pos + 1, max(0, len(zeilen) - sicht))
            elif k in (ord("k"), curses.KEY_UP):
                pos = max(0, pos - 1)
            elif k in (ord(" "), curses.KEY_NPAGE):
                pos = min(pos + sicht, max(0, len(zeilen) - sicht))
            elif k in (ord("b"), curses.KEY_PPAGE):
                pos = max(0, pos - sicht)
            elif k == 4:
                pos = min(pos + sicht // 2, max(0, len(zeilen) - sicht))
            elif k == 21:
                pos = max(0, pos - sicht // 2)
            elif k == ord("g"):
                pos = 0
            elif k == ord("G"):
                pos = max(0, len(zeilen) - sicht)

    def bestaetigen(self, text: str) -> bool:
        self._zeile(self.hoehe - 1, f" {text} {t('ja_nein')} ".ljust(self.breite - 1),
                    curses.color_pair(C_HILFE))
        self.scr.refresh()
        while True:
            k = self.scr.getch()
            if k in (ord("j"), ord("J"), ord("y"), ord("Y"), 10):
                return True
            if k in (ord("n"), ord("N"), 27, ord("q")):
                return False

    def oeffnen(self, ziel: str) -> None:
        """Adresse oder Datei im Standardprogramm des Systems öffnen."""
        try:
            subprocess.Popen(shlex.split(self.cfg.browser) + [ziel],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.meldung = t("geoeffnet", name=os.path.basename(ziel)[:40])
        except Exception as e:
            self.meldung = t("fehler_oeffnen", fehler=e)

    # -- Ebene 1: Foren ----------------------------------------------------

    def menue(self) -> None:
        foren: list[Forum] = []
        laden = True
        pos = 0
        filter_text = ""
        springe_zu_treffer = False
        while True:
            if laden:
                self.warten()
                try:
                    foren = self.b.foren()
                except LoginRequired:
                    if not self.anmelden():
                        return
                    continue
                except BoardError as e:
                    self.meldung = str(e)
                    foren = []
                laden = False

            eintraege: list[tuple[str, object]] = []
            zeilen: list[str] = []
            # Als Gast gibt es kein Postfach – dann auch keinen Eintrag dafür.
            if self.b.username:
                eintraege.append(("pm", None))
                zeilen.append(f"  ✉ {t('pm_titel')}"
                              + (f"   ({self.b.pm_neu} {t('pm_neu')})"
                                 if self.b.pm_neu else ""))
            eintraege += [("suche", sid) for sid, _ in SUCHEN]
            zeilen += [f"  ★ {t(schl)}" for _, schl in SUCHEN]
            if self.b.username:
                eintraege += [("ucp", modus) for modus, _ in UCP_LISTEN]
                zeilen += [f"  ☆ {t(schl)}" for _, schl in UCP_LISTEN]
            eintraege.append(("trenner", None))
            zeilen.append("  " + "─" * 40)
            eltern: list[str] = []          # Pfad der übergeordneten Foren
            for f in foren:
                del eltern[f.ebene:]
                pfad = " › ".join(eltern)
                eltern.append(f.name)
                if filter_text and filter_text.lower() not in f.name.lower():
                    continue
                if filter_text:
                    # Ohne Baum drumherum sagt erst der Pfad, welches
                    # „Fehlermeldungen“ gemeint ist.
                    zeilen.append(f"  ▸ {f.name}" + (f"   ({pfad})" if pfad else ""))
                else:
                    einzug = "  " + "   " * f.ebene
                    zusatz = f"   {f.beschreibung}" if f.beschreibung else ""
                    zeilen.append(f"{einzug}▸ {f.name}{zusatz}")
                eintraege.append(("forum", f))
            if springe_zu_treffer:
                pos = next((i for i, (art, _) in enumerate(eintraege)
                            if art == "forum"), pos)
                springe_zu_treffer = False

            taste, pos = self.liste(
                f"{self.b.name} · {self.b.username or t('gast')}"
                + (f"  ⟨{filter_text}⟩" if filter_text else ""),
                zeilen, t("hilfe_menue"), tasten="/swRmM", start=pos)

            if taste == "q":
                return
            if taste == "w":
                raise Wechseln
            if taste == "m":
                self.postfach()
                laden = True            # frischt nebenbei den Zähler auf
            elif taste == "R":
                laden = True
            elif taste == "M" and self.b.username:
                if self._als_gelesen(None, t("gelesen_alle_frage")):
                    laden = True
            elif taste == "/":
                filter_text = self.frage(t("frage_foren_filter"))
                pos, springe_zu_treffer = 0, bool(filter_text)
            elif taste == "s":
                worte = self.frage(t("frage_suche"))
                if worte:
                    self.themenliste(worte=worte)
            elif taste == "\n" and pos < len(eintraege):
                art, wert = eintraege[pos]
                if art == "pm":
                    self.postfach()
                    laden = True
                elif art == "suche":
                    self.themenliste(such_id=str(wert))
                elif art == "ucp":
                    self.themenliste(ucp=str(wert))
                elif art == "forum":
                    self.themenliste(fid=wert.fid)      # type: ignore[union-attr]

    # -- Ebene 2: Themen ---------------------------------------------------

    def _als_gelesen(self, fid: int | None, frage: str) -> bool:
        """Nach Rückfrage das Board bzw. ein Forum als gelesen markieren."""
        if not self.bestaetigen(frage):
            return False
        self.warten()
        try:
            self.b.als_gelesen_markieren(fid)
        except BoardError as fehler:
            self.meldung = str(fehler)
            return False
        self.meldung = t("gelesen_erledigt")
        return True

    def themenliste(self, fid: int | None = None, such_id: str | None = None,
                    worte: str = "", ucp: str = "") -> None:
        start, pos, filter_text = 0, 0, ""
        nav: dict[str, int] = {}
        themen: list[Topic] = []
        unterforen: list[Forum] = []
        kopf, seite, seiten = "", 1, 1
        laden = True
        # „u“: der Reihe nach durch die ungelesenen Themen. `besucht` hält
        # fest, was schon offen war, falls das Board es nicht als gelesen
        # führt; `frisch` heißt, die Seite wurde dafür schon neu geladen.
        weiter_ungelesen, frisch, leere_seiten = False, False, 0
        besucht: set[int] = set()

        while True:
            if laden:
                self.warten()
                try:
                    if fid is not None:
                        themen, unterforen, kopf, seite, seiten = self.b.topics(fid, start)
                    elif worte:
                        themen, unterforen, kopf, seite, seiten = self.b.suche(worte, start)
                    elif ucp:
                        themen, unterforen, kopf, seite, seiten = self.b.ucp_themen(ucp, start)
                    else:
                        themen, unterforen, kopf, seite, seiten = self.b.search(str(such_id), start)
                except LoginRequired:
                    if not self.anmelden():
                        return
                    continue
                except BoardError as e:
                    self.meldung = str(e)
                    themen, unterforen = [], []
                    nav = {}
                else:
                    nav = navigation(self.b, seite, seiten)
                laden = False
                pos = 0

            sichtbar = [x for x in themen
                        if not filter_text or filter_text.lower() in x.title.lower()]
            eintraege: list[tuple[str, object]] = [("forum", f) for f in unterforen]
            zeilen = [f"  ▸ {f.name}" for f in unterforen]
            if unterforen and sichtbar:
                zeilen.append("  " + "─" * 40)
                eintraege.append(("trenner", None))
            for thema in sichtbar:      # nicht „t": das ist die Übersetzung
                datum = (thema.last_date or "")[:19]
                rechte = f"{thema.replies:>4}  {thema.last_author[:18]:<18} {datum:>19}"
                marke = "!" if thema.sticky else ("●" if thema.unread else " ")
                platz = max(10, self.breite - len(rechte) - 6)
                zeilen.append(f" {marke} {thema.title[:platz]}".ljust(
                    max(0, self.breite - len(rechte) - 2)) + rechte)
                eintraege.append(("thema", thema))

            if weiter_ungelesen:
                ziel = next((i for i, (art, x) in enumerate(eintraege)
                             if art == "thema" and x.unread           # type: ignore[union-attr]
                             and x.tid not in besucht), None)         # type: ignore[union-attr]
                if ziel is not None:
                    pos, leere_seiten = ziel, 0
                    taste = "\n"
                elif not frisch:
                    # Suchlisten wie „Ungelesene Beiträge“ rücken nach, wenn
                    # Gelesenes herausfällt: erst dieselbe Seite neu laden.
                    frisch, laden = True, True
                    continue
                elif "n" in nav and leere_seiten < 3:
                    # Höchstens drei Seiten ohne Treffer weitersuchen; ein
                    # großes, gelesenes Forum soll nicht ganz durchlaufen.
                    start, laden, frisch = nav["n"], True, False
                    leere_seiten += 1
                    continue
                else:
                    weiter_ungelesen = False
                    self.meldung = t("keine_ungelesenen_mehr")
                    laden = True
                    continue
            else:
                taste, pos = self.liste(
                    f"{kopf}{f'  ⟨{filter_text}⟩' if filter_text else ''}",
                    zeilen, t("hilfe_liste"), tasten="/npNRMu", start=pos)

            if taste == "q":
                return
            if taste == "u":
                weiter_ungelesen, frisch = True, False
                continue
            if taste in ("n", "p"):
                if taste in nav:
                    start = nav[taste]
                    laden = True
                elif ((taste == "n" and seite < seiten)
                      or (taste == "p" and start > 0)):
                    self.meldung = t("navigation_fehlt")
            elif taste == "R":
                laden = True
            elif taste == "/":
                filter_text = self.frage(t("frage_filter"))
                pos = 0
            elif taste == "N" and fid is not None:
                if self.schreiben(mode="post", fid=fid):
                    laden = True
            elif taste == "M" and fid is not None and self.b.username:
                if self._als_gelesen(fid, t("gelesen_forum_frage")):
                    laden = True
            elif taste == "\n" and pos < len(eintraege):
                art, wert = eintraege[pos]
                if art == "forum":
                    self.themenliste(fid=wert.fid)      # type: ignore[union-attr]
                elif art == "thema":
                    # Ungelesene Themen öffnen beim ersten neuen Beitrag.
                    weiter = self.thread_zeigen(
                        wert.tid, ungelesen=wert.unread)  # type: ignore[union-attr]
                    besucht.add(wert.tid)                 # type: ignore[union-attr]
                    wert.unread = False                   # type: ignore[union-attr]
                    weiter_ungelesen, frisch = bool(weiter), False
                    laden = not weiter_ungelesen

    # -- Ebene 3: Beiträge -------------------------------------------------

    def thread_zeigen(self, tid: int, ungelesen: bool = False) -> bool:
        """Ein Thema lesen. True heißt: weiter zum nächsten ungelesenen („u“)."""
        start, pos = 0, 0
        th: Thread | None = None
        nav: dict[str, int] = {}
        laden = True
        zeilen: list[tuple[str, int, int]] = []

        while True:
            if laden:
                self.warten()
                try:
                    th = (self.b.thread(tid, start, ungelesen=True) if ungelesen
                          else self.b.thread(tid, start))
                except LoginRequired:
                    if not self.anmelden():
                        return False
                    continue
                except BoardError as e:
                    self.meldung = str(e)
                    return False
                nav = navigation(self.b, th.page, th.pages)
                zeilen = self._thread_zeilen(th)
                laden = False
                pos = 0
                if ungelesen:
                    # Nur beim Öffnen; Blättern und Neuladen bleiben normal.
                    start, ungelesen = th.start, False
                    if th.erster_neuer is not None:
                        pos = next((i for i, (_, _, n) in enumerate(zeilen)
                                    if n == th.erster_neuer), 0)

            assert th is not None
            sicht = self.hoehe - 2
            self.scr.erase()
            self.kopf(th.title, t("seite_von", a=th.page, b=th.pages))
            for i, (text, attr, _) in enumerate(zeilen[pos: pos + sicht]):
                self._zeile(1 + i, text, attr)
            self.fuss(t("hilfe_thread"))
            self.scr.refresh()

            akt = zeilen[min(pos, len(zeilen) - 1)][2] if zeilen else 0
            k = self.scr.getch()

            if k in (ord("q"), 27, ord("h"), curses.KEY_LEFT):
                return False
            elif k == ord("u"):
                return True
            elif k in (ord("j"), curses.KEY_DOWN):
                pos = min(pos + 1, max(0, len(zeilen) - 1))
            elif k in (ord("k"), curses.KEY_UP):
                pos = max(0, pos - 1)
            elif k in (ord(" "), curses.KEY_NPAGE):
                pos = min(pos + sicht, max(0, len(zeilen) - sicht))
            elif k in (ord("b"), curses.KEY_PPAGE):
                pos = max(0, pos - sicht)
            elif k == 4:                                    # ^D
                pos = min(pos + sicht // 2, max(0, len(zeilen) - sicht))
            elif k == 21:                                   # ^U
                pos = max(0, pos - sicht // 2)
            elif k == ord("g"):
                pos = 0
            elif k == ord("G"):
                pos = max(0, len(zeilen) - sicht)
            elif k == ord("J"):
                pos = self._sprung(zeilen, pos, +1)
            elif k == ord("K"):
                pos = self._sprung(zeilen, pos, -1)
            elif k in (ord("n"), ord("p")):
                key = chr(k)
                if key in nav:
                    start = nav[key]
                    laden = True
                elif ((key == "n" and th.page < th.pages)
                      or (key == "p" and start > 0)):
                    self.meldung = t("navigation_fehlt")
            elif k in (ord("R"), curses.KEY_F5):
                laden = True
            elif k == ord("o"):
                self.oeffnen(f"{self.b.base}/viewtopic.php?t={tid}")
            elif k == ord("a") and th.posts:
                self.anhaenge(th.posts[akt].author, th.posts[akt].anhaenge)
            elif k == ord("r"):
                if self.schreiben(mode="reply", tid=tid):
                    start = nav.get("last", start)
                    laden = True
            elif k == ord("z") and th.posts:
                if self.schreiben(mode="quote", tid=tid, pid=th.posts[akt].pid):
                    start = nav.get("last", start)
                    laden = True
            elif k == ord("e") and th.posts:
                p = th.posts[akt]
                if not p.own:
                    self.meldung = t("nur_eigene")
                elif self.schreiben(mode="edit", pid=p.pid):
                    laden = True

    def _thread_zeilen(self, th: Thread) -> list[tuple[str, int, int]]:
        breite = max(40, min(self.breite - 2, 100))
        zeilen: list[tuple[str, int, int]] = []
        for n, p in enumerate(th.posts):
            kopf = f"┌─ {p.author} · {p.date}"
            if p.own:
                kopf += "  " + t("eigener_beitrag")
            if p.unread:
                kopf += "  " + t("neu_markiert")
            zeilen.append((kopf.ljust(breite)[:breite],
                           curses.color_pair(C_AUTOR) | curses.A_BOLD, n))
            for absatz in p.body.split("\n"):
                if not absatz.strip():
                    zeilen.append(("", 0, n))
                    continue
                zitat = absatz.startswith("│")
                attr = curses.color_pair(C_ZITAT) if zitat else 0
                for teil in textwrap.wrap(absatz, breite - 2,
                                          subsequent_indent="│ " if zitat else "",
                                          drop_whitespace=True) or [""]:
                    zeilen.append(("  " + teil, attr, n))
            for anh in p.anhaenge:
                groesse = f" ({anh.groesse})" if anh.groesse else ""
                zeilen.append((f"  📎 {anh.name}{groesse}   – {t('anhang_taste')}",
                               curses.color_pair(C_ANHANG), n))
            zeilen.append(("", 0, n))
        return zeilen

    @staticmethod
    def _sprung(zeilen: list[tuple[str, int, int]], pos: int, richtung: int) -> int:
        if not zeilen:
            return 0
        akt = zeilen[min(pos, len(zeilen) - 1)][2]
        ziel = akt + richtung
        for i, (_, _, n) in enumerate(zeilen):
            if n == ziel:
                return i
        return pos

    # -- Anhänge -----------------------------------------------------------

    def anhaenge(self, autor: str, liste: list[Anhang]) -> None:
        if not liste:
            self.meldung = t("keine_anhaenge")
            return
        pos = 0
        while True:
            zeilen = [f"  📎 {a.name}" + (f"  ({a.groesse})" if a.groesse else "")
                      for a in liste]
            taste, pos = self.liste(
                t("anhaenge_von", autor=autor), zeilen,
                t("hilfe_anhaenge"), tasten="sS", start=pos)
            if taste == "q":
                return
            if taste in ("\n", "s", "S") and liste:
                anh = liste[pos]
                self.warten(t("laedt_datei", name=anh.name))
                try:
                    ordner, target = self.cfg.downloads, None
                    if taste == "S":
                        antwort = self.frage(t("download_ziel"))
                        if not antwort:
                            continue
                        path = Path(antwort).expanduser()
                        if not path.is_absolute():
                            # Ein bloßer Name gehört in den Download-Ordner,
                            # nicht dorthin, wo das Programm gestartet wurde.
                            path = Path(self.cfg.downloads).expanduser() / path
                        if path.is_symlink():
                            raise BoardError(t("download_unsicher"))
                        if antwort.endswith(os.sep) or path.is_dir():
                            ordner = path
                        else:
                            ordner, target = path.parent, path
                    ziel = self._anhang_speichern(anh, ordner, target)
                    if ziel is None:
                        continue
                except Exception as e:
                    self.meldung = t("fehler_download", fehler=e)
                    continue
                if taste in ("s", "S"):
                    self.meldung = t("gespeichert", pfad=ziel)
                else:
                    self.oeffnen(str(ziel))

    def _anhang_speichern(self, anh: Anhang, ordner: Path,
                          target: Path | None = None) -> Path | None:
        """Require confirmation before replacing a downloaded file."""
        try:
            return self.b.anhang_laden(
                anh, ordner, target=target, overwrite=False)
        except FileExistsError as fehler:
            if not fehler.filename:
                raise
            path = Path(fehler.filename)
            if path.is_symlink() or not path.is_file():
                raise BoardError(t("download_unsicher")) from fehler
            if not self._download_ersetzen(path):
                self.meldung = t("download_abgebrochen")
                return None
            return self.b.anhang_laden(
                anh, path.parent, target=path, overwrite=True)

    def _download_ersetzen(self, path: Path) -> bool:
        """Show and wrap the full destination before confirmation."""
        choice = self.seite_zeigen(
            t("download_bestaetigen"),
            textwrap.wrap(t("download_ersetzen", pfad=path),
                          max(20, self.breite - 4)),
            t("ja_nein") + " · q", tasten="yjn")
        return choice in ("y", "j")

    # -- Schreiben ---------------------------------------------------------

    def schreiben(self, mode: str, tid: int = 0, fid: int = 0, pid: int = 0) -> bool:
        self.warten(t("holt_formular"))
        try:
            if mode == "reply":
                e = self.b.reply_form(tid)
            elif mode == "quote":
                e = self.b.quote_form(tid, pid)
            elif mode == "post":
                e = self.b.new_topic_form(fid)
            elif mode == "edit":
                e = self.b.edit_form(pid)
            else:
                return False
        except LoginRequired:
            return self.schreiben(mode, tid, fid, pid) if self.anmelden() else False
        except BoardError as fehler:
            self.meldung = str(fehler)
            return False
        return self._verfassen(e)

    def _verfassen(self, e: Entwurf, titel: str = "", erfolg: str = "") -> bool:
        """Editor → Vorschau → Absenden.

        Kennt den Unterschied zwischen Beitrag und privater Nachricht nicht
        mehr: der Entwurf weiß selbst, wohin er geht.
        """
        betreff = e.felder.get("subject", "")
        vorlage = bbcode_to_md(e.felder.get("message", ""))
        ziel = (self.b.name, e.ziel, e.params)
        hinweis = ""
        gesichert = entwuerfe.laden(*ziel)
        if gesichert:
            # Ein früher hier angefangener Text geht vor die Vorlage des Forums.
            betreff = gesichert[0] or betreff
            vorlage, hinweis = gesichert[1], t("entwurf_zurueck")
        if "[attachment=" in vorlage:
            try:
                vorlage = tui_attachments.editor_body(e, vorlage)
            except CommandError:
                self.meldung = t("inline_fehler")
                return False

        while True:
            betreff, text = self._editor(betreff, vorlage, hinweis)
            hinweis = ""
            if not text.strip():
                entwuerfe.loeschen(*ziel)
                self.meldung = t("verworfen")
                return False
            try:
                entwuerfe.sichern(*ziel, betreff, text)
            except OSError as fehler:
                self.meldung = t("entwurf_nicht_gesichert", fehler=fehler)

            while True:      # Resolve stable attachment IDs for every preview.
                try:
                    bb = tui_attachments.render(e, text)
                except (ValueError, CommandError):
                    self.meldung = t("inline_fehler")
                    hinweis = t("inline_fehler")
                    wahl = "b"
                    break
                self.warten(t("vorschau_laeuft"))
                try:
                    seite = self.b.submit(e, betreff, bb, preview=True)
                    vorschau = self.b.preview_text(seite)
                except Exception as fehler:
                    vorschau = t("keine_vorschau") + f" ({fehler})"

                dateien = self.b.angehaengte_dateien(e)
                kopf = [t("betreff_zeile", betreff=betreff)]
                if e.empfaenger:
                    kopf.append(t("an_zeile", liste=", ".join(e.empfaenger)))
                if dateien:
                    kopf.append(t("anhaenge_zeile", liste=", ".join(dateien)))
                zeilen = kopf + [""] + vorschau.split("\n")
                # Boards erlauben Anhänge nicht überall – bei privaten
                # Nachrichten sind sie meist abgeschaltet. Dann fehlt der
                # Knopf im Formular, und wir bieten ihn gar nicht erst an.
                wahl = self.seite_zeigen(
                    titel or t("vorschau"), zeilen,
                    (t("hilfe_vorschau_inline") if dateien
                     else t("hilfe_vorschau")) if e.anhaenge_moeglich
                    else t("hilfe_vorschau_ohne_anhang"),
                    tasten=("adxb" + ("i" if dateien else ""))
                    if e.anhaenge_moeglich else "ab")

                if wahl == "i":
                    try:
                        inline_text = self._inline_anhang(e, text)
                    except CommandError:
                        self.meldung = t("inline_fehler")
                        continue
                    if inline_text is None:
                        continue
                    text = inline_text
                    vorlage = text
                    wahl = "b"
                    break

                if wahl == "d":
                    self._anhaengen(e)
                    continue
                if wahl not in ("a", "b", "d", "x"):
                    # Ein versehentliches q kostete früher den ganzen Text.
                    if not self.bestaetigen(t("verwerfen_frage")):
                        continue
                if wahl == "x":
                    if dateien:
                        self.warten(t("entfernt"))
                        try:
                            self.b.anhang_entfernen(e, len(dateien) - 1)
                        except Exception as fehler:
                            self.meldung = t("fehler_entfernen", fehler=fehler)
                    continue
                break

            if wahl == "a":
                self.warten(t("sendet"))
                outcome, meldung = tui_submission.submit(
                    self.b, e, betreff, bb)
                self.meldung = (erfolg if outcome == "success" and erfolg
                                else meldung)
                if outcome == "success":
                    entwuerfe.loeschen(*ziel)
                    return True
                if outcome == "uncertain":
                    # Der Entwurf bleibt: war der Beitrag nicht angekommen,
                    # ist der Text beim nächsten Antworten hier wieder da.
                    self.seite_zeigen(t("senden_unklar_titel"),
                                      textwrap.wrap(
                                          meldung + " " + t("entwurf_bleibt"),
                                          max(20, self.breite - 4)),
                                      t("hilfe_zurueck"))
                    return False
                if not self.bestaetigen(t("nochmal_bearbeiten", meldung=meldung)):
                    self.meldung = t("entwurf_bleibt")
                    return False
                vorlage = text
            elif wahl == "b":
                vorlage = text
            else:
                entwuerfe.loeschen(*ziel)
                self.meldung = t("verworfen")
                return False

    def _inline_anhang(self, e: Entwurf, text: str) -> str | None:
        """Select an uploaded file and append a movable editor placeholder."""
        items = sorted(attachment_fields(e).items(),
                       key=lambda item: item[1][0])
        if not items:
            return None
        key, pos = self.liste(
            t("inline_titel"),
            [f"{name} ({{{{attach:id{aid}}}}})" for aid, (_, name) in items],
            t("inline_auswahl"))
        if key != "\n":
            return None
        return text + "\n\n{{attach:id" + items[pos][0] + "}}"

    def _anhaengen(self, e: Entwurf) -> None:
        pfad = self.frage(t("frage_datei"))
        if not pfad:
            return
        self.warten(t("laedt_hoch"))
        try:
            self.b.datei_anhaengen(e, Path(pfad).expanduser())
            self.meldung = t("angehaengt")
        except Exception as fehler:
            self.meldung = t("fehler_anhaengen", fehler=fehler)

    def _editor(self, betreff: str, vorlage: str, hinweis: str = "") -> tuple[str, str]:
        kopfzeilen = [
            f"# {betreff}" if betreff else "# ",
            "",
            t("editor_hinweis_1"),
            t("editor_hinweis_2"),
            *([t("inline_editor")] if "{{attach:" in vorlage else []),
            "",
        ]
        if hinweis:
            kopfzeilen[4:4] = [f"<!-- {hinweis} -->", ""]
        inhalt = "\n".join(kopfzeilen) + vorlage + "\n\n"

        with tempfile.NamedTemporaryFile("w+", suffix=".md", prefix="phpbb-",
                                         delete=False, encoding="utf-8") as f:
            f.write(inhalt)
            pfad = Path(f.name)

        curses.endwin()
        editor = os.environ.get("PHPBB_TUI_EDITOR") or os.environ.get("EDITOR") or "vi"
        subprocess.call(shlex.split(editor) + ["+", str(pfad)])
        self.scr.refresh()
        curses.curs_set(0)

        roh = pfad.read_text(encoding="utf-8")
        pfad.unlink(missing_ok=True)

        import re
        roh = re.sub(r"<!--.*?-->", "", roh, flags=re.S)
        zeilen = roh.split("\n")
        neuer_betreff = betreff
        for i, z in enumerate(zeilen):
            if z.startswith("# "):
                neuer_betreff = z[2:].strip()
                zeilen = zeilen[i + 1:]
                break
        return neuer_betreff, "\n".join(zeilen).strip()

    # -- Private Nachrichten -----------------------------------------------

    def postfach(self, ordner: str = "inbox") -> None:
        start, pos, filter_text = 0, 0, ""
        nachrichten: list[Nachricht] = []
        kopf, voll, seite, seiten = "", "", 1, 1
        nav: dict[str, int] = {}
        laden = True

        while True:
            if laden:
                self.warten()
                try:
                    nachrichten, kopf, voll, seite, seiten = self.b.pm_liste(ordner, start)
                except LoginRequired:
                    if not self.anmelden():
                        return
                    continue
                except BoardError as fehler:
                    self.meldung = str(fehler)
                    nachrichten = []
                    nav = {}
                else:
                    nav = navigation(self.b, seite, seiten)
                laden = False
                pos = 0

            sichtbar = [n for n in nachrichten if not filter_text
                        or filter_text.lower() in f"{n.subject} {n.partner}".lower()]
            zeilen = []
            for n in sichtbar:
                rechts = f"{n.partner[:20]:<20} {n.date[:19]:>19}"
                platz = max(10, self.breite - len(rechts) - 6)
                zeilen.append(f" {'●' if n.unread else ' '} {n.subject[:platz]}".ljust(
                    max(0, self.breite - len(rechts) - 2)) + rechts)

            taste, pos = self.liste(
                " · ".join(x for x in (kopf, voll) if x)
                + (f"  ⟨{filter_text}⟩" if filter_text else ""),
                zeilen, t("hilfe_postfach"), tasten="/npNRfd", start=pos)

            if taste == "q":
                return
            if taste == "f":
                namen = [o for o, _ in PM_ORDNER]
                ordner = namen[(namen.index(ordner) + 1) % len(namen)]
                start, filter_text = 0, ""
                laden = True
            elif taste in ("n", "p"):
                if taste in nav:
                    start = nav[taste]
                    laden = True
                elif ((taste == "n" and seite < seiten)
                      or (taste == "p" and start > 0)):
                    self.meldung = t("navigation_fehlt")
            elif taste == "R":
                laden = True
            elif taste == "/":
                filter_text = self.frage(t("frage_filter"))
                pos = 0
            elif taste == "N":
                if self.pm_schreiben():
                    laden = True
            elif taste == "d" and pos < len(sichtbar):
                n = sichtbar[pos]
                if self.bestaetigen(t("pm_loeschen_frage", betreff=n.subject[:40])):
                    self.warten()
                    try:
                        self.b.pm_loeschen(n.mid, n.ordner_id)
                        self.meldung = t("pm_geloescht")
                    except Exception as fehler:
                        self.meldung = t("pm_fehler_loeschen", fehler=fehler)
                    laden = True
            elif taste == "\n" and pos < len(sichtbar):
                self.pm_zeigen(sichtbar[pos].mid, sichtbar[pos].ordner_id)
                laden = True

    def pm_zeigen(self, mid: int, fid: int = 0) -> None:
        while True:
            self.warten()
            try:
                n = self.b.pm_lesen(mid, fid)
            except LoginRequired:
                if not self.anmelden():
                    return
                continue
            except BoardError as fehler:
                self.meldung = str(fehler)
                return

            wahl = self.seite_zeigen(n.subject, self._pm_zeilen(n),
                                     t("hilfe_pm"), tasten="rzdao")
            if wahl == "q":
                return
            if wahl in ("r", "z"):
                self.pm_schreiben(antwort_auf=mid, fid=fid, zitat=(wahl == "z"))
            elif wahl == "a":
                self.anhaenge(n.von, n.anhaenge)
            elif wahl == "o":
                self.oeffnen(f"{self.b.base}/ucp.php?i=pm&mode=view&f={fid}&p={mid}")
            elif wahl == "d":
                if not self.bestaetigen(t("pm_loeschen_frage", betreff=n.subject[:40])):
                    continue
                try:
                    self.b.pm_loeschen(mid, fid)
                except Exception as fehler:
                    self.meldung = t("pm_fehler_loeschen", fehler=fehler)
                    continue
                self.meldung = t("pm_geloescht")
                return

    def _pm_zeilen(self, n: PM) -> list[tuple[str, int]]:
        breite = max(40, min(self.breite - 2, 100))
        zeilen: list[tuple[str, int]] = [
            (f"┌─ {t('pm_von')} {n.von}".ljust(breite)[:breite],
             curses.color_pair(C_AUTOR) | curses.A_BOLD),
            (f"│  {t('pm_an')} {n.an}", curses.color_pair(C_AUTOR)),
            (f"│  {n.date}", curses.color_pair(C_DATUM)),
            ("", 0),
        ]
        for absatz in n.body.split("\n"):
            if not absatz.strip():
                zeilen.append(("", 0))
                continue
            zitat = absatz.startswith("│")
            attr = curses.color_pair(C_ZITAT) if zitat else 0
            for teil in textwrap.wrap(absatz, breite - 2,
                                      subsequent_indent="│ " if zitat else "",
                                      drop_whitespace=True) or [""]:
                zeilen.append(("  " + teil, attr))
        for anh in n.anhaenge:
            groesse = f" ({anh.groesse})" if anh.groesse else ""
            zeilen.append((f"  📎 {anh.name}{groesse}   – {t('anhang_taste')}",
                           curses.color_pair(C_ANHANG)))
        return zeilen

    def pm_schreiben(self, antwort_auf: int = 0, fid: int = 0,
                     zitat: bool = False) -> bool:
        self.warten(t("holt_formular"))
        try:
            if antwort_auf:
                e = self.b.pm_antwort_entwurf(antwort_auf, fid, zitat)
            else:
                e = self.b.pm_neu_entwurf()
        except LoginRequired:
            return (self.pm_schreiben(antwort_auf, fid, zitat)
                    if self.anmelden() else False)
        except BoardError as fehler:
            self.meldung = str(fehler)
            return False

        # Beim Antworten steht der Empfänger schon im Formular.
        if not e.empfaenger and not self._empfaenger_fragen(e):
            return False
        return self._verfassen(e, titel=t("pm_vorschau"), erfolg=t("pm_gesendet_kurz"))

    def _empfaenger_fragen(self, e: Entwurf) -> bool:
        """Empfänger erfragen, bis eine leere Zeile kommt.

        Jeder Name geht einzeln zum Board, das ihn in eine Benutzernummer
        auflöst – ein Tippfehler fällt also sofort auf und nicht erst beim
        Absenden. Die Rückmeldung steht dann vor der nächsten Frage.
        """
        hinweis = ""
        while True:
            name = self.frage(hinweis + t("frage_empfaenger",
                                          liste=", ".join(e.empfaenger) or "—"))
            hinweis = ""
            if not name:
                if e.empfaenger:
                    return True
                self.meldung = t("pm_kein_empfaenger")
                return False
            self.warten(t("pm_pruefe", name=name))
            try:
                self.b.pm_empfaenger_hinzufuegen(e, name)
            except Exception as fehler:
                hinweis = f"{fehler} · "

    # -- Anmeldung ---------------------------------------------------------

    def anmelden(self) -> bool:
        import getpass

        curses.endwin()
        print("\n" + t("login_kopf", name=self.b.name, url=self.b.base))
        try:
            user = input(t("benutzername") + ": ").strip()
            print(t("login_hinweis"))
            pw = getpass.getpass(t("passwort"))
            self.b.login(user, pw)
            print(t("angemeldet_als", name=self.b.username or user))
        except Exception as e:
            print(t("fehlgeschlagen", fehler=e))
            input(t("weiter_mit_enter"))
            return False
        self.scr.refresh()
        curses.curs_set(0)
        return True


def starten(board: Board, cfg: Config) -> str:
    """Gibt „wechseln" zurück, wenn ein anderes Board gewünscht ist."""
    ergebnis = "ende"

    def lauf(scr):
        nonlocal ergebnis
        _farben()
        curses.curs_set(0)
        scr.keypad(True)
        try:
            Oberflaeche(scr, board, cfg).menue()
        except Wechseln:
            ergebnis = "wechseln"

    curses.wrapper(lauf)
    return ergebnis
