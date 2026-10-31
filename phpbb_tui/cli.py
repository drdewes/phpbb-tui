"""Non-interactive commands sharing the existing Board client."""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

from . import config
from . import attachments
from . import posting
from . import private_messages
from .board import Anhang, Board, BoardError, Forum, LoginRequired, Post, Topic
from .command_error import CommandError


READ_COMMANDS = ("forums", "topics", "topic", "search") + attachments.COMMANDS
COMMANDS = READ_COMMANDS + posting.COMMANDS + private_messages.COMMANDS
BODY_COMMANDS = posting.COMMANDS + private_messages.WRITE_COMMANDS
SEARCHES = {"unread": "unreadposts", "new": "newposts",
            "mine": "egosearch", "active": "active_topics",
            "unanswered": "unanswered"}


class Parser(argparse.ArgumentParser):
    """Let the caller render argument errors as JSON when requested."""

    def error(self, message: str) -> None:
        if message.startswith("unrecognized arguments:"):
            message = "Unrecognized arguments; see command --help"
        raise CommandError("invalid_arguments", message)


def positive(value: str) -> int:
    """Parse positive phpBB identifiers and one-based page numbers."""
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "expected a positive integer") from error
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def parser() -> Parser:
    """Build the parser without loading configuration or sessions."""
    root = Parser(prog="phpbb-tui", allow_abbrev=False)
    commands = root.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("board", help="configured board name or slug")
        if name in READ_COMMANDS:
            command.add_argument("-g", "--guest", action="store_true",
                                 help="use an isolated anonymous session")
        else:
            command.set_defaults(guest=False)
        output = command.add_mutually_exclusive_group()
        output.add_argument("-j", "--json", action="store_true")
        output.add_argument("-q", "--quiet", action="store_true")
        if name in ("topics", "topic", "search"):
            command.add_argument("-p", "--page", type=positive, default=1)
        if name in ("forums", "topics", "search"):
            command.add_argument("-F", "--filter", default="",
                                 help="filter retrieved names/titles locally")
        if name == "topics":
            command.add_argument("-f", "--forum", type=positive, required=True)
        elif name == "topic":
            command.add_argument("-t", "--topic", type=positive, required=True)
        elif name == "search":
            modes = command.add_mutually_exclusive_group(required=True)
            for mode in SEARCHES:
                modes.add_argument("--" + mode, dest="mode",
                                   action="store_const", const=mode)
            modes.add_argument("-s", "--query")
        elif name in posting.COMMANDS:
            posting.arguments(command, name, positive)
        elif name in private_messages.COMMANDS:
            private_messages.arguments(command, name, positive)
        elif name in attachments.COMMANDS:
            attachments.arguments(command, name, positive)
    return root


class ReadBoard(Board):
    """Retain the last shared-client response for pagination navigation."""

    page_html = ""

    def get(self, path: str, **params) -> str:
        self.page_path = path
        self.page_params = params
        self.page_html = super().get(path, **params)
        return self.page_html

    def post(self, path: str, data: dict, files: dict | None = None,
             **params) -> str:
        self.page_html = super().post(path, data, files=files, **params)
        return self.page_html


