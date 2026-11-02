"""Zugriff auf ein beliebiges phpBB-Forum (Version 3.x, prosilver-Abkömmlinge).

Alles, was mit dem Server spricht: Anmeldung, Seiten holen, HTML auswerten,
Beiträge schreiben, Dateien anhängen und herunterladen.

Bewusst ohne Fremdbibliotheken außer `requests`. phpBB liefert stabiles
prosilver-HTML; ausgewertet wird mit `re` und `html.parser`.
"""

from __future__ import annotations

import errno
import html
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from email.message import Message
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from . import netz
from .i18n import t

from . import __version__

USER_AGENT = f"Mozilla/5.0 (X11; Linux x86_64) phpbb-tui/{__version__}"

# Übersichten, die jedes phpBB über search.php anbietet.
# Zweiter Eintrag ist der Sprachschlüssel, siehe i18n.py
SUCHEN = [
    ("unreadposts", "such_ungelesen"),
    ("newposts", "such_neu"),
    ("egosearch", "such_eigene"),
    ("active_topics", "such_aktiv"),
    ("unanswered", "such_unbeantwortet"),
]

# Listen aus dem persönlichen Bereich (ucp.php?i=ucp_main&mode=…).
UCP_LISTEN = [
    ("subscribed", "ucp_abos"),
    ("bookmarks", "ucp_lesezeichen"),
]

