"""Sprache der Oberfläche / interface language.

Deutsch, wenn die Umgebung deutsch ist, sonst Englisch. Erzwingen mit
`PHPBB_TUI_LANG=de` oder `PHPBB_TUI_LANG=en`.
"""

from __future__ import annotations

import os

TEXTE: dict[str, dict[str, str]] = {
    "de": {
        # Hilfezeilen
        "such_ungelesen": "Ungelesene Beiträge",
        "such_neu": "Neue Beiträge seit letztem Besuch",
        "such_eigene": "Meine Beiträge",
        "such_aktiv": "Aktive Themen",
        "such_unbeantwortet": "Unbeantwortete Themen",
        "hilfe_menue": "j/k Bewegen · Enter Öffnen · / Filtern · s Suchen · w Forum wechseln · q Ende",
        "hilfe_liste": "j/k · Enter Öffnen · n/p Seite · N Neues Thema · / Filtern · R Neu laden · q Zurück",
        "hilfe_thread": ("j/k · Leer/b Seite · J/K Beitrag · r Antworten · z Zitieren · "
                         "e Bearbeiten · a Anhänge · o Browser · q Zurück"),
        "hilfe_vorschau": ("a Absenden · d Datei anhängen · x Anhang entfernen · "
                           "b Bearbeiten · q Verwerfen"),
        "hilfe_anhaenge": "Enter Öffnen · s Nur speichern · q Zurück",
        "hilfe_zurueck": "q Zurück",
        # Zustände
        "laedt": "lädt …",
        "vorschau_laeuft": "Vorschau …",
        "sendet": "wird gesendet …",
        "holt_formular": "Formular wird geholt …",
        "laedt_hoch": "wird hochgeladen …",
        "entfernt": "Anhang wird entfernt …",
        "laedt_datei": "lade {name} …",
        # Meldungen
        "verworfen": "verworfen",
        "angehaengt": "angehängt",
        "fehler_anhaengen": "Anhängen fehlgeschlagen: {fehler}",
        "fehler_entfernen": "Entfernen fehlgeschlagen: {fehler}",
        "fehler_download": "Download fehlgeschlagen: {fehler}",
        "fehler_oeffnen": "Öffnen fehlgeschlagen: {fehler}",
        "gespeichert": "gespeichert: {pfad}",
        "geoeffnet": "geöffnet: {name}",
        "nur_eigene": "Nur eigene Beiträge lassen sich bearbeiten",
        "keine_anhaenge": "Dieser Beitrag hat keine Anhänge",
        "nichts": "(nichts)",
        # Beschriftungen
        "seite_von": "Seite {a}/{b}",
        "eigener_beitrag": "(eigener Beitrag)",
        "angemeldet": "angemeldet",
        "gast": "Gast",
        "vorschau": "Vorschau",
        "betreff_zeile": "Betreff: {betreff}",
        "anhaenge_zeile": "Anhänge: {liste}",
        "anhaenge_von": "Anhänge von {autor}",
        # Eingaben
        "frage_filter": "Filter: ",
        "frage_foren_filter": "Foren filtern: ",
        "frage_suche": "Suchbegriff: ",
        "frage_datei": "Datei: ",
        "nochmal_bearbeiten": "{meldung} – nochmal bearbeiten?",
        "ja_nein": "[j/n]",
        # Editor-Vorlage
        "editor_hinweis_1": "<!-- Erste Zeile mit # = Betreff. Markdown; speichern und schließen",
        "editor_hinweis_2": "     führt zur Vorschau, dort sendet erst „a“ ab. Dieser Block fliegt raus. -->",
        # Anmeldung
        "login_kopf": "Anmeldung bei {name} ({url})",
        "login_hinweis": "Das Passwort wird nirgends gespeichert, nur das Sitzungs-Cookie.",
        "benutzername": "Benutzername",
        "passwort": "Passwort: ",
        "angemeldet_als": "Angemeldet als {name}.",
        "fehlgeschlagen": "Fehlgeschlagen: {fehler}",
        "weiter_mit_enter": "Weiter mit Enter …",
        "kein_benutzername": "Kein Benutzername.",
        # board.py
        "sitzung_abgelaufen": "Sitzung abgelaufen – bitte neu anmelden",
        "anmeldung_fehlgeschlagen": "Anmeldung fehlgeschlagen",
        "formular_fehlt": "Formular „{id}“ nicht gefunden",
        "kein_formular": "Kein Formular – fehlen die Rechte?",
        "beitrag_da": "Beitrag ist im Forum",
        "beitrag_da_nr": "Beitrag {pid} ist im Forum",
        "formular_erneut": "Forum zeigt das Formular erneut (siehe {datei})",
        "antwort_unklar": "Unklare Antwort des Forums (siehe {datei})",
        "datei_fehlt": "Datei nicht gefunden: {pfad}",
        "keine_vorschau": "(keine Vorschau erhalten)",
        "upload_unerwartet": "Unerwartete Antwort beim Hochladen",
        # Kommandozeile
        "cli_forum_fehlt": "Forum „{name}“ nicht gefunden – phpbb-tui list",
        "cli_kein_forum": "Noch kein Forum eingerichtet.",
        "cli_hinzufuegen": "  phpbb-tui add https://forum.example.org/forum",
        "cli_nicht_erreichbar": "Forum nicht erreichbar: {fehler}",
        "cli_eingerichtet": "„{name}“ eingerichtet. Start mit: phpbb-tui {slug}",
        "cli_welches": "Welches Forum?",
        "cli_nummer": "Nummer (Enter = 1): ",
        "cli_nicht_angemeldet": "nicht angemeldet",
        "cli_sitzung_weg": "Sitzung verworfen.",
        "cli_noch_nicht": "Noch nicht angemeldet bei {name}.",
        "cli_aufruf_add": "Aufruf: phpbb-tui add https://forum.example.org/forum",
    },
    "en": {
        "such_ungelesen": "Unread posts",
        "such_neu": "New posts since last visit",
        "such_eigene": "My posts",
        "such_aktiv": "Active topics",
        "such_unbeantwortet": "Unanswered topics",
        "hilfe_menue": "j/k move · Enter open · / filter · s search · w switch board · q quit",
        "hilfe_liste": "j/k · Enter open · n/p page · N new topic · / filter · R reload · q back",
        "hilfe_thread": ("j/k · Space/b page · J/K post · r reply · z quote · "
                         "e edit · a attachments · o browser · q back"),
        "hilfe_vorschau": "a send · d attach file · x remove attachment · b edit · q discard",
        "hilfe_anhaenge": "Enter open · s save only · q back",
        "hilfe_zurueck": "q back",
        "laedt": "loading …",
        "vorschau_laeuft": "preview …",
        "sendet": "sending …",
        "holt_formular": "fetching form …",
        "laedt_hoch": "uploading …",
        "entfernt": "removing attachment …",
        "laedt_datei": "downloading {name} …",
        "verworfen": "discarded",
        "angehaengt": "attached",
        "fehler_anhaengen": "attaching failed: {fehler}",
        "fehler_entfernen": "removing failed: {fehler}",
        "fehler_download": "download failed: {fehler}",
        "fehler_oeffnen": "could not open: {fehler}",
        "gespeichert": "saved: {pfad}",
        "geoeffnet": "opened: {name}",
        "nur_eigene": "You can only edit your own posts",
        "keine_anhaenge": "This post has no attachments",
        "nichts": "(nothing)",
        "seite_von": "Page {a}/{b}",
        "eigener_beitrag": "(your post)",
        "angemeldet": "signed in",
        "gast": "guest",
        "vorschau": "Preview",
        "betreff_zeile": "Subject: {betreff}",
        "anhaenge_zeile": "Attachments: {liste}",
        "anhaenge_von": "Attachments by {autor}",
        "frage_filter": "Filter: ",
        "frage_foren_filter": "Filter forums: ",
        "frage_suche": "Search for: ",
        "frage_datei": "File: ",
        "nochmal_bearbeiten": "{meldung} – edit again?",
        "ja_nein": "[y/n]",
        "editor_hinweis_1": "<!-- First line starting with # is the subject. Markdown; save and",
        "editor_hinweis_2": "     quit to reach the preview, where only “a” sends. This block is dropped. -->",
        "login_kopf": "Signing in to {name} ({url})",
        "login_hinweis": "The password is never stored, only the session cookie.",
        "benutzername": "Username",
        "passwort": "Password: ",
        "angemeldet_als": "Signed in as {name}.",
        "fehlgeschlagen": "Failed: {fehler}",
        "weiter_mit_enter": "Press Enter …",
        "kein_benutzername": "No username given.",
        "sitzung_abgelaufen": "Session expired – please sign in again",
        "anmeldung_fehlgeschlagen": "Login failed",
        "formular_fehlt": "Form “{id}” not found",
        "kein_formular": "No form – missing permissions?",
        "beitrag_da": "Post is in the forum",
        "beitrag_da_nr": "Post {pid} is in the forum",
        "formular_erneut": "Forum returned the form again (see {datei})",
        "antwort_unklar": "Unclear response from the forum (see {datei})",
        "datei_fehlt": "File not found: {pfad}",
        "keine_vorschau": "(no preview received)",
        "upload_unerwartet": "Unexpected response while uploading",
        "cli_forum_fehlt": "Board “{name}” not found – phpbb-tui list",
        "cli_kein_forum": "No board configured yet.",
        "cli_hinzufuegen": "  phpbb-tui add https://forum.example.org/forum",
        "cli_nicht_erreichbar": "Board not reachable: {fehler}",
        "cli_eingerichtet": "“{name}” is set up. Start with: phpbb-tui {slug}",
        "cli_welches": "Which board?",
        "cli_nummer": "Number (Enter = 1): ",
        "cli_nicht_angemeldet": "not signed in",
        "cli_sitzung_weg": "Session dropped.",
        "cli_noch_nicht": "Not signed in to {name} yet.",
        "cli_aufruf_add": "Usage: phpbb-tui add https://forum.example.org/forum",
    },
}