class Pagination(HTMLParser):
    """Read page-number links only inside phpBB pagination containers."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.label = ""
        self.current_pages: list[int] = []
        self.in_current = False
        self.text: list[str] = []

    def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        active = (bool(self.stack and self.stack[-1][1])
                  or "pagination" in (attributes.get("class") or "").split())
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img",
                       "input", "link", "meta", "param", "source", "track",
                       "wbr"):
            self.stack.append((tag, active))
        if tag == "li" and active:
            self.in_current = "active" in (
                attributes.get("class") or "").split()
        if tag == "a" and active:
            self.href = attributes.get("href")
            self.label = ""

    def handle_data(self, data: str) -> None:
        if self.stack and self.stack[-1][1]:
            self.text.append(data)
        if self.href is not None:
            self.label += data
        if self.in_current and data.strip().isdecimal():
            self.current_pages.append(int(data.strip()))

    def handle_endtag(self, tag: str) -> None:
        if tag == "li":
            self.in_current = False
        if tag == "a" and self.href is not None:
            self.links.append((self.label.strip(), self.href))
            self.href = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def pagination(html: str, requested: int, path: str = "",
               params: dict | None = None) -> tuple[int, int | None, int]:
    """Find an offset from server links, never from a fixed page-size guess."""
    links = Pagination()
    links.feed(html)
    count = re.search(
        r"(?:Page|Seite)\s*(\d+)\s*(?:of|von)\s*(\d+)",
        " ".join(links.text), re.I)
    current = links.current_pages[0] if links.current_pages else 1
    pages = None
    if count:
        current, pages = int(count[1]), int(count[2])
    if pages is not None and requested > pages:
        raise CommandError(
            "page_not_found", "Requested page does not exist", 7)
    if requested == 1:
        return current, pages, 0
    sizes = set()
    offsets = {}
    for label, href in links.links:
        try:
            address = urlsplit(href)
        except ValueError:
            continue
        query = parse_qs(address.query)
        if path and address.path.rsplit("/", 1)[-1] != path:
            continue
        if any(query.get(key) != [str(value)]
               for key, value in (params or {}).items() if key in ("f", "t")):
            continue
        if not label.isdecimal() or len(query.get("start", [])) != 1:
            continue
        number = int(label)
        try:
            offset = int(query["start"][0])
        except ValueError:
            continue
        if number > 1 and offset > 0 and offset % (number - 1) == 0:
            sizes.add(offset // (number - 1))
            offsets[number] = offset
    if requested in offsets:
        return current, pages, offsets[requested]
    if len(sizes) == 1:
        return current, pages, (requested - 1) * sizes.pop()
    raise CommandError("pagination_unavailable",
                       "The board did not provide usable page navigation", 8)


def forum_data(forum: Forum) -> dict:
    """Map internal names to documented machine fields."""
    return {"id": forum.fid, "name": forum.name,
            "description": forum.beschreibung or None,
            "category": forum.kategorie or None, "level": forum.ebene}


def topic_data(topic: Topic) -> dict:
    return {"id": topic.tid, "title": topic.title,
            "author": topic.author or None, "replies": topic.replies,
            "views": topic.views, "last_author": topic.last_author or None,
            "last_date": topic.last_date or None, "unread": topic.unread,
            "sticky": topic.sticky, "forum": topic.forum or None}


def attachment_data(attachment: Anhang) -> dict:
    return {"id": attachment.aid, "name": attachment.name,
            "size": attachment.groesse or None, "image": attachment.bild}


def post_data(post: Post) -> dict:
    return {"id": post.pid, "author": post.author, "date": post.date or None,
            "subject": post.subject, "body": post.body, "own": post.own,
            "attachments": [attachment_data(a) for a in post.anhaenge]}


def read(board: ReadBoard, args: argparse.Namespace) -> dict:
    """Run one read operation; pagination follows the same shared operation."""
    if args.command == "forums":
        return {"forums": [forum_data(f) for f in board.foren()
                           if args.filter.lower() in f.name.lower()]}

    def fetch(start: int):
        if args.command == "topic":
            result = board.thread(args.topic, start=start)
        elif args.command == "topics":
            result = board.topics(args.forum, start=start)
        elif args.query is not None:
            result = board.suche(args.query, start=start)
        else:
            result = board.search(SEARCHES[args.mode], start=start)
        if (board._fehlertext(board.page_html)
                or re.search(r'<div\b[^>]*\bid="message"', board.page_html)):
            raise CommandError("operation_unavailable",
                               "The board did not provide this resource", 8)
        if args.command == "topic" and not result.posts:
            raise CommandError("operation_unavailable",
                               "The board did not provide topic posts", 8)
        return result

    result = fetch(0)
    current, pages, start = pagination(
        board.page_html, args.page, board.page_path, board.page_params)
    if start:
        result = fetch(start)
        current, pages, _ = pagination(board.page_html, 1)
    if current != args.page:
        raise CommandError(
            "pagination_unavailable",
            "The board did not confirm the requested page", 8)
    meta = {"page": current, "pages": pages, "start": start}
    if args.command == "topic":
        return {"topic_id": result.tid, "title": result.title,
                "forum_id": result.forum_id or None, "pagination": meta,
                "posts": [post_data(p) for p in result.posts]}
    topics, subforums, title, _, _ = result
    data = {"title": title, "pagination": meta,
            "topics": [topic_data(t) for t in topics
                       if args.filter.lower() in t.title.lower()],
            "forums": [forum_data(f) for f in subforums]}
    if args.command == "topics":
        data["forum_id"] = args.forum
    return data


def human(data: dict) -> str:
    """Render data without making human wording part of the machine API."""
    if data["action"] in attachments.COMMANDS:
        return attachments.human(data)
    if data["action"] in private_messages.COMMANDS:
        return private_messages.human(data)
    lines = []
    if "submitted" in data:
        if data.get("dry_run"):
            return f"Validated {data['action']}: {data['subject']}\n" + data[
                "body_bbcode"]
        if data.get("preview"):
            return f"Preview: {data['subject']}\n" + data["preview_text"]
        return f"Submitted {data['action']}" + (
            ": " + data["url"] if data.get("url") else "")
    if "title" in data:
        lines.append(data["title"])
    for forum in data.get("forums", []):
        lines.append(f"{'  ' * forum['level']}{forum['id']}  {forum['name']}")
    for topic in data.get("topics", []):
        marker = "*" if topic["unread"] else " "
        lines.append(f"{marker} {topic['id']}  {topic['title']}  "
                     f"({topic['replies']} replies)")
    for post in data.get("posts", []):
        header = f"\n#{post['id']}  {post['author']}  {post['date'] or ''}"
        lines.extend([header,
                      post["subject"], post["body"]])
        for attachment in post["attachments"]:
            lines.append(
                f"Attachment #{attachment['id']}: {attachment['name']}")
    if "pagination" in data:
        meta = data["pagination"]
        lines.append(f"Page {meta['page']}" +
                     (f" of {meta['pages']}" if meta["pages"] else ""))
    return "\n".join(lines)


def select_board(cfg: config.Config, name: str) -> config.BoardConfig:
    """Use name/slug/substring lookup while rejecting ambiguous matches."""
    exact = [b for b in cfg.boards
             if name.lower() in (b.name.lower(), b.slug)]
    matches = exact or [b for b in cfg.boards
                        if name.lower() in b.name.lower()]
    if len(matches) != 1:
        raise CommandError("invalid_board",
                           "Unknown or ambiguous board: " + name)
    return matches[0]


def main(argv: list[str]) -> int:
    """Parse arguments and return a status without interactive fallback."""
    json_output = "--json" in argv or "-j" in argv
    try:
        args = parser().parse_args(argv)
        if args.command in private_messages.COMMANDS:
            private_messages.validate(args)
        if args.command in attachments.COMMANDS:
            attachments.validate(args)
        body = (posting.body_input(args)
                if args.command in BODY_COMMANDS else "")
        if args.command in BODY_COMMANDS:
            attachments.validate_uploads(args, body)
        if args.command == "search" and args.query is not None:
            if not args.query.strip():
                raise CommandError(
                    "invalid_arguments", "Search query is empty")
        try:
            cfg = config.laden()
        except (OSError, ValueError, TypeError, KeyError,
                AttributeError) as error:
            raise CommandError(
                "configuration_error",
                "Could not read board configuration", 5) from error
        if any(not isinstance(b.name, str) or not b.name.strip()
               or not isinstance(b.url, str) for b in cfg.boards):
            raise CommandError(
                "configuration_error", "Invalid board configuration", 5)
        bc = select_board(cfg, args.board)
        if args.command == "attachment":
            args.output_plan = attachments.destination(args, cfg.downloads)
        try:
            address = urlsplit(bc.url)
            address.port
        except ValueError as cause:
            raise CommandError(
                "configuration_error", "Invalid board URL", 5) from cause
        if address.scheme not in ("http", "https") or not address.hostname:
            raise CommandError("configuration_error", "Invalid board URL", 5)
        if address.username is not None or address.password is not None:
            raise CommandError("configuration_error",
                               "Board URLs must not contain credentials", 5)
        with tempfile.TemporaryDirectory(prefix="phpbb-tui-guest-") as guest:
            cookie_path = (Path(guest) / "cookies.txt" if args.guest
                           else bc.cookie_path)
            board = ReadBoard(bc.url, cookie_path, bc.name)
            try:
                if not args.guest and not board.logged_in():
                    raise LoginRequired()
                if args.command in posting.COMMANDS:
                    data = posting.write(board, args, body)
                elif args.command in private_messages.COMMANDS:
                    data = private_messages.run(board, args, body, pagination)
                elif args.command in attachments.COMMANDS:
                    data = attachments.run(board, args)
                else:
                    data = read(board, args)
            finally:
                board.s.close()
        data = {"success": True, "action": args.command, "board": bc.name,
                **data}
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        elif not args.quiet:
            print(human(data))
        return 0
    except LoginRequired:
        error = CommandError(
            "authentication_required",
            "Authentication required; run: phpbb-tui login " + args.board, 3)
    except BoardError as cause:
        error = CommandError(
            "board_error", "The board could not complete the read", 4)
        network = cause.__cause__
        if isinstance(network, requests.RequestException):
            response = network.response
            status = response.status_code if response is not None else None
            if status == 404:
                error = CommandError(
                    "resource_not_found", "Resource not found", 7)
            elif status == 403:
                error = CommandError(
                    "permission_denied", "Permission denied", 8)
            elif status == 503 and (
                    args.command in private_messages.COMMANDS
                    or (args.command in attachments.COMMANDS
                        and args.message is not None)):
                error = CommandError(
                    "operation_unavailable",
                    "Private messaging is unavailable", 8)
            else:
                error = CommandError(
                    "network_error", "Could not read from board", 9)
    except OSError:
        error = CommandError(
            "local_error", "Could not access local session files", 5)
    except CommandError as cause:
        error = cause
    if json_output:
        print(json.dumps({"success": False, "error": error.code,
                          "message": str(error)}, ensure_ascii=False))
    else:
        print(str(error), file=sys.stderr)
    return error.status
