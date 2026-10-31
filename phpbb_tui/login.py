"""Explicit unattended authentication through the existing Board client."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import requests

from . import config
from .board import Board, BoardError, LoginRequired
from .cli import Parser, select_board
from .command_error import CommandError


def parser() -> Parser:
    """Parse login-only credentials without changing bare interactive login."""
    root = Parser(prog="phpbb-tui login", allow_abbrev=False)
    root.add_argument("board", help="configured board name or slug")
    root.add_argument("-u", "--user", help="override the configured username")
    password = root.add_mutually_exclusive_group(required=True)
    password.add_argument("-p", "--password",
                          help="password (visible in arguments/history)")
    password.add_argument("-s", "--password-stdin", action="store_true",
                          help="read the password from redirected stdin")
    output = root.add_mutually_exclusive_group()
    output.add_argument("-j", "--json", action="store_true")
    output.add_argument("-q", "--quiet", action="store_true")
    return root


def credentials(args, default_user: str) -> tuple[str, str]:
    """Read one bounded password, preserving spaces without line breaks."""
    user = args.user if args.user is not None else default_user
    if not isinstance(user, str) or not user.strip() or any(
            character in user for character in "\r\n\x00"):
        raise CommandError("invalid_arguments",
                           "Supply --user or configure user")
    password = args.password
    if args.password_stdin:
        if sys.stdin.isatty():
            raise CommandError("invalid_arguments",
                               "--password-stdin requires redirected input")
        try:
            password = sys.stdin.read(65537)
        except (OSError, UnicodeError) as cause:
            raise CommandError(
                "local_input_error",
                "Could not read the password from stdin", 5
            ) from cause
        if password.endswith("\r\n"):
            password = password[:-2]
        elif password.endswith("\n"):
            password = password[:-1]
    if (not password or len(password) > 65536
            or any(character in password for character in "\r\n\x00")):
        raise CommandError("invalid_arguments",
                           "Supply one nonempty password of at most 65536 "
                           "characters, without line breaks")
    return user.strip(), password


def main(argv: list[str]) -> int:
    """Log in once without prompts, emitting only safe results or errors."""
    json_output = "--json" in argv or "-j" in argv
    try:
        args = parser().parse_args(argv)
        try:
            cfg = config.laden()
            bc = select_board(cfg, args.board)
            address = urlsplit(bc.url)
            address.port
            if (address.scheme not in ("http", "https") or not address.hostname
                    or address.username is not None
                    or address.password is not None):
                raise ValueError("Invalid board URL")
        except (OSError, ValueError, TypeError, KeyError,
                AttributeError) as cause:
            raise CommandError(
                "configuration_error",
                "Could not read a valid board configuration", 5
            ) from cause
        user, password = credentials(args, bc.user)
        # phpBB redirects an authenticated session away from its login form.
        # Authenticate separately so rejection cannot replace a working cookie.
        bc.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
                prefix=".login-", dir=bc.cookie_path.parent) as temporary:
            cookie = Path(temporary) / "cookies.txt"
            board = Board(bc.url, cookie, bc.name)
            try:
                board.login(user, password)
            finally:
                board.s.close()
            os.replace(cookie, bc.cookie_path)
        result = {"success": True, "action": "login", "board": bc.name,
                  "username": board.username or user}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif not args.quiet:
            print(f"Logged in to {bc.name} as {result['username']}.")
        return 0
    except LoginRequired:
        error = CommandError(
            "authentication_failed",
            "Login failed; check credentials or required board challenges", 3)
    except BoardError as cause:
        network = isinstance(cause.__cause__, requests.RequestException)
        error = CommandError(
            "network_error" if network else "login_failed",
            "Could not complete login", 9 if network else 4)
    except OSError:
        error = CommandError("local_error",
                             "Could not access session files", 5)
    except CommandError as cause:
        error = cause
    if json_output:
        print(json.dumps({"success": False, "error": error.code,
                          "message": str(error)}, ensure_ascii=False))
    else:
        print(str(error), file=sys.stderr)
    return error.status
