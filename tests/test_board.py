"""Exercise existing board behavior using synthetic pages and mocked HTTP."""

import subprocess
import sys
import tempfile
import unittest
from http.cookiejar import Cookie
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from phpbb_tui.board import Board, BoardError, LoginRequired


FIXTURES = Path(__file__).parent / "fixtures"


class BoardTests(unittest.TestCase):
    """Keep sessions isolated and prohibit accidental real HTTP requests."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Mock()
        self.session.headers = {}
        session_patch = patch("phpbb_tui.board.requests.Session",
                              return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.cookie_path = Path(self.temp.name) / "board.cookies.txt"
        self.board = Board("https://example.invalid/", self.cookie_path)

    def test_import_does_not_load_curses(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import sys; import phpbb_tui.board; "
             "assert 'curses' not in sys.modules; "
             "assert 'phpbb_tui.ui' not in sys.modules"],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_jumpbox_preserves_hierarchy(self) -> None:
        forums = self.board._parse_jumpbox(
            (FIXTURES / "forum.html").read_text(encoding="utf-8"))
        self.assertEqual(
            [(f.fid, f.name, f.kategorie, f.ebene) for f in forums],
            [(1, "Category", "", 0),
             (2, "Support & help", "Category", 0),
             (3, "Child", "Category", 1)],
        )

    def test_topics_keep_nested_pagination_and_metadata(self) -> None:
        self.session.get.return_value.text = (
            FIXTURES / "forum.html").read_text(encoding="utf-8")
        topics, _, title, page, pages = self.board.topics(2, start=25)
        self.assertEqual((title, page, pages), ("Support & help", 2, 3))
        self.assertEqual([t.tid for t in topics], [42, 43])
        first = topics[0]
        self.assertEqual((first.title, first.author, first.last_author),
                         ("Café & support", "Alice", "Bob"))
        self.assertEqual((first.replies, first.views), (12, 345))
        self.assertTrue(first.unread)
        self.assertTrue(first.sticky)
        self.assertFalse(topics[1].unread)
        self.session.get.assert_called_once_with(
            "https://example.invalid/viewforum.php",
            params={"f": 2, "start": 25}, timeout=30)

    def test_thread_preserves_posts_and_ownership(self) -> None:
        self.session.get.return_value.text = (
            FIXTURES / "thread.html").read_text(encoding="utf-8")
        self.board.username = "Alice"
        thread = self.board.thread(42, start=10)
        self.assertEqual((thread.tid, thread.forum_id, thread.start,
                          thread.page, thread.pages), (42, 2, 10, 2, 3))
        self.assertEqual([p.pid for p in thread.posts], [101, 102])
        self.assertIn("café", thread.posts[0].body)
        self.assertTrue(thread.posts[0].own)
        self.assertFalse(thread.posts[1].own)

    def test_expired_session_is_reported(self) -> None:
        self.session.get.return_value.text = (
            '<title>Login</title><a href="ucp.php?mode=login">Login</a>')
        with self.assertRaises(LoginRequired):
            self.board.topics(2)

    def test_get_retries_connection_failure_once(self) -> None:
        self.session.get.side_effect = [
            requests.ConnectionError(), Mock(text="recovered")]
        self.assertEqual(self.board.get("index.php"), "recovered")
        self.assertEqual(self.session.get.call_count, 2)
        self.session.close.assert_called_once_with()

    def test_get_stops_after_second_connection_failure(self) -> None:
        self.session.get.side_effect = requests.ConnectionError()
        with self.assertRaises(BoardError):
            self.board.get("index.php")
        self.assertEqual(self.session.get.call_count, 2)

    def test_post_never_retries_network_failure(self) -> None:
        for error in (requests.ConnectionError, requests.Timeout,
                      requests.HTTPError):
            with self.subTest(error=error):
                self.session.post.reset_mock(side_effect=True)
                self.session.post.side_effect = error()
                with self.assertRaises(BoardError):
                    self.board.post("posting.php", {"message": "Synthetic"})
                self.session.post.assert_called_once()

    @staticmethod
    def _http_error(status: int, headers: dict) -> requests.HTTPError:
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        return requests.HTTPError(f"{status} Client Error", response=response)

    def test_cloudflare_challenge_gets_its_own_message(self) -> None:
        error = self._http_error(403, {"cf-mitigated": "challenge"})
        self.session.get.return_value.raise_for_status.side_effect = error
        self.session.post.return_value.raise_for_status.side_effect = error
        for call in (lambda: self.board.get("index.php"),
                     lambda: self.board.post("posting.php", {"message": "x"})):
            with self.subTest(call=call):
                with self.assertRaises(BoardError) as raised:
                    call()
                self.assertIn("Cloudflare", str(raised.exception))
                self.assertNotIn("403", str(raised.exception))

    def test_plain_forbidden_keeps_generic_message(self) -> None:
        error = self._http_error(403, {"server": "cloudflare"})
        self.session.get.return_value.raise_for_status.side_effect = error
        with self.assertRaises(BoardError) as raised:
            self.board.get("index.php")
        self.assertIn("403", str(raised.exception))
        self.assertNotIn("challenge", str(raised.exception))

    def test_saved_cookies_load_and_have_private_permissions(self) -> None:
        cookie = Cookie(
            0, "synthetic_session", "fixture", None, False,
            "example.invalid", False, False, "/", True, False,
            None, True, None, None, {}, False,
        )
        self.board.jar.set_cookie(cookie)
        self.board._save()
        restored = Board("https://example.invalid", self.cookie_path)
        self.assertEqual([(c.name, c.value) for c in restored.jar],
                         [("synthetic_session", "fixture")])
        self.assertEqual(self.cookie_path.stat().st_mode & 0o777, 0o600)
