"""Resolve editor attachment placeholders against the current compose form."""

import re

from .attachments import attachment_fields
from .board import Entwurf
from .markup import md_to_bbcode

PLACEHOLDER = re.compile(r"\{\{attach:id([1-9][0-9]*)\}\}")
TAG = re.compile(r"\[attachment=([0-9]+)\](.*?)\[/attachment\]", re.S)


def editor_body(form: Entwurf, body: str) -> str:
    """Replace existing indexed tags with stable IDs before editing."""
    positions = {index: aid for aid, (index, _) in
                 attachment_fields(form).items()}

    def replace(match: re.Match) -> str:
        aid = positions.get(int(match[1]))
        return "{{attach:id" + aid + "}}" if aid else match[0]

    return TAG.sub(replace, body)


def render(form: Entwurf, body: str) -> str:
    """Convert Markdown and resolve references after uploads or removals.

    Missing references raise ValueError before preview or final submission.
    Filenames are inserted after Markdown conversion to preserve underscores.
    """
    if "{{attach:" not in body:
        return md_to_bbcode(body)
    fields = attachment_fields(form)
    rendered = {}

    def replace(match: re.Match) -> str:
        aid = match[1]
        if aid not in fields:
            raise ValueError("Attachment no longer exists")
        index, name = fields[aid]
        marker = f"\x00TUIATTACHMENT{len(rendered)}\x00"
        while marker in body:
            marker += "\x00"
        name = name.replace("[", "&#91;").replace("]", "&#93;")
        rendered[marker] = f"[attachment={index}]{name}[/attachment]"
        return marker

    converted = PLACEHOLDER.sub(replace, body)
    if "{{attach:" in converted:
        raise ValueError("Invalid attachment placeholder")
    message = md_to_bbcode(converted)
    for marker, tag in rendered.items():
        message = message.replace(marker, tag)
    return message
