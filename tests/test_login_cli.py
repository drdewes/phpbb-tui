"""Exercise explicit unattended login without prompts or leaked credentials."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from phpbb_tui import __main__ as entry
from phpbb_tui import config


PASSWORD = "SYNTHETIC_PASSWORD_SECRET"
TOKEN = "SYNTHETIC_LOGIN_TOKEN"
FORM = ('<form id="login"><input name="form_token" value="' + TOKEN
        + '"><input name="creation_time" value="1"></form>')
SUCCESS = ('<a class="username">Alice</a>'
           '<a href="ucp.php?mode=logout">Logout</a>')


class LoginCliTests(unittest.TestCase):
    """Use real Board authentication with mocked HTTP and temporary cookies."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.bc = config.BoardConfig("Example", "https://example.invalid",
                                     "Configured User")
        self.cfg = config.Config([self.bc], self.root, "browser")
        self.session = Mock(headers={})
        self.session.get.return_value = Mock(text=FORM)
        self.session.post.return_value = Mock(text=SUCCESS)
        self.stdin = io.StringIO("")
        for manager in (
            patch.object(config, "laden", return_value=self.cfg),
            patch.object(config, "STATE_DIR", self.root),
            patch("phpbb_tui.board.requests.Session",
                  return_value=self.session),
            patch("builtins.input", side_effect=AssertionError("Prompt")),
            patch("getpass.getpass", side_effect=AssertionError("Password")),
            patch("sys.stdin", self.stdin),
        ):
            manager.start()
            self.addCleanup(manager.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            status = entry.main(["login", *args])
        for secret in (PASSWORD, TOKEN):
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
        return status, stdout.getvalue(), stderr.getvalue()

    def test_explicit_credentials_reuse_login_and_store_private_cookies(self):
        status, stdout, stderr = self.invoke(
            "Example", "-u", "Alice", "-p", PASSWORD, "--json")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(json.loads(stdout), {
            "success": True, "action": "login", "board": "Example",
            "username": "Alice"})
        data = self.session.post.call_args.kwargs["data"]
        self.assertEqual(data["username"], "Alice")
        self.assertEqual(data["password"], PASSWORD)
        self.assertEqual(data["form_token"], TOKEN)
        self.assertEqual(self.bc.cookie_path.stat().st_mode & 0o777, 0o600)
        self.session.post.assert_called_once()

    def test_stdin_keeps_password_spaces_and_strips_one_line_ending(self):
        self.stdin.write("  " + PASSWORD + "  \r\n")
        self.stdin.seek(0)
        status, _, _ = self.invoke("Example", "-s", "-j")
        self.assertEqual(status, 0)
        data = self.session.post.call_args.kwargs["data"]
        self.assertEqual(data["username"], "Configured User")
        self.assertEqual(data["password"], "  " + PASSWORD + "  ")

    def test_missing_credentials_do_not_prompt_or_connect(self):
        for options in (("--user", "Alice"), ("--json",),
                        ("--password", ""), ("--password-stdin",)):
            with self.subTest(options=options):
                self.assertEqual(self.invoke("Example", *options)[0], 2)
        self.session.get.assert_not_called()

    def test_username_required_without_configured_default(self):
        self.bc.user = ""
        self.assertEqual(self.invoke("Example", "-p", PASSWORD)[0], 2)
        self.session.get.assert_not_called()

    def test_password_sources_are_mutually_exclusive(self):
        status, _, _ = self.invoke("Example", "-p", PASSWORD,
                                   "--password-stdin", "--json")
        self.assertEqual(status, 2)
        self.session.get.assert_not_called()

    def test_stdin_terminal_fails_instead_of_waiting(self):
        with patch.object(self.stdin, "isatty", return_value=True):
            status, _, _ = self.invoke("Example", "--password-stdin")
        self.assertEqual(status, 2)
        self.session.get.assert_not_called()

    def test_rejected_credentials_hide_board_echoed_secrets(self):
        self.session.post.return_value = Mock(
            text='<div class="error">' + PASSWORD + TOKEN + '</div>')
        status, stdout, stderr = self.invoke("Example", "-p", PASSWORD, "-j")
        self.assertEqual(status, 3)
        self.assertEqual(json.loads(stdout)["error"], "authentication_failed")
        self.assertEqual(stderr, "")

    def test_network_failure_is_safe_and_final_post_not_retried(self):
        self.session.post.side_effect = requests.ConnectionError(PASSWORD)
        status, stdout, _ = self.invoke("Example", "-p", PASSWORD, "-j")
        self.assertEqual(status, 9)
        self.assertEqual(json.loads(stdout)["error"], "network_error")
        self.session.post.assert_called_once()

    def test_quiet_suppresses_success_and_keeps_errors_on_stderr(self):
        self.assertEqual(self.invoke("Example", "-p", PASSWORD, "-q"),
                         (0, "", ""))
        self.session.post.return_value = Mock(text=FORM)
        status, stdout, stderr = self.invoke("Example", "-p", PASSWORD, "-q")
        self.assertEqual(status, 3)
        self.assertEqual(stdout, "")
        self.assertTrue(stderr)

    def test_unknown_board_fails_before_network(self):
        self.assertEqual(self.invoke("Missing", "-p", PASSWORD, "-j")[0], 2)
        self.session.get.assert_not_called()

    def test_bare_login_still_uses_the_existing_interactive_flow(self):
        with patch.object(entry, "anmelden", return_value=True) as interactive:
            self.assertEqual(self.invoke("Example")[0], 0)
        interactive.assert_called_once()
        self.session.get.assert_not_called()

    def test_credential_options_remain_scoped_to_login(self):
        for command in (("forums", "Example"),
                        ("reply", "Example", "--topic", "42", "--body", "Hi")):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr):
                status = entry.main([*command, "--user", "Alice",
                                     "--password", PASSWORD, "--json"])
            self.assertEqual(status, 2)
            self.assertNotIn(PASSWORD, stdout.getvalue() + stderr.getvalue())
        self.session.get.assert_not_called()

    def test_multiline_stdin_is_rejected_before_http(self):
        self.stdin.write(PASSWORD + "\nsecond line\n")
        self.stdin.seek(0)
        self.assertEqual(self.invoke("Example", "--password-stdin")[0], 2)
        self.session.get.assert_not_called()

    def test_invalid_configured_url_does_not_connect(self):
        self.bc.url = "https://secret:credential@example.invalid"
        self.assertEqual(self.invoke("Example", "-p", PASSWORD, "-j")[0], 5)
        self.session.get.assert_not_called()

    def test_json_and_quiet_are_mutually_exclusive(self):
        status, stdout, _ = self.invoke(
            "Example", "-p", PASSWORD, "-j", "-q")
        self.assertEqual(status, 2)
        self.assertFalse(json.loads(stdout)["success"])
        self.session.get.assert_not_called()

    def test_bare_login_without_board_keeps_existing_selection(self):
        with patch.object(entry, "anmelden", return_value=True) as interactive:
            self.assertEqual(self.invoke()[0], 0)
        interactive.assert_called_once()

    def test_oversized_stdin_password_is_rejected_before_http(self):
        self.stdin.write("x" * 65537)
        self.stdin.seek(0)
        self.assertEqual(self.invoke("Example", "--password-stdin")[0], 2)
        self.session.get.assert_not_called()

    def test_failed_login_preserves_cookie_and_cleans_temporary_state(self):
        self.bc.cookie_path.write_bytes(b"existing private session")
        self.session.post.return_value = Mock(text=FORM)
        self.assertEqual(self.invoke("Example", "-p", PASSWORD, "-j")[0], 3)
        self.assertEqual(self.bc.cookie_path.read_bytes(),
                         b"existing private session")
        self.assertEqual(list(self.root.glob(".login-*")), [])

    def test_network_failure_preserves_existing_cookie(self):
        self.bc.cookie_path.write_bytes(b"existing private session")
        self.session.post.side_effect = requests.ConnectionError(PASSWORD)
        self.assertEqual(self.invoke("Example", "-p", PASSWORD, "-j")[0], 9)
        self.assertEqual(self.bc.cookie_path.read_bytes(),
                         b"existing private session")
        self.assertEqual(list(self.root.glob(".login-*")), [])

    def test_cookie_publication_failure_preserves_existing_session(self):
        self.bc.cookie_path.write_bytes(b"existing private session")
        with patch("phpbb_tui.login.os.replace", side_effect=PermissionError):
            status, _, _ = self.invoke("Example", "-p", PASSWORD, "-j")
            self.assertEqual(status, 5)
        self.assertEqual(self.bc.cookie_path.read_bytes(),
                         b"existing private session")
        self.assertEqual(list(self.root.glob(".login-*")), [])
