"""Non-interactive PM operations using the shared phpBB client and forms."""

from __future__ import annotations

import argparse
import re
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from . import posting
from . import attachments
from .board import Board, BoardError, Entwurf, LoginRequired, unescape
from .command_error import CommandError
from .markup import bbcode_to_md


WRITE_COMMANDS = ("pm", "pm-reply")
COMMANDS = ("pms", "pm-read", *WRITE_COMMANDS, "pm-delete")
FOLDERS = {"inbox": (0, "inbox"), "sent": (-1, "sentbox"),
           "outbox": (-2, "outbox")}
FOLDER_NAMES = {value[0]: name for name, value in FOLDERS.items()}


def arguments(parser: argparse.ArgumentParser, name: str, positive) -> None:
    """Expose mailbox context and explicit confirmation without prompts."""
    if name != "pm":
        parser.add_argument("-f", "--folder", default="inbox",
                            choices=(*FOLDERS, "sentbox"))
    if name == "pms":
        parser.add_argument("-p", "--page", type=positive, default=1)
        parser.add_argument("-F", "--filter", default="")
    elif name != "pm":
        parser.add_argument("-m", "--message", type=positive, required=True)
    if name == "pm":
        parser.add_argument("-t", "--to", action="append", required=True)
    elif name == "pm-reply":
        parser.add_argument("-Q", "--quote", action="store_true")
    elif name == "pm-delete":
        parser.add_argument("-y", "--yes", action="store_true", required=True,
                            help="explicitly confirm deletion of this PM")
    if name in WRITE_COMMANDS:
        posting.body_arguments(
            parser, name == "pm",
            "validate form and recipients without final submission")


def username_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def validate(args: argparse.Namespace) -> None:
    """Validate recipient input before network access and normalize folders."""
    if args.command == "pm":
        names = []
        seen = set()
        for name in args.to:
            name = name.strip()
            if not name or "\n" in name or "\r" in name:
                raise CommandError(
                    "invalid_arguments", "Supply one username per --to")
            key = username_key(name)
            if key not in seen:
                names.append(name)
                seen.add(key)
        args.to = names
    elif args.folder == "sentbox":
        args.folder = "sent"


def addresses(form: Entwurf) -> set[str]:
    """Identify resolved recipients without exposing hidden fields."""
    return {key for key in form.felder
            if re.fullmatch(r"address_list\[u\]\[[1-9][0-9]*\]", key)}


def compose_form(board: Board, args: argparse.Namespace) -> Entwurf:
    """Fetch PM forms and resolve every recipient before final send."""
    try:
        if args.command == "pm":
            form = board.pm_neu_entwurf()
        else:
            form = board.pm_antwort_entwurf(
                args.message, FOLDERS[args.folder][0], zitat=args.quote)
    except LoginRequired:
        raise
    except BoardError as cause:
        if isinstance(cause.__cause__, requests.RequestException):
            raise
        raise CommandError("operation_unavailable",
                           "The board did not provide a PM form", 8) from cause
    if args.command == "pm":
        if addresses(form) or any(k.startswith("address_list[g]")
                                  for k in form.felder):
            raise CommandError("operation_unavailable",
                               "Unexpected preselected PM recipients", 8)
        for name in args.to:
            before = addresses(form)
            try:
                board.pm_empfaenger_hinzufuegen(form, name)
            except BoardError as cause:
                board.require_login(board.page_html)
                if isinstance(cause.__cause__, requests.RequestException):
                    raise CommandError(
                        "network_error",
                        "Could not resolve PM recipients", 9) from cause
                raise CommandError(
                    "recipient_resolution_failed",
                    "Could not resolve every requested recipient", 6
                ) from cause
            after = addresses(form)
            duplicate = username_key(name) in {
                username_key(n) for n in form.empfaenger}
            if not before.issubset(after) or (
                    len(after - before) != 1
                    and not (after == before and duplicate)):
                raise CommandError(
                    "recipient_resolution_failed",
                    "Could not resolve every requested recipient", 6)
    if (not addresses(form) or len(form.empfaenger) != len(addresses(form))
            or any(k.startswith("address_list[g]") for k in form.felder)):
        raise CommandError("recipient_resolution_failed",
                           "The board did not confirm PM recipients", 6)
    return form


def send(board: Board, args: argparse.Namespace, body: str) -> dict:
    """Compose a PM using shared converters; send at most one final request."""
    form = compose_form(board, args)
    subject = args.subject if args.subject is not None else form.felder.get(
        "subject", "")
    if not subject.strip():
        raise CommandError("invalid_arguments", "Subject must not be empty")
    if args.command == "pm-reply" and args.quote:
        quote = bbcode_to_md(form.felder.get("message", ""))
        if not quote.strip():
            raise CommandError(
                "operation_unavailable",
                "The board did not provide quoted PM content", 8)
        body = quote + "\n\n" + body
    message, uploaded = attachments.compose(board, form, args, body)
    result = {"subject": subject, "recipients": form.empfaenger,
              "submitted": False}
    if uploaded:
        result["attachments"] = uploaded
    if args.command == "pm-reply":
        result["in_reply_to"] = args.message
    if args.dry_run:
        return {**result, "dry_run": True, "body_bbcode": message}
    page = posting.submit(board, form, subject, message, args.preview)
    board.require_login(page)
    if board._fehlertext(page):
        raise CommandError("submission_rejected", "phpBB rejected the PM", 4)
    if args.preview:
        if not re.search(r'\bid="preview"', page):
            raise CommandError(
                "preview_unavailable", "No PM preview returned", 8)
        return {**result, "preview": True,
                "preview_text": board.preview_text(page)}
    if re.search(r'<form\b[^>]*\bid="postform"', page):
        raise CommandError("submission_rejected",
                           "phpBB returned the PM form without sending", 4)
    mid = board.pm_submission_id(page)
    if mid is None:
        raise posting.submission_error()
    return {**result, "submitted": True, "message_id": mid,
            "url": f"{board.base}/ucp.php?i=pm&mode=view&p={mid}"}


