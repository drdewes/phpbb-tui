"""Zugriff auf ein beliebiges phpBB-Forum (Version 3.x, prosilver-Abkömmlinge).

Alles, was mit dem Server spricht: Anmeldung, Seiten holen, HTML auswerten,
Beiträge schreiben, Dateien anhängen und herunterladen.

Bewusst ohne Fremdbibliotheken außer `requests`. phpBB liefert stabiles
prosilver-HTML; ausgewertet wird mit `re` und `html.parser`.
"""

from __future__ import annotations

import html
import os
import re
import time
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import requests

from .i18n import t

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) phpbb-tui/1.0"

# Übersichten, die jedes phpBB über search.php anbietet.
# Zweiter Eintrag ist der Sprachschlüssel, siehe i18n.py
SUCHEN = [
    ("unreadposts", "such_ungelesen"),
    ("newposts", "such_neu"),
    ("egosearch", "such_eigene"),
    ("active_topics", "such_aktiv"),
    ("unanswered", "such_unbeantwortet"),
]


class BoardError(RuntimeError):
    pass


class LoginRequired(BoardError):
    pass


@dataclass
class Forum:
    fid: int
    name: str
    beschreibung: str = ""
    kategorie: str = ""
    ebene: int = 0


@dataclass
class Topic:
    tid: int
    title: str
    author: str = ""
    replies: int = 0
    views: int = 0
    last_author: str = ""
    last_date: str = ""
    unread: bool = False
    sticky: bool = False
    forum: str = ""


@dataclass
class Anhang:
    aid: int
    name: str
    groesse: str = ""
    bild: bool = False


@dataclass
class Post:
    pid: int
    author: str
    date: str
    subject: str
    body: str
    own: bool = False
    anhaenge: list[Anhang] = field(default_factory=list)


@dataclass
class Thread:
    tid: int
    title: str
    forum_id: int = 0
    posts: list[Post] = field(default_factory=list)
    page: int = 1
    pages: int = 1
    start: int = 0


# --------------------------------------------------------------------------
# HTML-Werkzeug
# --------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
# Benutzernamen stehen mal als Link, mal als <span> (Gäste, gesperrte Profile)
_USER = re.compile(r'class="username[^"]*"[^>]*>([^<]+)</(?:a|span)>')


def unescape(s: str) -> str:
    return html.unescape(s).replace("\xa0", " ").strip()


def strip_tags(s: str) -> str:
    # <style>/<script> samt Inhalt entfernen: Manche Boards haben untergeschobenen
    # SEO-Spam in Forenbeschreibungen, im Browser per „display: none" versteckt.
    # Ohne diesen Schritt landet die CSS-Regel als Text in der Anzeige.
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    return unescape(_TAG.sub("", s))


def form_fields(page: str, form_id: str) -> dict[str, str]:
    """Alle Felder eines Formulars einsammeln (versteckte und vorbelegte).

    Wichtig für Anhänge: phpBB reicht hochgeladene Dateien als
    `attachment_data[0][attach_id]` usw. durch das Formular weiter – die
    müssen beim endgültigen Absenden wieder mitgeschickt werden.
    """
    m = re.search(r'<form[^>]*id="%s".*?</form>' % re.escape(form_id), page, re.S)
    if not m:
        raise BoardError(t("formular_fehlt", id=form_id))
    body = m.group(0)
    out: dict[str, str] = {}
    for tag in re.finditer(r"<input[^>]*>", body):
        roh = tag.group(0)          # nicht „t" nennen: das ist die Übersetzung
        name = re.search(r'name="([^"]*)"', roh)
        if not name:
            continue
        typ = (re.search(r'type="([^"]*)"', roh) or [None, "text"])[1]
        if typ in ("submit", "button", "file", "radio", "checkbox"):
            continue
        val = re.search(r'value="([^"]*)"', roh)
        out[unescape(name.group(1))] = unescape(val.group(1)) if val else ""
    for tag in re.finditer(r'<textarea[^>]*name="([^"]*)"[^>]*>(.*?)</textarea>', body, re.S):
        out[unescape(tag.group(1))] = unescape(tag.group(2))
    return out


