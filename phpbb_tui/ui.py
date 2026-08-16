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

from .board import (SUCHEN, Anhang, Board, BoardError, Forum, LoginRequired,
                    Post, Thread, Topic)
from .config import Config
from .i18n import t
from .markup import bbcode_to_md, md_to_bbcode


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
                self._zeile(1 + i, z)
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

            eintraege: list[tuple[str, object]] = [("suche", sid) for sid, _ in SUCHEN]
            zeilen = [f"  ★ {t(schl)}" for _, schl in SUCHEN] + ["  " + "─" * 40]
            eintraege.append(("trenner", None))
            for f in foren:
                if filter_text and filter_text.lower() not in f.name.lower():
                    continue
                einzug = "  " + "   " * f.ebene
                zusatz = f"   {f.beschreibung}" if f.beschreibung else ""
                zeilen.append(f"{einzug}▸ {f.name}{zusatz}")
                eintraege.append(("forum", f))

            taste, pos = self.liste(
                f"{self.b.name} · {self.b.username or t('gast')}"
                + (f"  ⟨{filter_text}⟩" if filter_text else ""),
                zeilen, t("hilfe_menue"), tasten="/swR", start=pos)

            if taste == "q":
                return
            if taste == "w":
                raise Wechseln
            if taste == "R":
                laden = True
            elif taste == "/":
                filter_text = self.frage(t("frage_foren_filter"))
                pos = 0
            elif taste == "s":
                worte = self.frage(t("frage_suche"))
                if worte:
                    self.themenliste(worte=worte)
            elif taste == "\n" and pos < len(eintraege):
                art, wert = eintraege[pos]
                if art == "suche":
                    self.themenliste(such_id=str(wert))
                elif art == "forum":
                    self.themenliste(fid=wert.fid)      # type: ignore[union-attr]

    # -- Ebene 2: Themen ---------------------------------------------------

    def themenliste(self, fid: int | None = None, such_id: str | None = None,
                    worte: str = "") -> None:
        start, pos, filter_text = 0, 0, ""
        themen: list[Topic] = []
        unterforen: list[Forum] = []
        kopf, seite, seiten = "", 1, 1
        laden = True

        while True:
            if laden:
                self.warten()
                try:
                    if fid is not None:
                        themen, unterforen, kopf, seite, seiten = self.b.topics(fid, start)
                    elif worte:
                        themen, unterforen, kopf, seite, seiten = self.b.suche(worte, start)
                    else:
                        themen, unterforen, kopf, seite, seiten = self.b.search(str(such_id), start)
                except LoginRequired:
                    if not self.anmelden():
                        return
                    continue
                except BoardError as e:
                    self.meldung = str(e)
                    themen, unterforen = [], []
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

            taste, pos = self.liste(
                f"{kopf}{f'  ⟨{filter_text}⟩' if filter_text else ''}",
                zeilen, t("hilfe_liste"), tasten="/npNR", start=pos)

            if taste == "q":
                return
            if taste == "n" and seite < seiten:
                start += 50
                laden = True
            elif taste == "p" and start > 0:
                start = max(0, start - 50)
                laden = True
            elif taste == "R":
                laden = True
            elif taste == "/":
                filter_text = self.frage(t("frage_filter"))
                pos = 0
            elif taste == "N" and fid is not None:
                if self.schreiben(mode="post", fid=fid):
                    laden = True
            elif taste == "\n" and pos < len(eintraege):
                art, wert = eintraege[pos]
                if art == "forum":
                    self.themenliste(fid=wert.fid)      # type: ignore[union-attr]
                elif art == "thema":
                    self.thread_zeigen(wert.tid)        # type: ignore[union-attr]
                    laden = True

    # -- Ebene 3: Beiträge -------------------------------------------------

    def thread_zeigen(self, tid: int) -> None:
        start, pos = 0, 0
        th: Thread | None = None
        laden = True
        zeilen: list[tuple[str, int, int]] = []

        while True:
            if laden:
                self.warten()
                try:
                    th = self.b.thread(tid, start)
                except LoginRequired:
                    if not self.anmelden():
                        return
                    continue
                except BoardError as e:
                    self.meldung = str(e)
                    return
                zeilen = self._thread_zeilen(th)
                laden = False
                pos = 0

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
                return
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
            elif k == ord("n") and th.page < th.pages:
                start += 15
                laden = True
            elif k == ord("p") and start > 0:
                start = max(0, start - 15)
                laden = True
            elif k in (ord("R"), curses.KEY_F5):
                laden = True
            elif k == ord("o"):
                self.oeffnen(f"{self.b.base}/viewtopic.php?t={tid}")
            elif k == ord("a") and th.posts:
                self.anhaenge(th.posts[akt])
            elif k == ord("r"):
                if self.schreiben(mode="reply", tid=tid):
                    start = max(0, (th.pages - 1) * 15)
                    laden = True
            elif k == ord("z") and th.posts:
                if self.schreiben(mode="quote", tid=tid, pid=th.posts[akt].pid):
                    start = max(0, (th.pages - 1) * 15)
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
                zeilen.append((f"  📎 {anh.name}{groesse}   – Taste a",
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

    def anhaenge(self, post: Post) -> None:
        if not post.anhaenge:
            self.meldung = t("keine_anhaenge")
            return
        pos = 0
        while True:
            zeilen = [f"  📎 {a.name}" + (f"  ({a.groesse})" if a.groesse else "")
                      for a in post.anhaenge]
            taste, pos = self.liste(
                t("anhaenge_von", autor=post.author), zeilen,
                t("hilfe_anhaenge"), tasten="s", start=pos)
            if taste == "q":
                return
            if taste in ("\n", "s") and post.anhaenge:
                anh = post.anhaenge[pos]
                self.warten(t("laedt_datei", name=anh.name))
                try:
                    ziel = self.b.anhang_laden(anh, self.cfg.downloads)
                except Exception as e:
                    self.meldung = t("fehler_download", fehler=e)
                    continue
                if taste == "s":
                    self.meldung = t("gespeichert", pfad=ziel)
                else:
                    self.oeffnen(str(ziel))

    # -- Schreiben ---------------------------------------------------------

    def schreiben(self, mode: str, tid: int = 0, fid: int = 0, pid: int = 0) -> bool:
        self.warten(t("holt_formular"))
        try:
            if mode == "reply":
                felder, params = self.b.reply_form(tid), {"mode": "reply", "t": tid}
            elif mode == "quote":
                felder, params = self.b.quote_form(tid, pid), {"mode": "reply", "t": tid}
            elif mode == "post":
                felder, params = self.b.new_topic_form(fid), {"mode": "post", "f": fid}
            elif mode == "edit":
                felder, params = self.b.edit_form(pid), {"mode": "edit", "p": pid}
            else:
                return False
        except LoginRequired:
            return self.schreiben(mode, tid, fid, pid) if self.anmelden() else False
        except BoardError as e:
            self.meldung = str(e)
            return False

        rest = {k: v for k, v in params.items() if k != "mode"}
        betreff = felder.get("subject", "")
        vorlage = bbcode_to_md(felder.get("message", ""))

        while True:
            betreff, text = self._editor(betreff, vorlage)
            if not text.strip():
                self.meldung = "verworfen"
                return False
            bb = md_to_bbcode(text)

            while True:      # Vorschau-Schleife (Anhänge ändern nichts am Text)
                self.warten(t("vorschau_laeuft"))
                try:
                    seite = self.b.submit(felder, betreff, bb, mode=params["mode"],
                                          preview=True, **rest)
                    vorschau = self.b.preview_text(seite)
                except Exception as e:
                    vorschau = t("keine_vorschau") + f" ({e})"

                dateien = self.b.angehaengte_dateien(felder)
                kopf = [t("betreff_zeile", betreff=betreff)]
                if dateien:
                    kopf.append(t("anhaenge_zeile", liste=", ".join(dateien)))
                zeilen = kopf + [""] + vorschau.split("\n")
                wahl = self.seite_zeigen(t("vorschau"), zeilen,
                                         t("hilfe_vorschau"), tasten="adxb")

                if wahl == "d":
                    self._anhaengen(felder, params["mode"], rest)
                    continue
                if wahl == "x":
                    if dateien:
                        self.warten(t("entfernt"))
                        try:
                            felder.clear()
                            felder.update(self.b.anhang_entfernen(
                                dict(felder), len(dateien) - 1, params["mode"], **rest))
                        except Exception as e:
                            self.meldung = t("fehler_entfernen", fehler=e)
                    continue
                break

            if wahl == "a":
                self.warten(t("sendet"))
                try:
                    seite = self.b.submit(felder, betreff, bb, mode=params["mode"], **rest)
                    ok, meldung = self.b.submit_result(seite)
                except Exception as e:
                    ok, meldung = False, str(e)
                self.meldung = meldung
                if ok:
                    return True
                if not self.bestaetigen(t("nochmal_bearbeiten", meldung=meldung)):
                    return False
                vorlage = text
            elif wahl == "b":
                vorlage = text
            else:
                self.meldung = "verworfen"
                return False

    def _anhaengen(self, felder: dict, mode: str, rest: dict) -> None:
        pfad = self.frage(t("frage_datei"), vorgabe="")
        if not pfad:
            return
        self.warten(t("laedt_hoch"))
        try:
            neu = self.b.datei_anhaengen(dict(felder), Path(pfad).expanduser(),
                                         "", mode, **rest)
            felder.clear()
            felder.update(neu)
            self.meldung = t("angehaengt")
        except Exception as e:
            self.meldung = t("fehler_anhaengen", fehler=e)

    def _editor(self, betreff: str, vorlage: str) -> tuple[str, str]:
        kopfzeilen = [
            f"# {betreff}" if betreff else "# ",
            "",
            t("editor_hinweis_1"),
            t("editor_hinweis_2"),
            "",
        ]
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
