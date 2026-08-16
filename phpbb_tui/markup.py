"""Umwandlung zwischen den drei Schreibweisen, die hier aufeinandertreffen:

  HTML      – was phpBB ausliefert          → lesbarer Text (Anzeige)
  Markdown  – womit geschrieben wird         → BBCode (Absenden)
  BBCode    – was phpBB speichert            → Markdown (Zitat/Bearbeiten)
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# --------------------------------------------------------------------------
# HTML → Text (Anzeige eines Beitrags)
# --------------------------------------------------------------------------

_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.quote = 0
        self.in_code = 0
        self.skip = 0          # <script>/<style>/Zitat-Kopfzeile

    # ---- Hilfen
    def _nl(self) -> None:
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "br":
            self.out.append("\n")
        elif tag == "blockquote":
            self._nl()
            self.quote += 1
        elif tag == "cite":
            self._nl()
            self.out.append("┌ ")
        elif tag == "li":
            self._nl()
            self.out.append("• ")
        elif tag == "img":
            alt = a.get("alt") or a.get("title") or ""
            if "smilies" in cls or "smiley" in cls:
                self.out.append(alt)
            else:
                self.out.append(f"[Bild: {a.get('src', '')}]")
        elif tag == "a":
            self._href = a.get("href", "")
        elif tag in ("code", "pre") or "codebox" in cls:
            self.in_code += 1
            self._nl()
        elif tag in _BLOCK:
            self._nl()

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag == "blockquote":
            self.quote = max(0, self.quote - 1)
            self._nl()
        elif tag == "cite":
            self.out.append("\n")
        elif tag == "a":
            href = getattr(self, "_href", "")
            # Nur ergänzen, wenn der Linktext nicht ohnehin die Adresse ist
            if href.startswith("http") and self.out and href not in "".join(self.out[-3:]):
                self.out.append(f" <{href}>")
            self._href = ""
        elif tag in ("code", "pre"):
            self.in_code = max(0, self.in_code - 1)
            self._nl()
        elif tag in _BLOCK:
            self._nl()

    def handle_data(self, data):
        if self.skip:
            return
        if not self.in_code:
            data = re.sub(r"[ \t]+", " ", data.replace("\n", " "))
            if not data.strip() and (not self.out or self.out[-1].endswith("\n")):
                return
        self.out.append(data)

    # ---- Ergebnis
    def text(self) -> str:
        raw = "".join(self.out)
        lines, depth = [], 0
        for line in raw.split("\n"):
            lines.append(line.rstrip())
        # Zitatebenen sind über die Marker ┌ … bereits sichtbar; hier nur noch
        # Leerzeilen eindampfen
        out: list[str] = []
        for line in lines:
            if not line.strip() and (not out or not out[-1].strip()):
                continue
            out.append(line)
        return "\n".join(out).strip()


def html_to_text(fragment: str) -> str:
    p = _Text()
    p.feed(fragment)
    p.close()
    txt = p.text()
    # Zitate als „> " einrücken: phpBB verschachtelt <blockquote>, wir markieren
    # den Beginn mit ┌ und rücken bis zur nächsten Leerzeile ein.
    out, in_quote = [], False
    for line in txt.split("\n"):
        # phpBB trennt Absätze mit <br />\n – daraus bleibt ein führendes
        # Leerzeichen stehen. Bis zu zwei davon weg; tiefere Einrückung
        # (Code, Aufzählungen) bleibt erhalten.
        line = re.sub(r"^ {1,2}(?=\S)", "", line)
        if line.startswith("┌ "):
            in_quote = True
            out.append("│ " + line[2:] + " schrieb:")
        elif in_quote and line.strip():
            out.append("│ " + line)
        else:
            in_quote = False
            out.append(line)
    return "\n".join(out)


# --------------------------------------------------------------------------
# Markdown → BBCode (Absenden)
# --------------------------------------------------------------------------

def md_to_bbcode(md: str) -> str:
    """Die im Forum übliche Teilmenge. Alles, was BBCode nicht kennt, bleibt
    als Klartext stehen – nichts geht verloren."""
    out: list[str] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    list_stack: list[str] = []

    def close_lists(to: int = 0) -> None:
        while len(list_stack) > to:
            out.append("[/list]")
            list_stack.pop()

    while i < len(lines):
        line = lines[i]

        # Codeblock ```
        m = re.match(r"^\s*```(\w*)\s*$", line)
        if m:
            close_lists()
            buf = []
            i += 1
            while i < len(lines) and not re.match(r"^\s*```\s*$", lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("[code]" + "\n".join(buf) + "[/code]")
            continue

        # Überschrift  →  fett (BBCode kennt keine Überschriften)
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            close_lists()
            out.append(f"[b]{_inline(m.group(2))}[/b]")
            i += 1
            continue

        # Zitat
        if re.match(r"^\s*>", line):
            close_lists()
            buf = []
            while i < len(lines) and re.match(r"^\s*>", lines[i]):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            # „> **Name:**" in der ersten Zeile ist die Zitatzuschreibung, wie
            # sie beim Zitieren entsteht – zurück nach [quote="Name"].
            wer = ""
            if buf:
                m2 = re.match(r"^\*\*(.+?):\*\*\s*$", buf[0].strip())
                if m2:
                    wer, buf = m2.group(1), buf[1:]
            auf = f'[quote="{wer}"]' if wer else "[quote]"
            out.append(auf + _inline("\n".join(buf).strip()) + "[/quote]")
            continue

        # Listen (auch verschachtelt über Einrückung)
        m = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if m:
            indent, marker, text = len(m.group(1)) // 2, m.group(2), m.group(3)
            kind = "[list]" if marker in "-*+" else "[list=1]"
            while len(list_stack) > indent + 1:
                out.append("[/list]")
                list_stack.pop()
            if len(list_stack) < indent + 1:
                out.append(kind)
                list_stack.append(kind)
            out.append("[*]" + _inline(text))
            i += 1
            continue

        # Trennlinie – BBCode hat keine, wir nehmen eine Textlinie
        if re.match(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$", line):
            close_lists()
            out.append("————————————————")
            i += 1
            continue

        close_lists()
        out.append(_inline(line))
        i += 1

    close_lists()
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _inline(s: str) -> str:
    """Inline-Auszeichnungen; Code-Spannen bleiben unangetastet."""
    parts = re.split(r"(`[^`]+`)", s)
    for n, part in enumerate(parts):
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            # Dieses Board kennt nur die Standard-BBCodes – ein [c] für Code im
            # Fließtext gibt es nicht und stünde wörtlich im Beitrag. Also
            # bleibt der Inhalt schlicht Klartext.
            parts[n] = part[1:-1]
            continue
        p = part
        p = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", r"[img]\2[/img]", p)
        p = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"[url=\2]\1[/url]", p)
        p = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", r"[b]\2[/b]", p)
        p = re.sub(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])", r"[i]\1[/i]", p)
        p = re.sub(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])", r"[i]\1[/i]", p)
        p = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"[s]\1[/s]", p)
        parts[n] = p
    return "".join(parts)


# --------------------------------------------------------------------------
# BBCode → Markdown (Zitieren, eigenen Beitrag bearbeiten)
# --------------------------------------------------------------------------

def bbcode_to_md(bb: str) -> str:
    s = bb.replace("\r\n", "\n")
    s = re.sub(r"\[code\](.*?)\[/code\]", lambda m: "```\n" + m.group(1).strip("\n") + "\n```",
               s, flags=re.S | re.I)
    s = re.sub(r"\[c\](.*?)\[/c\]", r"`\1`", s, flags=re.S | re.I)

    def quote(m: re.Match) -> str:
        wer = m.group(1) or ""
        inner = bbcode_to_md(m.group(2)).strip()
        kopf = f"> **{wer}:**\n" if wer else ""
        return kopf + "\n".join("> " + z for z in inner.split("\n")) + "\n"

    s = re.sub(r'\[quote(?:="([^"]*)")?\](.*?)\[/quote\]', quote, s, flags=re.S | re.I)
    s = re.sub(r"\[b\](.*?)\[/b\]", r"**\1**", s, flags=re.S | re.I)
    s = re.sub(r"\[i\](.*?)\[/i\]", r"*\1*", s, flags=re.S | re.I)
    s = re.sub(r"\[u\](.*?)\[/u\]", r"\1", s, flags=re.S | re.I)
    s = re.sub(r"\[s\](.*?)\[/s\]", r"~~\1~~", s, flags=re.S | re.I)
    s = re.sub(r"\[img\](.*?)\[/img\]", r"![](\1)", s, flags=re.S | re.I)
    s = re.sub(r'\[url=([^\]]+)\](.*?)\[/url\]', r"[\2](\1)", s, flags=re.S | re.I)
    s = re.sub(r"\[url\](.*?)\[/url\]", r"\1", s, flags=re.S | re.I)
    def liste(m: re.Match) -> str:
        nummeriert = bool(m.group(1))
        posten = [p.strip() for p in re.split(r"\[\*\]", m.group(2)) if p.strip()]
        if nummeriert:
            return "\n".join(f"{i}. {p}" for i, p in enumerate(posten, 1)) + "\n"
        return "\n".join(f"- {p}" for p in posten) + "\n"

    s = re.sub(r"\[list(=1)?\](.*?)\[/list\]", liste, s, flags=re.S | re.I)
    s = re.sub(r"\[/?list[^\]]*\]", "", s, flags=re.I)
    s = re.sub(r"\[\*\]\s*", "- ", s)
    s = re.sub(r"\[size=[^\]]*\]|\[/size\]|\[color=[^\]]*\]|\[/color\]", "", s, flags=re.I)
    return re.sub(r"\n{3,}", "\n\n", s).strip()