# --------------------------------------------------------------------------
# Sitzung
# --------------------------------------------------------------------------


class Board:
    """Ein Forum. `base` ist die Adresse bis einschließlich Forenverzeichnis,
    z. B. https://www.example.org/forum"""

    def __init__(self, base: str, cookie_path: Path, name: str = ""):
        self.base = base.rstrip("/")
        self.name = name or self.base
        self.cookie_path = Path(cookie_path)
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.jar = MozillaCookieJar(str(self.cookie_path))
        if self.cookie_path.exists():
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        self.s = requests.Session()
        self.s.cookies = self.jar            # type: ignore[assignment]
        self.s.headers["User-Agent"] = USER_AGENT
        self.username = ""

    # -- unterste Ebene ----------------------------------------------------

    def _save(self) -> None:
        self.jar.save(ignore_discard=True, ignore_expires=True)
        try:
            os.chmod(self.cookie_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _formular_reifen(data: dict) -> None:
        """phpBB weist ein Formular ab, das in derselben Sekunde zurückkommt.

        In `check_form_key` steht `$diff = time() - $creation_time;` und danach
        `if ($diff && …)` – bei 0 Sekunden ist das falsch, und die Meldung
        lautet „Das übermittelte Formular war ungültig". Ein Mensch braucht
        immer länger, ein Programm nicht. Also kurz warten.
        """
        try:
            ct = int(data.get("creation_time", 0))
        except (TypeError, ValueError):
            return
        if not ct:
            return
        rest = (ct + 2) - time.time()
        if 0 < rest <= 3:
            time.sleep(rest)

    def get(self, path: str, **params) -> str:
        r = self.s.get(f"{self.base}/{path}", params=params or None, timeout=30)
        r.raise_for_status()
        self._save()
        return r.text

    def post(self, path: str, data: dict, files: dict | None = None, **params) -> str:
        self._formular_reifen(data)
        r = self.s.post(f"{self.base}/{path}", params=params or None,
                        data=data, files=files, timeout=120)
        r.raise_for_status()
        self._save()
        return r.text

    # -- Anmeldung ---------------------------------------------------------

    def logged_in(self, page: str | None = None) -> bool:
        page = self.get("index.php") if page is None else page
        if "mode=logout" not in page:
            return False
        namen = _USER.findall(page.split("mode=logout")[0][-4000:])
        if namen:
            self.username = unescape(namen[-1])
        return True

    def login(self, user: str, password: str) -> None:
        page = self.get("ucp.php", mode="login")
        fields = form_fields(page, "login")
        fields.update({
            "username": user,
            "password": password,
            "autologin": "on",      # Sitzung hält lange (Board-Einstellung)
            "viewonline": "on",
            "login": "Anmelden",
        })
        res = self.post("ucp.php", fields, mode="login")
        if not self.logged_in(res):
            msg = re.search(r'class="error"[^>]*>(.*?)</', res, re.S)
            raise LoginRequired(strip_tags(msg.group(1)) if msg
                                else t("anmeldung_fehlgeschlagen"))

    def require_login(self, page: str) -> None:
        if "ucp.php?mode=login" in page and re.search(r"<title>[^<]*(Anmelden|Login)",
                                                      page, re.I):
            raise LoginRequired(t("sitzung_abgelaufen"))

    # -- Forenübersicht ----------------------------------------------------

    def foren(self) -> list[Forum]:
        """Der Forenbaum.

        Erste Quelle ist das „Gehe zu"-Sprungfeld: Es führt **alle** Foren auf,
        die der angemeldete Nutzer sehen darf. Manche Boards zeigen auf der
        Startseite nur einen Teil davon – im Extremfall ein einziges öffentliches
        Forum, während die Mitgliederforen dort gar nicht auftauchen.
        """
        page = self.get("index.php")
        self.require_login(page)

        sprung = self._parse_jumpbox(page)
        if sprung:
            return sprung

        # Kein Sprungfeld auf der Startseite: Dort stehen oft nur die obersten
        # Ebenen – die Foren mit den Themen liegen eine Stufe tiefer. Also die
        # erste Forenseite holen, deren Sprungfeld den ganzen Baum enthält.
        oben = self._parse_foren(page)
        for f in oben:
            mehr = self._parse_jumpbox(self.get("viewforum.php", f=f.fid))
            if len(mehr) > len(oben):
                return mehr
        return oben

    @staticmethod
    def _parse_jumpbox(page: str) -> list[Forum]:
        """Foren aus dem Sprungfeld, in der Reihenfolge des Boards.

        Kategorien stehen dort als reine Beschriftung ohne Link; sie liefern
        die Gruppierung für die darunter aufgeführten Foren.
        """
        out: list[Forum] = []
        kategorie = ""
        muster = re.compile(
            r'<a href="[^"]*viewforum\.php\?f=(\d+)[^"]*"\s+'
            r'class="jumpbox-(cat|forum|sub)-link"[^>]*>(.*?)</a>', re.S)
        for m in muster.finditer(page):
            fid, art, innen = int(m.group(1)), m.group(2), m.group(3)
            ebene = innen.count('class="spacer"')
            name = strip_tags(innen).lstrip("↳").strip()
            if art == "cat":
                kategorie = name
            out.append(Forum(fid, name, "", "" if art == "cat" else kategorie, ebene))
        return out

    @staticmethod
    def _parse_foren(page: str) -> list[Forum]:
        out: list[Forum] = []
        for block in re.split(r'(?=<div class="forabg")', page):
            if "forumtitle" not in block and "forumtitle" not in block:
                continue
            kat = re.search(r'<h2[^>]*>\s*(?:<a[^>]*>)?(.*?)(?:</a>)?\s*</h2>', block, re.S)
            kategorie = strip_tags(kat.group(1)) if kat else ""
            for zeile in re.split(r'(?=<li class="row)', block):
                m = re.search(r'href="[^"]*viewforum\.php\?f=(\d+)[^"]*"[^>]*'
                              r'class="forumtitle"[^>]*>(.*?)</a>', zeile, re.S)
                if not m:
                    continue
                # Beschreibung: nur der Text direkt nach dem Forumnamen, bis zum
                # nächsten Element. Sonst geraten der Mobil-Block („Themen: 5")
                # und untergeschobener Spam mit in die Zeile.
                beschr = re.search(r'<br\s*/?>\s*(.*?)(?=<div|<dd|</div>)', zeile, re.S)
                out.append(Forum(int(m.group(1)), strip_tags(m.group(2)),
                                 " ".join(strip_tags(beschr.group(1)).split())[:70]
                                 if beschr else "",
                                 kategorie))
                for sub in re.finditer(r'href="[^"]*viewforum\.php\?f=(\d+)[^"]*"[^>]*'
                                       r'class="subforum[^"]*"[^>]*>(.*?)</a>', zeile, re.S):
                    out.append(Forum(int(sub.group(1)), strip_tags(sub.group(2)),
                                     "", kategorie, ebene=1))
        return out

    @staticmethod
    def _parse_unterforen(page: str) -> list[Forum]:
        """Unterforen, die auf einer Forenseite stehen (Kategorie-Foren)."""
        out: list[Forum] = []
        gesehen = set()
        for zeile in re.split(r'(?=<li class="row)', page):
            m = re.search(r'href="[^"]*viewforum\.php\?f=(\d+)[^"]*"[^>]*'
                          r'class="forumtitle"[^>]*>(.*?)</a>', zeile, re.S)
            if m and int(m.group(1)) not in gesehen:
                gesehen.add(int(m.group(1)))
                out.append(Forum(int(m.group(1)), strip_tags(m.group(2))))
        return out

    # -- Themenlisten ------------------------------------------------------

    def topics(self, fid: int, start: int = 0) -> tuple[list[Topic], list[Forum], str, int, int]:
        page = self.get("viewforum.php", f=fid, start=start)
        self.require_login(page)
        themen, titel, seite, seiten = self._parse_topics(page)
        return themen, self._parse_unterforen(page), titel, seite, seiten

    def search(self, search_id: str, start: int = 0) -> tuple[list[Topic], list[Forum], str, int, int]:
        page = self.get("search.php", search_id=search_id, start=start)
        self.require_login(page)
        themen, titel, seite, seiten = self._parse_topics(page)
        return themen, [], titel, seite, seiten

    def suche(self, worte: str, start: int = 0) -> tuple[list[Topic], list[Forum], str, int, int]:
        page = self.get("search.php", keywords=worte, start=start)
        self.require_login(page)
        themen, titel, seite, seiten = self._parse_topics(page)
        return themen, [], f"Suche: {worte}", seite, seiten

    def _parse_topics(self, page: str) -> tuple[list[Topic], str, int, int]:
        # Überschrift der Seite bevorzugen; der <title> trägt zusätzlich den
        # Boardnamen, und je nach Board steht der vorn oder hinten.
        kopf = (re.search(r'<h2 class="(?:forum|searchresults)-title"[^>]*>'
                          r'(?:<a[^>]*>)?(.*?)(?:</a>)?</h2>', page, re.S)
                or re.search(r"<title>(.*?)</title>", page, re.S))
        title = strip_tags(kopf.group(1)) if kopf else ""
        topics: list[Topic] = []
        # Nicht bis zum nächsten </li> schneiden: mehrseitige Themen haben eine
        # eigene Seitenliste (<li>) in der Zeile stehen.
        for r in re.split(r'(?=<li class="row)', page):
            link = re.search(r'<a href="[^"]*?viewtopic\.php\?[^"]*?t=(\d+)[^"]*"[^>]*'
                             r'class="topictitle"[^>]*>(.*?)</a>', r, re.S)
            if not link:
                continue
            thema = Topic(tid=int(link.group(1)), title=strip_tags(link.group(2)))
            thema.unread = "topic_unread" in r
            thema.sticky = "sticky" in r or "announce" in r
            poster = re.search(r'class="topic-poster[^"]*"[^>]*>(.*?)</div>', r, re.S)
            a = _USER.search(poster.group(1) if poster else r)
            if a:
                thema.author = unescape(a.group(1))
            reps = re.search(r'class="posts"[^>]*>\s*(\d+)', r)
            if reps:
                thema.replies = int(reps.group(1))
            views = re.search(r'class="views"[^>]*>\s*(\d+)', r)
            if views:
                thema.views = int(views.group(1))
            last = re.search(r'class="lastpost"(.*?)</dd>', r, re.S)
            if last:
                la = _USER.findall(last.group(1))
                if la:
                    thema.last_author = unescape(la[-1])
                ld = re.findall(r"<time[^>]*>([^<]+)</time>", last.group(1))
                if ld:
                    thema.last_date = unescape(ld[-1])
            topics.append(thema)
        page_no, pages = self._pagination(page)
        return topics, title, page_no, pages

    @staticmethod
    def _pagination(page: str) -> tuple[int, int]:
        m = re.search(r"(?:Seite|Page)\s*<strong>(\d+)</strong>\s*(?:von|of)\s*"
                      r"<strong>(\d+)</strong>", page)
        if m:
            return int(m.group(1)), int(m.group(2))
        m = re.search(r"(?:Seite|Page) (\d+) (?:von|of) (\d+)", strip_tags(page))
        return (int(m.group(1)), int(m.group(2))) if m else (1, 1)

    # -- Thread ------------------------------------------------------------

    def thread(self, tid: int, start: int = 0) -> Thread:
        from .markup import html_to_text

        page = self.get("viewtopic.php", t=tid, start=start)
        self.require_login(page)
        title = strip_tags((re.search(r'<h2 class="topic-title"[^>]*>(.*?)</h2>', page, re.S)
                            or re.search(r"<title>(.*?)</title>", page, re.S)
                            or [None, ""])[1])
        fid = int((re.search(r'viewforum\.php\?f=(\d+)', page) or [0, 0])[1])
        th = Thread(tid=tid, title=title, forum_id=fid, start=start)
        for m in re.finditer(r'<div id="p(\d+)" class="post[^"]*">(.*?)(?=<div id="p\d+" '
                             r'class="post|<div class="action-bar bar-bottom|$)', page, re.S):
            pid, blk = int(m.group(1)), m.group(2)
            author = _USER.search(blk)
            date = re.search(r"<time[^>]*>([^<]+)</time>", blk)
            subj = re.search(r'<h3[^>]*>(?:<a[^>]*>)?(.*?)(?:</a>)?</h3>', blk, re.S)
            content = re.search(r'<div class="content">(.*?)</div>\s*(?:<dl class="attach|'
                                r'<div id="sig|</div>)', blk, re.S)
            th.posts.append(Post(
                pid=pid,
                author=unescape(author.group(1)) if author else "?",
                date=unescape(date.group(1)) if date else "",
                subject=strip_tags(subj.group(1)) if subj else "",
                body=html_to_text(content.group(1)) if content else "",
                own=bool(author and unescape(author.group(1)) == self.username),
                anhaenge=self._parse_anhaenge(blk),
            ))
        th.page, th.pages = self._pagination(page)
        return th

    @staticmethod
    def _parse_anhaenge(block: str) -> list[Anhang]:
        out: list[Anhang] = []
        gesehen: set[int] = set()
        for m in re.finditer(r'<dl class="(file|thumbnail|attachbox)".*?(?=<dl class="|</dl>\s*</dd>|$)',
                             block, re.S):
            teil = m.group(0)
            link = re.search(r'download/file\.php\?id=(\d+)', teil)
            if not link:
                continue
            aid = int(link.group(1))
            if aid in gesehen:
                continue
            gesehen.add(aid)
            name = re.search(r'download/file\.php\?id=\d+[^"]*"[^>]*>(.*?)</a>', teil, re.S)
            alt = re.search(r'alt="([^"]+)"', teil)
            groesse = re.search(r"\(([\d.,]+\s*[KMG]?i?B)\)", strip_tags(teil))
            out.append(Anhang(
                aid=aid,
                name=strip_tags(name.group(1)) if name else (alt.group(1) if alt else f"Datei {aid}"),
                groesse=groesse.group(1) if groesse else "",
                bild="thumbnail" in teil or "image" in teil,
            ))
        return out

    def anhang_laden(self, anhang: Anhang, ziel_ordner: Path) -> Path:
        """Anhang herunterladen und den Pfad zurückgeben."""
        ziel_ordner.mkdir(parents=True, exist_ok=True)
        r = self.s.get(f"{self.base}/download/file.php", params={"id": anhang.aid},
                       timeout=120, stream=True)
        r.raise_for_status()
        name = anhang.name or f"anhang-{anhang.aid}"
        # Dateiname aus dem Kopf bevorzugen, er ist verlässlicher
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
        if m:
            name = unescape(m.group(1))
        name = re.sub(r"[/\\]", "_", name).strip() or f"anhang-{anhang.aid}"
        ziel = ziel_ordner / name
        with open(ziel, "wb") as f:
            for stueck in r.iter_content(65536):
                f.write(stueck)
        return ziel

    # -- Schreiben ---------------------------------------------------------

    def _posting_form(self, **params) -> dict[str, str]:
        page = self.get("posting.php", **params)
        self.require_login(page)
        if "postform" not in page:
            err = re.search(r'<div class="[^"]*message[^"]*">(.*?)</div>', page, re.S)
            raise BoardError(strip_tags(err.group(1)) if err
                             else t("kein_formular"))
        return form_fields(page, "postform")

    def reply_form(self, tid: int) -> dict[str, str]:
        return self._posting_form(mode="reply", t=tid)

    def quote_form(self, tid: int, pid: int) -> dict[str, str]:
        return self._posting_form(mode="quote", t=tid, p=pid)

    def new_topic_form(self, fid: int) -> dict[str, str]:
        return self._posting_form(mode="post", f=fid)

    def edit_form(self, pid: int) -> dict[str, str]:
        return self._posting_form(mode="edit", p=pid)

    def datei_anhaengen(self, fields: dict[str, str], pfad: Path, kommentar: str,
                        mode: str, **params) -> dict[str, str]:
        """Datei ins offene Formular hochladen.

        phpBB nimmt den Upload für sich entgegen („Datei hinzufügen") und gibt
        das Formular mit zusätzlichen Feldern `attachment_data[n][…]` zurück.
        Genau diese Felder müssen später beim Absenden mitgehen – deshalb wird
        das Formular hier komplett neu eingelesen.
        """
        pfad = Path(pfad).expanduser()
        if not pfad.is_file():
            raise BoardError(t("datei_fehlt", pfad=pfad))
        data = dict(fields)
        data["filecomment"] = kommentar
        data["add_file"] = "Datei hinzufügen"
        with open(pfad, "rb") as f:
            seite = self.post("posting.php", data,
                              files={"fileupload": (pfad.name, f)}, mode=mode, **params)
        fehler = self._fehlertext(seite)
        if fehler:
            raise BoardError(fehler)
        if "postform" not in seite:
            raise BoardError(t("upload_unerwartet"))
        return form_fields(seite, "postform")

    @staticmethod
    def angehaengte_dateien(fields: dict[str, str]) -> list[str]:
        """Namen der Dateien, die im Formular hängen."""
        return [v for k, v in fields.items()
                if re.match(r"attachment_data\[\d+\]\[real_filename\]", k) and v]

    def anhang_entfernen(self, fields: dict[str, str], nummer: int,
                         mode: str, **params) -> dict[str, str]:
        data = dict(fields)
        data[f"delete_file[{nummer}]"] = "Datei löschen"
        seite = self.post("posting.php", data, mode=mode, **params)
        return form_fields(seite, "postform") if "postform" in seite else fields

    def submit(self, fields: dict[str, str], subject: str, message: str,
               mode: str, preview: bool = False, **params) -> str:
        data = dict(fields)
        data["subject"] = subject
        data["message"] = message
        data["preview" if preview else "post"] = "Vorschau" if preview else "Absenden"
        data.setdefault("creation_time", str(int(time.time()) - 2))
        return self.post("posting.php", data, mode=mode, **params)

    # -- Antwort auswerten -------------------------------------------------

    @staticmethod
    def _fehlertext(page: str) -> str:
        for m in re.finditer(r'<(?:p|div|span|h2)[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)'
                             r'</(?:p|div|span|h2)>', page, re.S):
            txt = re.split(r"\s{2,}", strip_tags(m.group(1)))[0].strip()
            if len(txt) > 5:
                return txt
        return ""

    def submit_result(self, page: str) -> tuple[bool, str]:
        """(erfolgreich, Meldung). Erst positiv prüfen: ein bloßes
        `class="error"` steht auch im Gerüst von Erfolgsseiten."""
        if re.search(r"wurde erfolgreich|successfully (?:posted|submitted|edited)",
                     page, re.I):
            return True, t("beitrag_da")
        weiter = re.search(r'<meta http-equiv="refresh"[^>]*viewtopic\.php[^"]*#p(\d+)', page)
        if weiter:
            return True, t("beitrag_da_nr", pid=weiter.group(1))
        fehler = self._fehlertext(page)
        if fehler:
            return False, fehler
        if "postform" in page:
            return False, t("formular_erneut", datei=self.dump(page).name)
        return False, t("antwort_unklar", datei=self.dump(page).name)

    def preview_text(self, page: str) -> str:
        from .markup import html_to_text

        m = re.search(r'<div class="content">(.*?)</div>\s*</div>', page, re.S)
        return html_to_text(m.group(1)) if m else t("keine_vorschau")

    def dump(self, page: str) -> Path:
        pfad = self.cookie_path.parent / "last-response.html"
        pfad.write_text(page, encoding="utf-8")
        return pfad