# Die drei Postfächer, die phpBB immer anlegt. „outbox" sind Nachrichten, die
# der Empfänger noch nicht gelesen hat – solange stehen sie dort und nicht in
# „sentbox". Eigene Ordner des Nutzers kennt dieser Client nicht.
PM_ORDNER = [
    ("inbox", "pm_posteingang"),
    ("sentbox", "pm_gesendet"),
    ("outbox", "pm_postausgang"),
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
    unread: bool = False


@dataclass
class Thread:
    tid: int
    title: str
    forum_id: int = 0
    posts: list[Post] = field(default_factory=list)
    page: int = 1
    pages: int = 1
    start: int = 0
    erster_neuer: int | None = None   # Index des ersten ungelesenen Beitrags


@dataclass
class Nachricht:
    """Eine Zeile im Postfach."""
    mid: int
    subject: str
    partner: str = ""          # Absender im Eingang, Empfänger im Ausgang
    date: str = ""
    unread: bool = False
    ordner_id: int = 0         # phpBBs „f": 0 = Eingang, -1 = Gesendet, -2 = Ausgang


@dataclass
class PM:
    """Eine geöffnete private Nachricht."""
    mid: int
    subject: str
    von: str = ""
    an: str = ""
    date: str = ""
    body: str = ""
    ordner_id: int = 0
    anhaenge: list[Anhang] = field(default_factory=list)


@dataclass
class Entwurf:
    """Ein offenes Formular: Beitrag oder private Nachricht.

    `ziel` und `params` sagen, wohin das Fertige geht – `posting.php` für
    Beiträge, `ucp.php?i=pm&mode=compose` für Nachrichten. Alles danach ist
    für beide gleich, deshalb kennt die Oberfläche nur noch diesen Entwurf.
    """
    felder: dict[str, str]
    ziel: str
    params: dict
    anhaenge_moeglich: bool = True
    empfaenger: list[str] = field(default_factory=list)


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
        netz.einrichten()
        self.s = requests.Session()
        self.s.cookies = self.jar            # type: ignore[assignment]
        self.s.headers["User-Agent"] = USER_AGENT
        self.username = ""
        self.pm_neu = 0          # ungelesene private Nachrichten
        self.last_post_url = ""

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

    @staticmethod
    def _netzfehler(fehler: requests.RequestException) -> BoardError:
        """Netzfehler in eine verständliche Meldung übersetzen.

        Cloudflare stellt manchen Clients statt der Seite eine
        JavaScript-Abfrage („challenge") und kennzeichnet sie mit dem Header
        `cf-mitigated: challenge`. Ein Programm ohne Browser kommt daran nicht
        vorbei; ein nacktes „403 Forbidden" erklärt das aber niemandem.
        """
        antwort = getattr(fehler, "response", None)
        kopf = getattr(antwort, "headers", None) or {}
        if str(kopf.get("cf-mitigated", "")).lower() == "challenge":
            return BoardError(t("cloudflare_abfrage"))
        return BoardError(t("netz_fehler", fehler=fehler))

    def _nachfassen(self, path: str, params: dict | None):
        """Einmal nachfassen, wenn die Verbindung abreißt.

        Boards schließen ruhende Verbindungen; die nächste Anfrage läuft dann
        in einen abgebrochenen Kanal. Beim Lesen darf man das gefahrlos
        wiederholen – beim Senden nicht, das gäbe womöglich zwei Beiträge.
        """
        try:
            return self.s.get(f"{self.base}/{path}", params=params, timeout=30)
        except requests.ConnectionError:
            self.s.close()
            return self.s.get(f"{self.base}/{path}", params=params, timeout=30)

    def get(self, path: str, **params) -> str:
        self.last_post_url = ""
        # Netzfehler kommen als BoardError heraus, damit die Oberfläche sie
        # wie jeden anderen Fehler in der Fußzeile zeigt, statt abzustürzen.
        try:
            r = self._nachfassen(path, params or None)
            r.raise_for_status()
        except requests.RequestException as fehler:
            raise self._netzfehler(fehler) from fehler
        self._save()
        self.page_html = r.text
        self.page_path = path
        self.page_params = params
        return r.text

    def post(self, path: str, data: dict, files: dict | None = None, **params) -> str:
        self.last_post_url = ""
        self._formular_reifen(data)
        try:
            r = self.s.post(f"{self.base}/{path}", params=params or None,
                            data=data, files=files, timeout=120)
            r.raise_for_status()
        except requests.RequestException as fehler:
            raise self._netzfehler(fehler) from fehler
        if r.history and isinstance(r.url, str):
            self.last_post_url = r.url
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
        self.pm_neu = self._pm_badge(page)

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

    def ucp_themen(self, modus: str, start: int = 0) -> tuple[list[Topic], list[Forum], str, int, int]:
        """Abonnierte Themen oder Lesezeichen aus dem persönlichen Bereich.

        Die Zeilen sind wie in jeder Themenliste gebaut; abonnierte Foren
        stehen als `forumtitle`-Zeilen davor und kommen als Unterforen zurück.
        """
        page = self.get("ucp.php", i="ucp_main", mode=modus, start=start)
        self.require_login(page)
        themen, _, seite, seiten = self._parse_topics(page)
        schluessel = dict(UCP_LISTEN).get(modus, modus)
        return themen, self._parse_unterforen(page), t(schluessel), seite, seiten

    @staticmethod
    def _gelesen_link(page: str, art: str) -> str:
        """Den Link „Foren/Themen als gelesen markieren“ der Seite.

        Nur Links mit `hash=` zählen: Den Schlüssel legt phpBB pro Sitzung
        fest, ohne ihn käme statt der Markierung eine Rückfrage.
        """
        for href in re.findall(r'href="([^"]*)"', page):
            href = unescape(href)
            if f"mark={art}" in href and "hash=" in href:
                return href[2:] if href.startswith("./") else href
        return ""

    def als_gelesen_markieren(self, fid: int | None = None) -> None:
        """Alle Foren (ohne `fid`) oder alle Themen eines Forums als gelesen markieren.

        Der Link wird jedes Mal frisch geholt, weil `hash` und `mark_time`
        zur Seite gehören, von der aus markiert wird.
        """
        if fid is None:
            seite, art = self.get("index.php"), "forums"
        else:
            seite, art = self.get("viewforum.php", f=fid), "topics"
        self.require_login(seite)
        link = self._gelesen_link(seite, art)
        if not link:
            raise BoardError(t("gelesen_nicht_moeglich"))
        pfad, _, abfrage = link.partition("?")
        params = {k: v[0] for k, v in parse_qs(abfrage).items()}
        self.require_login(self.get(pfad, **params))

    def search(self, search_id: str, start: int = 0) -> tuple[list[Topic], list[Forum], str, int, int]:
        page = self.get("search.php", search_id=search_id, start=start)
        self.require_login(page)
        themen, titel, seite, seiten = self._parse_topics(page)
        return themen, [], titel, seite, seiten

    def suche(self, worte: str, start: int = 0) -> tuple[list[Topic], list[Forum], str, int, int]:
        page = self.get("search.php", keywords=worte, sr="topics", start=start)
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
        if m:
            return int(m.group(1)), int(m.group(2))
        # Manche Styles (z. B. damaïo) schreiben kein „Seite x von y“, nur die
        # Seitenleiste. Die der ganzen Seite hat einen aktiven Eintrag; die
        # Mini-Leisten in Themenzeilen haben keinen und zählen nicht.
        for leiste in re.findall(r'<div class="pagination">(.*?)</ul>', page, re.S):
            aktiv = re.search(r'<li class="active"><span>(\d+)</span>', leiste)
            if aktiv:
                zahlen = [int(z) for z in re.findall(r'>\s*(\d+)\s*</(?:a|span)>', leiste)]
                return int(aktiv.group(1)), max(zahlen + [int(aktiv.group(1))])
        return 1, 1

    # -- Thread ------------------------------------------------------------

    def thread(self, tid: int, start: int = 0, ungelesen: bool = False) -> Thread:
        """Eine Seite eines Themas; mit `ungelesen` die mit dem ersten neuen Beitrag.

        phpBB springt bei `view=unread` selbst auf die richtige Seite und setzt
        davor einen Anker `id="unread"`. Welche Seite das ist, verrät nur der
        Canonical-Link; den braucht das Blättern als Ausgangspunkt.
        """
        from .markup import html_to_text

        if ungelesen:
            page = self.get("viewtopic.php", t=tid, view="unread")
            canon = re.search(r'<link rel="canonical" href="[^"]*?[?&](?:amp;)?start=(\d+)', page)
            start = int(canon.group(1)) if canon else 0
            self.page_params = {"t": tid, "start": start}
        else:
            page = self.get("viewtopic.php", t=tid, start=start)
        self.require_login(page)
        anker = re.search(r'id="unread"[^>]*>\s*(?:</a>)?\s*<div id="p(\d+)"', page)
        erster_neuer_pid = int(anker.group(1)) if anker else 0
        title = strip_tags((re.search(r'<h2 class="topic-title"[^>]*>(.*?)</h2>', page, re.S)
                            or re.search(r"<title>(.*?)</title>", page, re.S)
                            or [None, ""])[1])
        fid = int((re.search(r'viewforum\.php\?f=(\d+)', page) or [0, 0])[1])
        # Breadcrumbs run from the parent category to the actual forum.
        # Ignore forum links elsewhere, retaining the legacy fallback when
        # a style does not expose forum breadcrumbs.
        for crumb in re.finditer(r'<li\b[^>]*class="([^"]*)"[^>]*>(.*?)</li>',
                                 page, re.S):
            if "breadcrumbs" not in crumb[1].split():
                continue
            forums = re.findall(
                r'viewforum\.php\?(?:[^"#]*&(?:amp;)?)?f=([0-9]+)(?=[&#"])',
                crumb[2])
            if forums:
                fid = int(forums[-1])
        th = Thread(tid=tid, title=title, forum_id=fid, start=start)
        for m in re.finditer(r'<div id="p(\d+)" class="(post[^"]*)">(.*?)(?=<div id="p\d+" '
                             r'class="post|<div class="action-bar bar-bottom|$)', page, re.S):
            pid, klassen, blk = int(m.group(1)), m.group(2).split(), m.group(3)
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
                unread="unreadpost" in klassen or pid == erster_neuer_pid,
            ))
            if th.erster_neuer is None and (pid == erster_neuer_pid or (
                    ungelesen and "unreadpost" in klassen)):
                th.erster_neuer = len(th.posts) - 1
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
            alt = re.search(r'alt="([^"]+)"',
                            name.group(1) if name else teil)
            filename = strip_tags(name.group(1)) if name else ""
            if not filename and alt:
                filename = unescape(alt.group(1))
            groesse = re.search(r"\(([\d.,]+\s*[KMG]?i?B)\)", strip_tags(teil))
            out.append(Anhang(
                aid=aid,
                name=filename or f"Datei {aid}",
                groesse=groesse.group(1) if groesse else "",
                bild="thumbnail" in teil or "image" in teil,
            ))
        return out

    def anhang_laden(self, anhang: Anhang, ziel_ordner: Path, *,
                     target: Path | None = None, overwrite: bool = True) -> Path:
        """Anhang herunterladen und den Pfad zurückgeben."""
        ziel_ordner = Path(target).parent if target is not None else ziel_ordner
        ziel_ordner.mkdir(parents=True, exist_ok=True)
        r = self.s.get(f"{self.base}/download/file.php", params={"id": anhang.aid},
                       timeout=120, stream=True)
        temporary = None
        try:
            r.raise_for_status()
            header = Message()
            header["Content-Disposition"] = r.headers.get(
                "Content-Disposition", "")
            filename = header.get_filename()
            content_type = r.headers.get("Content-Type", "").split(";")[0].lower()
            if (not filename and content_type
                    in ("text/html", "application/xhtml+xml")):
                raise BoardError("HTML page returned instead of an attachment")
            name = filename or anhang.name or f"anhang-{anhang.aid}"
            name = re.sub(r"[/\\\x00-\x1f\x7f]", "_", name).strip()
            if name in ("", ".", ".."):
                name = f"anhang-{anhang.aid}"
            ziel = Path(target) if target is not None else ziel_ordner / name
            if not overwrite and os.path.lexists(ziel):
                raise FileExistsError(
                    errno.EEXIST, "Attachment destination already exists",
                    str(ziel))
            if ziel.is_symlink() or (ziel.exists() and not ziel.is_file()):
                raise BoardError("Attachment destination is not a regular file")
            fd, temporary = tempfile.mkstemp(prefix=".phpbb-tui-", dir=ziel_ordner)
            with os.fdopen(fd, "wb") as f:
                for stueck in r.iter_content(65536):
                    f.write(stueck)
            if ziel.is_symlink() or (ziel.exists() and not ziel.is_file()):
                raise BoardError("Attachment destination is not a regular file")
            if overwrite:
                os.replace(temporary, ziel)
            else:
                # Publishing by hard link fails atomically if a file appeared.
                try:
                    os.link(temporary, ziel)
                except FileExistsError as cause:
                    raise FileExistsError(
                        errno.EEXIST, "Attachment destination already exists",
                        str(ziel)) from cause
            return ziel
        finally:
            try:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
            finally:
                r.close()

    # -- Schreiben ---------------------------------------------------------

    def _posting_form(self, sende_params: dict, **hol_params) -> Entwurf:
        """Formular holen. `hol_params` steht in der Adresse, `sende_params`
        geht später beim Absenden mit – beim Zitieren ist das nicht dasselbe:
        geholt wird mit `mode=quote`, abgeschickt mit `mode=reply`."""
        page = self.get("posting.php", **hol_params)
        self.require_login(page)
        if "postform" not in page:
            err = re.search(r'<div class="[^"]*message[^"]*">(.*?)</div>', page, re.S)
            raise BoardError(strip_tags(err.group(1)) if err
                             else t("kein_formular"))
        return Entwurf(form_fields(page, "postform"), "posting.php", sende_params,
                       anhaenge_moeglich='name="fileupload"' in page)

    def reply_form(self, tid: int) -> Entwurf:
        return self._posting_form(dict(mode="reply", t=tid), mode="reply", t=tid)

    def quote_form(self, tid: int, pid: int) -> Entwurf:
        return self._posting_form(dict(mode="reply", t=tid), mode="quote", t=tid, p=pid)

    def new_topic_form(self, fid: int) -> Entwurf:
        return self._posting_form(dict(mode="post", f=fid), mode="post", f=fid)

    def edit_form(self, pid: int) -> Entwurf:
        return self._posting_form(dict(mode="edit", p=pid), mode="edit", p=pid)

    def datei_anhaengen(self, e: Entwurf, pfad: Path, kommentar: str = "") -> None:
        """Datei ins offene Formular hochladen.

        phpBB nimmt den Upload für sich entgegen („Datei hinzufügen") und gibt
        das Formular mit zusätzlichen Feldern `attachment_data[n][…]` zurück.
        Genau diese Felder müssen später beim Absenden mitgehen – deshalb wird
        das Formular hier komplett neu eingelesen.
        """
        pfad = Path(pfad).expanduser()
        if not pfad.is_file():
            raise BoardError(t("datei_fehlt", pfad=pfad))
        data = dict(e.felder)
        data["filecomment"] = kommentar
        data["add_file"] = "Datei hinzufügen"
        with open(pfad, "rb") as f:
            seite = self.post(e.ziel, data, files={"fileupload": (pfad.name, f)},
                              **e.params)
        fehler = self._fehlertext(seite)
        if fehler:
            raise BoardError(fehler)
        if "postform" not in seite:
            raise BoardError(t("upload_unerwartet"))
        e.felder = form_fields(seite, "postform")

    @staticmethod
    def angehaengte_dateien(e: Entwurf) -> list[str]:
        """Namen der Dateien, die im Formular hängen."""
        return [v for k, v in e.felder.items()
                if re.match(r"attachment_data\[\d+\]\[real_filename\]", k) and v]

    def anhang_entfernen(self, e: Entwurf, nummer: int) -> None:
        data = dict(e.felder)
        data[f"delete_file[{nummer}]"] = "Datei löschen"
        seite = self.post(e.ziel, data, **e.params)
        if "postform" in seite:
            e.felder = form_fields(seite, "postform")

    def submit(self, e: Entwurf, subject: str, message: str,
               preview: bool = False) -> str:
        data = dict(e.felder)
        data["subject"] = subject
        data["message"] = message
        data["preview" if preview else "post"] = "Vorschau" if preview else "Absenden"
        data.setdefault("creation_time", str(int(time.time()) - 2))
        return self.post(e.ziel, data, **e.params)

    # -- Antwort auswerten -------------------------------------------------

    @staticmethod
    def _fehlertext(page: str) -> str:
        for m in re.finditer(r'<(?:p|div|span|h2)[^>]*class="[^"]*error[^"]*"[^>]*>(.*?)'
                             r'</(?:p|div|span|h2)>', page, re.S):
            txt = re.split(r"\s{2,}", strip_tags(m.group(1)))[0].strip()
            if len(txt) > 5:
                return txt
        return ""

    def pm_submission_id(self, page: str) -> int | None:
        """Identify a successful PM confirmation by its same-board view URL."""
        if (not re.search(r'\bid="message"', page)
                or re.search(r'<form\b[^>]*\bid="postform"', page)):
            return None
        base = urlsplit(self.base)
        for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
            if not re.search(r'http-equiv="refresh"', tag, re.I):
                continue
            content = re.search(r'content="([^"]+)"', tag, re.I)
            if not content:
                continue
            parts = re.split(r"\burl=", content[1], maxsplit=1, flags=re.I)
            if len(parts) != 2:
                continue
            try:
                address = urlsplit(urljoin(self.base + "/", unescape(parts[1])))
            except ValueError:
                continue
            if (address.scheme, address.netloc, address.path) != (
                    base.scheme, base.netloc, base.path.rstrip("/") + "/ucp.php"):
                continue
            query = parse_qs(address.query)
            values = query.get("p", [])
            if (query.get("i") == ["pm"] and query.get("mode") == ["view"]
                    and len(values) == 1 and re.fullmatch(r"[0-9]+", values[0])
                    and int(values[0]) > 0):
                return int(values[0])
        return None

    def submission_success(self, page: str) -> tuple[bool, str]:
        """Recognize positive confirmations without saving diagnostic HTML."""
        if re.search(r"wurde erfolgreich|successfully (?:posted|submitted|edited)",
                     page, re.I):
            return True, t("beitrag_da")
        weiter = re.search(r'<meta http-equiv="refresh"[^>]*viewtopic\.php[^"]*#p(\d+)', page)
        if weiter:
            return True, t("beitrag_da_nr", pid=weiter.group(1))
        pid = self._redirected_post_id(page)
        if pid:
            return True, t("beitrag_da_nr", pid=pid)
        return False, ""

    def _redirected_post_id(self, page: str) -> int | None:
        """Confirm a redirected post only on the configured board's thread."""
        address = urlsplit(getattr(self, "last_post_url", ""))
        base = urlsplit(self.base)
        if (address.scheme, address.netloc, address.path) != (
                base.scheme, base.netloc,
                base.path.rstrip("/") + "/viewtopic.php"):
            return None
        fragment = re.fullmatch(r"p([0-9]+)", address.fragment)
        query = parse_qs(address.query)
        values = query.get("p", [])
        value = fragment[1] if fragment else (
            values[0] if len(values) == 1 else "")
        if not value and len(query.get("t", [])) == 1:
            # A newly created topic redirects to t=ID without a post fragment.
            # Only a single visible post identifies its first post reliably.
            topic_id = query["t"][0]
            posts = set(re.findall(r'<div id="p(\d+)" class="post\b', page))
            if (re.fullmatch(r"[0-9]+", topic_id) and int(topic_id) > 0
                    and len(posts) == 1):
                value = posts.pop()
        if not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
            return None
        pid = int(value)
        if re.search(fr'<div id="p{pid}" class="post\b', page):
            return pid
        return None

    def submit_result(self, page: str) -> tuple[bool, str]:
        """(erfolgreich, Meldung). Erst positiv prüfen: ein bloßes
        `class="error"` steht auch im Gerüst von Erfolgsseiten."""
        success, message = self.submission_success(page)
        if success:
            return success, message
        if self.pm_submission_id(page) is not None:
            return True, t("pm_gesendet_kurz")
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

    # -- Private Nachrichten -----------------------------------------------
    #
    # phpBB behandelt Nachrichten fast wie Beiträge, nur unter ucp.php: die
    # Postfächer sehen aus wie Themenlisten, eine geöffnete Nachricht wie ein
    # Beitrag, und das Schreibformular heißt ebenfalls „postform". Neu ist nur
    # der Empfänger – der wird in zwei Schritten gesetzt, siehe unten.

    def pm_liste(self, ordner: str = "inbox",
                 start: int = 0) -> tuple[list[Nachricht], str, str, int, int]:
        """(Nachrichten, Überschrift, Füllstand, Seite, Seiten)"""
        page = self.get("ucp.php", i="pm", folder=ordner, start=start)
        self.require_login(page)
        # Die erste <h2> ist die Überschrift des persönlichen Bereichs und
        # trägt eine Klasse; die des Postfachs steht blank da.
        kopf = re.search(r"<h2>\s*(.*?)\s*</h2>", page, re.S)
        # Der Füllstand steht als einziger Absatz mit Prozentzeichen über der
        # Liste – so gefunden statt über den (übersetzten) Wortlaut.
        voll = re.search(r"<p>([^<]*%[^<]*)</p>", page)
        out: list[Nachricht] = []
        for r in re.split(r'(?=<li class="row)', page):
            link = re.search(r'<a href="[^"]*?mode=view[^"]*?f=(-?\d+)[^"]*?p=(\d+)[^"]*"'
                             r'[^>]*class="topictitle"[^>]*>(.*?)</a>', r, re.S)
            if not link:
                continue
            wer = _USER.search(r)
            # Eingang: „von X » Datum" im Fließtext. Gesendet/Ausgang: das
            # Datum steht rechts in einer eigenen Spalte.
            datum = (re.search(r"&raquo;\s*([^<]+)", r)
                     or re.search(r'class="info"[^>]*>\s*<span>[^<:]*:?\s*([^<]+)', r))
            out.append(Nachricht(
                mid=int(link.group(2)),
                subject=strip_tags(link.group(3)),
                partner=unescape(wer.group(1)) if wer else "",
                date=unescape(datum.group(1)) if datum else "",
                unread="pm_unread" in r,
                ordner_id=int(link.group(1)),
            ))
        seite, seiten = self._pagination(page)
        return (out, strip_tags(kopf.group(1)) if kopf else "",
                strip_tags(voll.group(1)) if voll else "", seite, seiten)

    def pm_lesen(self, mid: int, fid: int = 0) -> PM:
        from .markup import html_to_text

        page = self.get("ucp.php", i="pm", mode="view", f=fid, p=mid)
        self.require_login(page)
        betreff = re.search(r'<h3 class="first">(.*?)</h3>', page, re.S)
        kopf = re.search(r'<p class="author">(.*?)</p>', page, re.S)
        # Im Kopf stehen der Reihe nach: Datum, Absender, Empfänger.
        datum = re.search(r"</strong>\s*([^<]+)", kopf.group(1)) if kopf else None
        namen = _USER.findall(kopf.group(1)) if kopf else []
        inhalt = re.search(r'<div class="content">(.*?)</div>\s*(?:<dl class="attach|'
                           r'<div id="sig|</div>)', page, re.S)
        return PM(
            mid=mid,
            subject=strip_tags(betreff.group(1)) if betreff else "",
            von=unescape(namen[0]) if namen else "",
            an=", ".join(unescape(n) for n in namen[1:]),
            date=unescape(datum.group(1)) if datum else "",
            body=html_to_text(inhalt.group(1)) if inhalt else "",
            ordner_id=fid,
            anhaenge=self._parse_anhaenge(page),
        )

    def _pm_form(self, **hol) -> Entwurf:
        page = self.get("ucp.php", i="pm", mode="compose", **hol)
        self.require_login(page)
        if "postform" not in page:
            err = re.search(r'<div class="[^"]*message[^"]*">(.*?)</div>', page, re.S)
            raise BoardError(strip_tags(err.group(1)) if err else t("kein_formular"))
        params = {"i": "pm", "mode": "compose", "action": hol.get("action", "post")}
        if "p" in hol:
            params["p"] = hol["p"]
        return Entwurf(form_fields(page, "postform"), "ucp.php", params,
                       anhaenge_moeglich='name="fileupload"' in page,
                       empfaenger=self._pm_empfaenger(page))

    def pm_neu_entwurf(self, an_id: int = 0) -> Entwurf:
        """Leere Nachricht. `an_id` ist die Benutzernummer, wenn das Board sie
        schon kennt (Link „Private Nachricht" im Profil) – dann steht der
        Empfänger von vornherein drin."""
        return self._pm_form(u=an_id) if an_id else self._pm_form()

    def pm_antwort_entwurf(self, mid: int, fid: int = 0, zitat: bool = False) -> Entwurf:
        """Antwort oder Zitat. Der Empfänger kommt hier fertig aus dem Board:
        das Formular bringt `address_list[u][…]` als verstecktes Feld mit."""
        return self._pm_form(action="quote" if zitat else "reply", f=fid, p=mid)

    @staticmethod
    def _pm_empfaenger(page: str) -> list[str]:
        """Die Namen, die im Formular als Empfänger hängen.

        Die Nummern stehen in `address_list[u][…]`, die Namen aber nur in der
        Liste daneben – jeder Eintrag hat einen „Löschen"-Knopf `remove_u[…]`,
        direkt gefolgt vom Namen.
        """
        m = re.search(r'<form[^>]*id="postform".*?</form>', page, re.S)
        block = m.group(0) if m else page
        return [unescape(n) for n in re.findall(
            r'remove_[ug]\[\d+\].*?class="username[^"]*"[^>]*>([^<]+)</(?:a|span)>',
            block, re.S)]

    def pm_empfaenger_hinzufuegen(self, e: Entwurf, name: str) -> None:
        """Empfänger eintragen – Schritt eins von zweien.

        phpBB nimmt keinen Namen beim Absenden entgegen. Der Name muss erst
        über den Knopf „Hinzufügen" zu einer Benutzernummer aufgelöst werden;
        zurück kommt das Formular mit `address_list[u][nummer]`. Genauso läuft
        das Anhängen einer Datei, siehe `datei_anhaengen`.
        """
        data = dict(e.felder)
        data["username_list"] = name
        data["add_to"] = "Hinzufügen"
        seite = self.post(e.ziel, data, **e.params)
        fehler = self._fehlertext(seite)
        if fehler:
            raise BoardError(fehler)
        if "postform" not in seite:
            raise BoardError(t("kein_formular"))
        e.felder = form_fields(seite, "postform")
        e.empfaenger = self._pm_empfaenger(seite)

    def pm_loeschen(self, mid: int, fid: int = 0) -> None:
        self.pm_loeschen_result(mid, fid)

    def pm_loeschen_result(self, mid: int, fid: int = 0) -> str:
        """Delete through the existing confirmation flow and return its response."""
        page = self.get("ucp.php", i="pm", mode="compose", action="delete", f=fid, p=mid)
        self.require_login(page)
        m = re.search(r'<form[^>]*id="confirm"[^>]*action="([^"]*)".*?</form>', page, re.S)
        if not m:
            raise BoardError(t("formular_fehlt", id="confirm"))
        daten = form_fields(page, "confirm")
        button = re.search(r'<(?:input|button)\b[^>]*\bname="confirm"[^>]*>',
                           m.group(0))
        value = re.search(r'\bvalue="([^"]+)"', button[0]) if button else None
        if not value:
            raise BoardError(t("formular_fehlt", id="confirm"))
        daten["confirm"] = unescape(value[1])
        # Die Rückfrage trägt ihren Schlüssel in der Adresse, nicht im Formular.
        r = self.s.post(f"{self.base}/{unescape(m.group(1)).lstrip('./')}",
                        data=daten, timeout=60)
        r.raise_for_status()
        self._save()
        fehler = self._fehlertext(r.text)
        if fehler:
            raise BoardError(fehler)
        return r.text

    @staticmethod
    def _pm_badge(page: str) -> int:
        """Ungelesene Nachrichten aus der Kopfleiste. Bei Null hängt phpBB der
        Zahl die Klasse „hidden" an, statt sie wegzulassen."""
        m = re.search(r'folder=inbox[^"]*"[^>]*>.{0,400}?<strong class="badge([^"]*)"'
                      r"[^>]*>(\d+)</strong>", page, re.S)
        if not m:
            return 0
        return 0 if "hidden" in m.group(1) else int(m.group(2))

    def dump(self, page: str) -> Path:
        pfad = self.cookie_path.parent / "last-response.html"
        pfad.write_text(page, encoding="utf-8")
        return pfad
