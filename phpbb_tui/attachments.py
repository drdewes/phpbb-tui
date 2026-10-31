"""Attachment discovery, safe downloads, and shared compose uploads."""

from __future__ import annotations

import argparse
import os
import re
from collections import Counter
from pathlib import Path

import requests

from .board import Anhang, Board, BoardError, Entwurf, LoginRequired
from .command_error import CommandError
from .markup import md_to_bbcode


COMMANDS = ("attachments", "attachment")
INLINE = re.compile(r"\{\{attach:([^{}]*)\}\}")
ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
FOLDERS = {"inbox": 0, "sent": -1, "outbox": -2, "sentbox": -1}


def arguments(parser: argparse.ArgumentParser, name: str, positive) -> None:
    """Allow resource context or an independently authorized file ID."""
    source = parser.add_mutually_exclusive_group(
        required=name == "attachments")
    source.add_argument("-p", "--post", type=positive)
    source.add_argument("-m", "--message", type=positive)
    parser.add_argument("-f", "--folder", choices=tuple(FOLDERS))
    if name == "attachment":
        parser.add_argument("-a", "--attachment", type=positive, required=True)
        parser.add_argument("-o", "--output",
                            help="output file or directory (trailing /)")
        parser.add_argument("-F", "--force", action="store_true",
                            help="replace a regular file after downloading")


def upload_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-A", "--attach", action="append", default=[],
                        metavar="FILE", help="upload a file; repeat as needed")
    parser.add_argument("-I", "--attach-inline", action="append", default=[],
                        metavar="ALIAS=FILE",
                        help="upload a file for {{attach:ALIAS}}; repeatable")


def destination(args: argparse.Namespace, default: Path) -> tuple[Path, bool]:
    """Validate the target before authentication or download requests."""
    try:
        if args.output == "":
            raise OSError("Empty output path")
        path = (Path(args.output).expanduser()
                if args.output is not None else default)
        directory = (args.output is None or args.output.endswith("/")
                     or path.is_dir())
        if not directory:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise OSError("Unsafe destination")
            if path.exists() and not args.force:
                raise FileExistsError("Existing destination")
        ancestor = path if directory else path.parent
        while not ancestor.exists() and not ancestor.is_symlink():
            ancestor = ancestor.parent
        if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
            raise OSError("Destination directory is not writable")
        return path, directory
    except (OSError, RuntimeError) as cause:
        raise CommandError(
            "local_output_error",
            "Invalid, existing, or unwritable output destination", 5
        ) from cause


def validate(args: argparse.Namespace) -> None:
    """Reject locally invalid combinations before any network traffic."""
    if args.folder is not None and args.message is None:
        raise CommandError("invalid_arguments", "--folder requires --message")
    if args.message is not None:
        if args.guest:
            raise CommandError(
                "invalid_arguments", "PM attachments require a session")
        args.folder = args.folder or "inbox"
    if args.command == "attachment" and args.output is not None:
        args.output_plan = destination(args, Path())


def validate_uploads(args: argparse.Namespace, body: str = "") -> None:
    """Check every requested file before any avoidable remote mutation."""
    aliases = {}
    for value in args.attach_inline:
        alias, separator, filename = value.partition("=")
        if (not separator or not filename or not ALIAS.fullmatch(alias)
                or alias in aliases):
            raise CommandError("invalid_arguments",
                               "Use distinct --attach-inline ALIAS=FILE")
        aliases[alias] = Path(filename)
    references = INLINE.findall(body)
    if (set(references) != set(aliases)
            or "{{attach:" in INLINE.sub("", body)):
        raise CommandError(
            "invalid_arguments",
            "Every inline alias needs a matching placeholder and file")
    paths = []
    try:
        for value in [*args.attach, *aliases.values()]:
            path = Path(value).expanduser()
            if not path.is_file():
                raise OSError("Upload is not a regular file")
            with path.open("rb"):
                pass
            paths.append(path)
    except (OSError, RuntimeError) as cause:
        raise CommandError(
            "local_input_error",
            "Every attachment must be a readable regular file", 5) from cause
    args.inline_files = dict(zip(aliases, paths[len(args.attach):]))
    args.attach = paths[:len(args.attach)]


def upload(board: Board, form: Entwurf, files: list[Path],
           dry_run: bool) -> list[str]:
    """Use the existing multipart workflow; never perform final submission."""
    if not files:
        return []
    if not form.anhaenge_moeglich:
        raise CommandError(
            "operation_unavailable",
            "Attachments are not available for this operation", 8)
    if dry_run:
        return [path.name for path in files]
    names = []
    for path in files:
        before = Counter(board.angehaengte_dateien(form))
        try:
            board.datei_anhaengen(form, path)
        except LoginRequired:
            raise
        except BoardError as cause:
            board.require_login(getattr(board, "page_html", ""))
            network_error = isinstance(
                cause.__cause__, requests.RequestException)
            status = 9 if network_error else 4
            raise CommandError(
                "attachment_upload_failed",
                "Upload failed; final content was not submitted", status
            ) from cause
        except OSError as cause:
            raise CommandError(
                "local_input_error",
                "Could not read attachment; no final content submitted", 5
            ) from cause
        after = Counter(board.angehaengte_dateien(form))
        if (sum(after.values()) != sum(before.values()) + 1
                or any(after[name] < count for name, count in before.items())):
            raise CommandError(
                "attachment_upload_failed",
                "Upload not confirmed; no final content submitted", 4)
        names.extend((after - before).elements())
    return names


