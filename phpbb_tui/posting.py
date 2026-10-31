"""Compose and submit non-interactive posts through the shared Board client."""

from __future__ import annotations

import argparse
import html
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from . import attachments
from .board import Board, BoardError, Entwurf, LoginRequired
from .command_error import CommandError
from .markup import bbcode_to_md


COMMANDS = ("post", "reply", "edit")


def arguments(parser: argparse.ArgumentParser, name: str, positive) -> None:
    """Add explicit targets, body sources, and non-submitting modes."""
    if name == "post":
        parser.add_argument("-f", "--forum", type=positive, required=True)
    elif name == "reply":
        parser.add_argument("-t", "--topic", type=positive, required=True)
        parser.add_argument("-Q", "--quote", type=positive)
    else:
        parser.add_argument("-P", "--post", type=positive, required=True)
    body_arguments(parser, name == "post")


def body_arguments(parser: argparse.ArgumentParser, subject_required: bool,
                   dry_help: str = "validate without a POST request") -> None:
    """Share body sources and non-submitting modes with PM composition."""
    parser.add_argument("-s", "--subject", required=subject_required)
    attachments.upload_arguments(parser)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument("-b", "--body")
    sources.add_argument("-B", "--body-file", metavar="FILE",
                         help="UTF-8 body file; - reads piped stdin")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("-n", "--dry-run", action="store_true",
                       help=dry_help)
    modes.add_argument("-v", "--preview", action="store_true",
                       help="request phpBB's preview without final submission")


def body_input(args: argparse.Namespace) -> str:
    """Read an explicit body or piped stdin before any HTTP request."""
    if args.subject is not None and not args.subject.strip():
        raise CommandError("invalid_arguments", "Subject must not be empty")
    try:
        if args.body is not None:
            body = args.body
        elif args.body_file is not None and args.body_file != "-":
            body = Path(args.body_file).expanduser().read_text(
                encoding="utf-8")
        elif not sys.stdin.isatty():
            body = sys.stdin.read()
        else:
            raise CommandError("invalid_arguments",
                               "Supply --body, --body-file, or piped stdin")
    except (OSError, UnicodeError) as cause:
        raise CommandError(
            "local_input_error",
            "Could not read the body as UTF-8 text", 5) from cause
    if not body.strip():
        raise CommandError("invalid_arguments", "Body must not be empty")
    return body


def draft(board: Board, args: argparse.Namespace) -> Entwurf:
    """Fetch an authorized form using the same entry points as the TUI."""
    try:
        if args.command == "post":
            return board.new_topic_form(args.forum)
        if args.command == "edit":
            return board.edit_form(args.post)
        if args.quote is not None:
            return board.quote_form(args.topic, args.quote)
        return board.reply_form(args.topic)
    except LoginRequired:
        raise
    except BoardError as cause:
        if isinstance(cause.__cause__, requests.RequestException):
            raise
        raise CommandError(
            "operation_unavailable",
            "The board did not provide an editable form", 8) from cause


def submit(board: Board, form: Entwurf, subject: str, message: str,
           preview: bool) -> str:
    """Submit once; preserve uncertainty whenever final delivery is unknown."""
    try:
        return board.submit(form, subject, message, preview=preview)
    except (BoardError, OSError) as cause:
        raise submission_error(cause, preview) from cause


def submission_error(cause: Exception | None = None,
                     preview: bool = False) -> CommandError:
    """Classify one attempted write without retrying it or echoing secrets."""
    network = (cause if isinstance(cause, requests.RequestException)
               else getattr(cause, "__cause__", None))
    if preview:
        return CommandError(
            "preview_failed", "Could not obtain the preview", 9)
    if isinstance(network, requests.ConnectTimeout):
        return CommandError("network_error",
                            "Could not connect; nothing was submitted", 9)
    if isinstance(network, requests.RequestException):
        response = network.response
        status = response.status_code if response is not None else None
        if status is not None and 400 <= status < 500:
            return CommandError("submission_rejected",
                                "The server rejected the submission", 4)
    return CommandError(
        "submission_uncertain",
        "Submission may have completed; check the board before retrying", 10)