def check_page(board: Board) -> None:
    if (board._fehlertext(board.page_html)
            or re.search(r'\bid="message"', board.page_html)):
        raise CommandError("operation_unavailable",
                           "The board did not provide the PM resource", 8)


def read(board: Board, args: argparse.Namespace, paginate) -> dict:
    """Read one mailbox page or one PM through the existing parsers."""
    fid, internal = FOLDERS[args.folder]
    if args.command == "pm-read":
        pm = board.pm_lesen(args.message, fid)
        check_page(board)
        if not re.search(fr'\bid="post-{args.message}"', board.page_html):
            raise CommandError("operation_unavailable",
                               "The board did not provide PM content", 8)
        return {"message_id": pm.mid, "subject": pm.subject,
                "sender": pm.von or None, "recipients": pm.an or None,
                "date": pm.date or None, "body": pm.body,
                "folder": args.folder, "folder_id": fid,
                "attachments": [{"id": a.aid, "name": a.name,
                                 "size": a.groesse or None, "image": a.bild}
                                for a in pm.anhaenge]}
    rows, title, storage, _, _ = board.pm_liste(internal)
    check_page(board)
    current, pages, start = paginate(
        board.page_html, args.page, board.page_path, board.page_params)
    if start:
        rows, title, storage, _, _ = board.pm_liste(internal, start=start)
        check_page(board)
        current, pages, _ = paginate(board.page_html, 1)
    if current != args.page:
        raise CommandError(
            "pagination_unavailable",
            "The board did not confirm the requested PM page", 8)
    return {"folder": args.folder, "title": title, "storage": storage or None,
            "pagination": {"page": current, "pages": pages, "start": start},
            "messages": [{"id": m.mid, "subject": m.subject,
                          "partner": m.partner or None, "date": m.date or None,
                          "unread": m.unread,
                          "folder": FOLDER_NAMES.get(m.ordner_id),
                          "folder_id": m.ordner_id}
                         for m in rows
                         if args.filter.lower() in m.subject.lower()]}


def deletion_confirmed(page: str, base: str, folder: str) -> bool:
    """Recognize a deletion's return-to-folder confirmation, not a prompt."""
    if (not re.search(r'\bid="message"', page)
            or re.search(r'<form\b[^>]*\bid="confirm"', page)):
        return False
    origin = urlsplit(base)
    fid, internal = FOLDERS[folder]
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        if not re.search(r'http-equiv="refresh"', tag, re.I):
            continue
        content = re.search(r'content="([^"]+)"', tag)
        if not content:
            continue
        parts = re.split(r"\burl=", content[1], maxsplit=1, flags=re.I)
        if len(parts) != 2:
            continue
        try:
            address = urlsplit(urljoin(base + "/", unescape(parts[1])))
        except ValueError:
            continue
        if (address.scheme, address.netloc, address.path) != (
                origin.scheme, origin.netloc,
                origin.path.rstrip("/") + "/ucp.php"):
            continue
        query = parse_qs(address.query)
        if query.get("i") == ["pm"] and query.get("folder") in (
                [str(fid)], [folder], [internal]):
            return True
    return False


def delete(board: Board, args: argparse.Namespace) -> dict:
    """Use the existing confirmation flow after an explicit --yes argument."""
    try:
        page = board.pm_loeschen_result(args.message, FOLDERS[args.folder][0])
    except LoginRequired:
        raise
    except BoardError as cause:
        if isinstance(cause.__cause__, requests.RequestException):
            raise
        raise CommandError("operation_unavailable",
                           "The board did not allow PM deletion", 8) from cause
    except (requests.RequestException, OSError) as cause:
        raise posting.submission_error(cause) from cause
    board.require_login(page)
    if not deletion_confirmed(page, board.base, args.folder):
        raise posting.submission_error()
    return {"message_id": args.message, "folder": args.folder, "deleted": True}


def run(board: Board, args: argparse.Namespace, body: str, paginate) -> dict:
    if args.command in WRITE_COMMANDS:
        return send(board, args, body)
    if args.command == "pm-delete":
        return delete(board, args)
    return read(board, args, paginate)


def human(data: dict) -> str:
    if data.get("deleted"):
        return f"Deleted PM #{data['message_id']} from {data['folder']}"
    if "submitted" in data:
        if data.get("dry_run"):
            return f"Validated PM: {data['subject']}\n{data['body_bbcode']}"
        if data.get("preview"):
            return f"PM preview: {data['subject']}\n{data['preview_text']}"
        return (f"Sent PM #{data['message_id']} to "
                + ", ".join(data["recipients"]))
    if data["action"] == "pm-read":
        return (f"{data['subject']}\nFrom: {data['sender']}\n"
                f"To: {data['recipients']}\n\n{data['body']}")
    lines = [f"{data['title']} ({data['folder']})"]
    for message in data["messages"]:
        marker = "*" if message["unread"] else " "
        lines.append(f"{marker} {message['id']}  {message['subject']}  "
                     f"{message['partner'] or ''}  {message['date'] or ''}")
    lines.append(f"Page {data['pagination']['page']}")
    return "\n".join(lines)