def attachment_fields(form: Entwurf) -> dict[str, tuple[int, str]]:
    """Map stable phpBB attachment IDs to their current form positions."""
    items = {}
    for key, aid in form.felder.items():
        match = re.fullmatch(r"attachment_data\[([0-9]+)\]\[attach_id\]", key)
        if match:
            name = form.felder.get(
                f"attachment_data[{match[1]}][real_filename]", "")
            if (not re.fullmatch(r"[1-9][0-9]*", aid)
                    or aid in items or not name):
                raise CommandError("attachment_upload_failed",
                                   "Ambiguous attachment fields", 4)
            items[aid] = (int(match[1]), name)
    return items


def compose(board: Board, form: Entwurf, args: argparse.Namespace,
            body: str) -> tuple[str, list[str]]:
    """Upload files and resolve inline aliases after phpBB reorders fields."""
    initial = (attachment_fields(form)
               if args.inline_files and not args.dry_run else {})
    names = upload(board, form, args.attach, args.dry_run)
    ids = {}
    for alias, path in args.inline_files.items():
        before = attachment_fields(form) if not args.dry_run else {}
        names.extend(upload(board, form, [path], args.dry_run))
        if not args.dry_run:
            after = attachment_fields(form)
            added = after.keys() - before.keys()
            if len(added) != 1 or not before.keys() <= after.keys():
                raise CommandError("attachment_upload_failed",
                                   "Inline attachment ID not confirmed", 4)
            ids[alias] = added.pop()
    if ids:
        final = attachment_fields(form)
        if any(aid not in final for aid in ids.values()):
            raise CommandError("attachment_upload_failed",
                               "Inline attachment disappeared", 4)
        old_ids = {index: aid for aid, (index, _) in initial.items()}

        def retained(match: re.Match) -> str:
            aid = old_ids.get(int(match[1]))
            if aid is None:
                return match[0]
            if aid not in final:
                raise CommandError("attachment_upload_failed",
                                   "Existing inline attachment disappeared", 4)
            return (f"[attachment={final[aid][0]}]" + match[2]
                    + "[/attachment]")

        body = re.sub(r"\[attachment=([0-9]+)\](.*?)\[/attachment\]",
                      retained, body, flags=re.S)
        # Use neutral markers while converting Markdown so filenames and
        # underscores in aliases cannot be interpreted as Markdown formatting.
        rendered = {}
        for number, (alias, aid) in enumerate(ids.items()):
            index, name = final[aid]
            marker = f"\x00ATTACHMENT{number}\x00"
            while marker in body:
                marker += "\x00"
            body = body.replace("{{attach:" + alias + "}}", marker)
            name = name.replace("[", "&#91;").replace("]", "&#93;")
            rendered[marker] = f"[attachment={index}]{name}[/attachment]"
        message = md_to_bbcode(body)
        for marker, tag in rendered.items():
            message = message.replace(marker, tag)
        return message, names
    return md_to_bbcode(body), names


def metadata(item: Anhang) -> dict:
    return {"id": item.aid, "name": item.name,
            "size": item.groesse or None, "image": item.bild}


def source(board: Board, args: argparse.Namespace) -> list[Anhang]:
    """Scope discovery to one authorized post or PM using existing parsers."""
    if args.message is not None:
        message = board.pm_lesen(args.message, FOLDERS[args.folder])
        page = board.page_html
        if not re.search(fr'\bid="post-{args.message}"', page):
            raise CommandError(
                "operation_unavailable", "PM content unavailable", 8)
        return message.anhaenge
    page = board.get("viewtopic.php", p=args.post)
    board.require_login(page)
    block = re.search(
        fr'<div id="p{args.post}" class="post[^"]*">(.*?)'
        r'(?=<div id="p\d+" class="post|<div class="action-bar bar-bottom|$)',
        page, re.S)
    if not block:
        raise CommandError(
            "operation_unavailable", "Post content unavailable", 8)
    return board._parse_anhaenge(block[1])


def run(board: Board, args: argparse.Namespace) -> dict:
    """List attachment metadata or download through the shared Board client."""
    scoped = args.post is not None or args.message is not None
    items = source(board, args) if scoped else []
    context = {}
    if args.post is not None:
        context["post_id"] = args.post
    elif args.message is not None:
        context["message_id"] = args.message
    if args.message is not None:
        context["folder"] = "sent" if args.folder == "sentbox" else args.folder
    if args.command == "attachments":
        return {**context, "attachments": [metadata(item) for item in items]}
    if context:
        item = next((item for item in items
                     if item.aid == args.attachment), None)
        if item is None:
            raise CommandError(
                "attachment_not_found", "Attachment not visible in resource",
                7)
    else:
        item = Anhang(args.attachment, f"attachment-{args.attachment}")
    path, directory = args.output_plan
    try:
        saved = board.anhang_laden(
            item, path if directory else path.parent,
            target=None if directory else path, overwrite=args.force)
    except requests.RequestException as cause:
        response = cause.response
        status = response.status_code if response is not None else None
        code = 7 if status == 404 else 8 if status == 403 else 9
        raise CommandError(
            "attachment_download_failed", "Download failed", code) from cause
    except BoardError as cause:
        raise CommandError(
            "operation_unavailable",
            "Attachment could not be downloaded safely", 8) from cause
    except OSError as cause:
        raise CommandError(
            "local_output_error",
            "Could not save safely; use --force only for a regular file", 5
        ) from cause
    return {**context, "attachment_id": item.aid, "path": str(saved),
            "bytes": saved.stat().st_size}


def human(data: dict) -> str:
    if "path" in data:
        return f"Saved attachment #{data['attachment_id']}: {data['path']}"
    return "\n".join(f"{a['id']}  {a['name']}  {a['size'] or ''}"
                     for a in data["attachments"])