HILFE = {
    "de": """phpbb-tui – phpBB-Foren im Terminal

  phpbb-tui                 Oberfläche starten (bei mehreren Foren: Auswahl)
  phpbb-tui <name>          bestimmtes Forum öffnen
  phpbb-tui add <adresse>   Forum hinzufügen und anmelden
  phpbb-tui login <name>    (neu) anmelden
  phpbb-tui list            eingerichtete Foren zeigen
  phpbb-tui logout <name>   Sitzung verwerfen
  --guest                   ohne Anmeldung lesen (Boards mit Gastzugang)

Tasten: j/k bewegen · l/Enter öffnen · h/q zurück · ^D/^U halbe Seite
        g/G Anfang/Ende · n/p Seite · / filtern · s suchen · w Forum wechseln
        N neues Thema · r antworten · z zitieren · e bearbeiten
        a Anhänge · J/K Beitrag weiter/zurück · o im Browser öffnen

Geschrieben wird in $EDITOR, in Markdown; beim Senden wird nach BBCode
gewandelt und vorher die Vorschau des Forums gezeigt.

Konfiguration: ~/.config/phpbb-tui/boards.toml
Sprache: PHPBB_TUI_LANG=de|en""",
    "en": """phpbb-tui – phpBB forums in the terminal

  phpbb-tui                 start (asks which board if several)
  phpbb-tui <name>          open a specific board
  phpbb-tui add <url>       add a board and sign in
  phpbb-tui login <name>    sign in again
  phpbb-tui list            list configured boards
  phpbb-tui logout <name>   drop the session
  --guest                   read without an account (boards that allow guests)

Keys:   j/k move · l/Enter open · h/q back · ^D/^U half page
        g/G top/bottom · n/p page · / filter · s search · w switch board
        N new topic · r reply · z quote · e edit
        a attachments · J/K next/previous post · o open in browser

Posts are written in $EDITOR, in Markdown; on send they are converted to
BBCode and the forum's own preview is shown first.

Configuration: ~/.config/phpbb-tui/boards.toml
Language: PHPBB_TUI_LANG=de|en""",
}


def _sprache() -> str:
    gesetzt = os.environ.get("PHPBB_TUI_LANG", "").lower()
    if gesetzt.startswith(("de", "en")):
        return gesetzt[:2]
    umgebung = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES")
                or os.environ.get("LANG") or "")
    return "de" if umgebung.lower().startswith("de") else "en"


LANG = _sprache()


def t(schluessel: str, **werte) -> str:
    text = TEXTE.get(LANG, TEXTE["en"]).get(schluessel) or TEXTE["en"].get(schluessel, schluessel)
    return text.format(**werte) if werte else text


def hilfe() -> str:
    return HILFE.get(LANG, HILFE["en"])