def identifiers(page: str, base: str, confirmed_url: str = "") -> dict:
    """Extract confirmed post/topic IDs and omit session-bearing URLs."""
    links = [confirmed_url] + re.findall(
        r'(?:content|href)="([^"]+)"', page, re.I)
    origin = urlsplit(base)
    for link in links:
        # Refresh values contain a delay before the actual URL.
        link = html.unescape(re.split(r"url=", link, flags=re.I)[-1]).strip()
        try:
            address = urlsplit(link)
        except ValueError:
            continue
        if address.netloc and address.netloc != origin.netloc:
            continue
        if address.path.rsplit("/", 1)[-1] != "viewtopic.php":
            continue
        query = parse_qs(address.query)
        ids = {}
        for key, name in (("t", "topic_id"), ("p", "post_id")):
            values = query.get(key, [])
            if len(values) == 1 and re.fullmatch(r"[0-9]+", values[0]):
                value = int(values[0])
                if value > 0:
                    ids[name] = value
        fragment = re.fullmatch(r"p([0-9]+)", address.fragment)
        if fragment and int(fragment[1]) > 0:
            ids["post_id"] = int(fragment[1])
        if "post_id" in ids:
            ids["url"] = (f"{base}/viewtopic.php?p={ids['post_id']}"
                          f"#p{ids['post_id']}")
        elif "topic_id" in ids:
            ids["url"] = f"{base}/viewtopic.php?t={ids['topic_id']}"
        if ids:
            return ids
    return {}


def write(board: Board, args: argparse.Namespace, body: str) -> dict:
    """Validate, compose, and optionally submit one post without UI calls."""
    form = draft(board, args)
    subject = args.subject if args.subject is not None else form.felder.get(
        "subject", "")
    if not subject.strip():
        raise CommandError("invalid_arguments", "Subject must not be empty")
    if args.command == "reply" and args.quote is not None:
        quote = bbcode_to_md(form.felder.get("message", ""))
        if not quote.strip():
            raise CommandError("operation_unavailable",
                               "The board did not provide quoted content", 8)
        body = quote + "\n\n" + body
    message, uploaded = attachments.compose(board, form, args, body)
    result = {"subject": subject, "submitted": False}
    if uploaded:
        result["attachments"] = uploaded
    if args.command == "post":
        result["forum_id"] = args.forum
    elif args.command == "reply":
        result["topic_id"] = args.topic
    else:
        result["post_id"] = args.post
    if args.dry_run:
        return {**result, "dry_run": True, "body_bbcode": message}
    page = submit(board, form, subject, message, args.preview)
    board.require_login(page)
    # Do not treat text echoed in an editable form as confirmation of success.
    if board._fehlertext(page):
        raise CommandError("submission_rejected",
                           "phpBB rejected the supplied post", 4)
    if args.preview:
        if not re.search(r'\bid="preview"', page):
            raise CommandError("preview_unavailable",
                               "phpBB did not return a preview", 8)
        return {**result, "preview": True,
                "preview_text": board.preview_text(page)}
    if re.search(r'<form\b[^>]*\bid="postform"', page):
        raise CommandError(
            "submission_rejected",
            "phpBB returned the posting form without submitting", 4)
    success, _ = board.submission_success(page)
    redirected_pid = board._redirected_post_id(page)
    confirmed_page = (redirected_pid or re.search(r'\bid="message"', page)
                      or re.search(r'<meta\b[^>]*http-equiv="refresh"',
                                   page, re.I))
    if not success or not confirmed_page:
        raise CommandError(
            "submission_uncertain",
            "Submission may have completed; check the board before retrying",
            10)
    ids = identifiers(page, board.base,
                      board.last_post_url if redirected_pid else "")
    if redirected_pid:
        ids["post_id"] = redirected_pid
        ids["url"] = (f"{board.base}/viewtopic.php?p={redirected_pid}"
                      f"#p{redirected_pid}")
    if ((args.command == "reply" and ids.get("topic_id", args.topic)
         != args.topic) or (args.command == "edit" and ids.get(
             "post_id", args.post) != args.post)):
        raise CommandError(
            "submission_uncertain",
            "The returned post target differs from the request", 10)
    return {**result, "submitted": True, **ids}
