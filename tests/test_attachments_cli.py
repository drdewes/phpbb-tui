"""Exercise attachment commands through shared client operations."""

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


SECRET = "SYNTHETIC_ATTACHMENT_TOKEN"


def form(names=(), files=(), enabled=True):
    fields = ''.join(
        f'<input name="attachment_data[{i}][attach_id]" value="{i + 1}">'
        f'<input name="attachment_data[{i}][real_filename]" value="{name}">'
        for i, name in enumerate(files))
    if names:
        fields += ('<input name="address_list[u][77]" value="to">'
                   '<input type="submit" name="remove_u[77]" value="x">'
                   '<a class="username">Alice</a>')
    if enabled:
        fields += '<input type="file" name="fileupload">'
    return (f'<form id="postform">{fields}'
            f'<input name="form_token" value="{SECRET}">'
            '<input name="creation_time" value="1">'
            '<input name="subject" value="Original">'
            '<textarea name="message">Original body</textarea></form>')


def attachment(aid=7, name="café.txt"):
    return (f'<dl class="file"><a href="./download/file.php?id={aid}">'
            f'{name}</a> (7 B)</dl>')


class AttachmentsCliTests(unittest.TestCase):
    """Exercise real Board methods using isolated files and mocked HTTP."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enabled = True
        self.uploads = []
        self.responses = []
        self.session = Mock(headers={})
        self.session.get.side_effect = self.get_response
        self.session.post.side_effect = self.post_response
        cfg = config.Config(
            [config.BoardConfig("Example", "https://example.invalid")],
            self.root / "downloads", "browser")
        for manager in (
            patch.object(config, "laden", return_value=cfg),
            patch.object(config, "STATE_DIR", self.root / "state"),
            patch("phpbb_tui.board.requests.Session",
                  return_value=self.session),
            patch("builtins.input", side_effect=AssertionError("Prompt")),
            patch("getpass.getpass", side_effect=AssertionError("Password")),
            patch("sys.stdin", io.StringIO("")),
            patch("subprocess.call", side_effect=AssertionError("Browser")),
        ):
            manager.start()
            self.addCleanup(manager.stop)

    def get_response(self, url, **kwargs):
        if url.endswith("index.php"):
            return Mock(text='<a href="ucp.php?mode=logout">Logout</a>')
        if url.endswith("download/file.php"):
            response = Mock(headers={
                "Content-Disposition":
                "attachment; filename*=UTF-8''caf%C3%A9.txt"})
            response.iter_content.return_value = iter((b"payload",))
            self.responses.append(response)
            return response
        params = kwargs.get("params") or {}
        if url.endswith("posting.php") or params.get("mode") == "compose":
            names = ("Alice",) if params.get("p") else ()
            return Mock(text=form(names=names, enabled=self.enabled))
        if params.get("mode") == "view":
            return Mock(text='<div id="post-5" class="post pm">'
                        '<h3 class="first">PM</h3>'
                        '<div class="content">Body</div>'
                        + attachment(9) + '</div>')
        return Mock(text='<div id="p101" class="post bg2">' + attachment()
                    + '</div><div id="p102" class="post bg1">'
                    + attachment(8, "other.txt") + '</div>')

    def post_response(self, url, **kwargs):
        data = kwargs["data"]
        names = ("Alice",) if "address_list[u][77]" in data else ()
        files = [value for key, value in data.items()
                 if key.endswith('[real_filename]')]
        if "add_to" in data:
            return Mock(text=form(("Alice",), files, self.enabled))
        if "add_file" in data:
            name, handle = kwargs["files"]["fileupload"]
            self.uploads.append((name, handle.read()))
            return Mock(text=form(names, (*files, name), self.enabled))
        if "preview" in data:
            return Mock(text='<div id="preview"><div class="content">'
                        'Preview</div></div>' + form(names, files))
        if url.endswith("ucp.php"):
            return Mock(text='<div id="message">Sent</div>'
                        '<meta http-equiv="refresh" content="3; '
                        'url=./ucp.php?i=pm&amp;mode=view&amp;p=22">')
        return Mock(text='<div id="message">Successfully posted</div>'
                    '<meta http-equiv="refresh" content="3; '
                    'url=./viewtopic.php?t=42&amp;p=201#p201">')

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            status = entry.main([*args, "--json"])
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(SECRET, stdout.getvalue())
        return status, json.loads(stdout.getvalue())

    def fixture(self, name):
        path = self.root / name
        path.write_bytes(b"fixture data")
        return str(path)

    def post_args(self, *extra):
        return ("post", "Example", "--forum", "2", "--subject", "Subject",
                "--body", "Body", *extra)

    def final_sends(self):
        return [call for call in self.session.post.call_args_list
                if "post" in call.kwargs["data"]]

    def test_inline_alias_uses_returned_attachment_index(self):
        path = self.fixture("mascot.png")
        status, _ = self.invoke(
            "reply", "Example", "--topic", "42", "--body",
            "Before {{attach:mascot}} after", "--attach-inline",
            "mascot=" + path)
        self.assertEqual(status, 0)
        message = self.final_sends()[0].kwargs["data"]["message"]
        self.assertEqual(message,
                         "Before [attachment=0]mascot.png[/attachment] after")

    def test_unknown_inline_alias_fails_before_network(self):
        status, _ = self.invoke(
            *self.post_args(), "--body", "{{attach:missing}}")
        self.assertEqual(status, 2)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_inline_dry_run_keeps_unresolved_indexes_without_uploading(self):
        status, data = self.invoke(
            *self.post_args(), "--body", "{{attach:bear}}",
            "--attach-inline", "bear=" + self.fixture("bear.png"),
            "--dry-run")
        self.assertEqual(status, 0)
        self.assertIn("{{attach:bear}}", data["body_bbcode"])
        self.assertEqual(data["attachments"], ["bear.png"])
        self.session.post.assert_not_called()

    def test_inline_reordering_duplicate_names_and_existing_edit_files(self):
        from phpbb_tui.board import Board

        def prepend(form, path):
            rows = [(form.felder[key], form.felder[
                key.replace("[attach_id]", "[real_filename]")])
                for key in form.felder if key.endswith("[attach_id]")]
            rows.insert(0, (str(100 + len(rows)), path.name))
            for key in list(form.felder):
                if key.startswith("attachment_data["):
                    del form.felder[key]
            for index, (aid, name) in enumerate(rows):
                form.felder[f"attachment_data[{index}][attach_id]"] = aid
                form.felder[f"attachment_data[{index}][real_filename]"] = name

        original_get = self.get_response
        self.session.get.side_effect = lambda url, **kwargs: (
            Mock(text=form(files=("existing.png",)))
            if url.endswith("posting.php") else original_get(url, **kwargs))
        path = self.fixture("same_name.png")
        with patch.object(Board, "datei_anhaengen", side_effect=prepend):
            status, _ = self.invoke(
                "edit", "Example", "--post", "201", "--body",
                "{{attach:first_one}} then {{attach:second_two}} "
                "[attachment=0]existing.png[/attachment]",
                "--attach", path, "--attach-inline", "first_one=" + path,
                "--attach-inline", "second_two=" + path)
        self.assertEqual(status, 0)
        message = self.final_sends()[0].kwargs["data"]["message"]
        self.assertEqual(message,
                         "[attachment=1]same_name.png[/attachment] then "
                         "[attachment=0]same_name.png[/attachment] "
                         "[attachment=3]existing.png[/attachment]")

    def test_invalid_inline_aliases_make_no_requests(self):
        path = self.fixture("bear.png")
        cases = (("bear=" + path, "bear=" + path),
                 ("bad alias=" + path,), ("bear=",), ("bear",),
                 ("unused=" + path,))
        for values in cases:
            with self.subTest(values=values):
                options = [item for value in values
                           for item in ("--attach-inline", value)]
                self.assertEqual(self.invoke(
                    *self.post_args(), "--body", "{{attach:bear}}",
                    *options)[0], 2)
        self.session.get.assert_not_called()
        self.session.post.assert_not_called()

    def test_missing_inline_file_fails_before_network(self):
        self.assertEqual(self.invoke(
            *self.post_args(), "--body", "{{attach:bear}}",
            "--attach-inline", "bear=" + str(self.root / "missing.png"))[0], 5)
        self.session.get.assert_not_called()

    def test_unconfirmed_inline_id_stops_final_submission(self):
        from phpbb_tui.board import Board

        def missing_id(form, path):
            form.felder["attachment_data[0][real_filename]"] = path.name

        with patch.object(Board, "datei_anhaengen", side_effect=missing_id):
            status, _ = self.invoke(
                *self.post_args(), "--body", "{{attach:bear}}",
                "--attach-inline", "bear=" + self.fixture("bear.png"))
        self.assertEqual(status, 4)
        self.assertEqual(self.final_sends(), [])

    def test_pm_and_pm_reply_use_inline_placement(self):
        path = self.fixture("bear.png")
        for command in (("pm", "Example", "--to", "Alice",
                         "--subject", "Inline"),
                        ("pm-reply", "Example", "--message", "5")):
            with self.subTest(command=command):
                status, _ = self.invoke(
                    *command, "--body", "{{attach:bear}}",
                    "--attach-inline", "bear=" + path)
                self.assertEqual(status, 0)
                message = self.final_sends()[-1].kwargs["data"]["message"]
                self.assertEqual(message,
                                 "[attachment=0]bear.png[/attachment]")

    def test_repeated_inline_placeholder_uploads_only_once(self):
        path = self.fixture("bear.png")
        status, _ = self.invoke(
            *self.post_args(), "--body", "{{attach:bear}} {{attach:bear}}",
            "-I", "bear=" + path)
        self.assertEqual(status, 0)
        self.assertEqual(len(self.uploads), 1)
        message = self.final_sends()[0].kwargs["data"]["message"]
        self.assertEqual(message.count("[attachment=0]"), 2)

    def test_inline_preview_resolves_tags_without_final_send(self):
        status, data = self.invoke(
            *self.post_args(), "--body", "{{attach:bear}}", "--preview",
            "-I", "bear=" + self.fixture("bear.png"))
        self.assertEqual(status, 0)
        self.assertFalse(data["submitted"])
        self.assertEqual(self.final_sends(), [])
        message = self.session.post.call_args.kwargs["data"]["message"]
        self.assertEqual(message, "[attachment=0]bear.png[/attachment]")

    def test_unclosed_inline_placeholder_fails_before_http(self):
        status, _ = self.invoke(
            *self.post_args(), "--body", "{{attach:bear}",
            "-I", "bear=" + self.fixture("bear.png"))
        self.assertEqual(status, 2)
        self.session.get.assert_not_called()

    def test_listing_is_scoped_to_the_selected_post(self):
        status, data = self.invoke("attachments", "Example", "--post", "101")
        self.assertEqual(status, 0)
        self.assertEqual([a["id"] for a in data["attachments"]], [7])
        self.assertEqual(data["post_id"], 101)

    def test_pm_listing_uses_folder_context(self):
        status, data = self.invoke("attachments", "Example", "--message", "5",
                                   "--folder", "sentbox")
        self.assertEqual(status, 0)
        self.assertEqual(data["folder"], "sent")
        self.assertEqual([a["id"] for a in data["attachments"]], [9])
        self.assertEqual(self.session.get.call_args.kwargs["params"]["f"], -1)

    def test_attachment_id_alone_downloads_with_server_filename(self):
        status, data = self.invoke(
            "attachment", "Example", "--attachment", "7")
        self.assertEqual(status, 0)
        saved = Path(data["path"])
        self.assertEqual(saved, self.root / "downloads/café.txt")
        self.assertEqual(saved.read_bytes(), b"payload")
        self.assertEqual(data["bytes"], 7)
        self.responses[0].close.assert_called_once()

    def test_explicit_file_and_new_directory_outputs(self):
        for output, expected in (
            (str(self.root / "chosen.bin"), self.root / "chosen.bin"),
            (str(self.root / "new") + "/", self.root / "new/café.txt"),
        ):
            with self.subTest(output=output):
                status, data = self.invoke(
                    "attachment", "Example", "--attachment", "7",
                    "--output", output)
                self.assertEqual(status, 0)
                self.assertEqual(Path(data["path"]), expected)
                self.assertEqual(expected.read_bytes(), b"payload")

    def test_existing_explicit_file_fails_before_http_without_force(self):
        path = self.root / "chosen.bin"
        path.write_bytes(b"preserve")
        status, _ = self.invoke("attachment", "Example", "--attachment", "7",
                                "--output", str(path))
        self.assertEqual(status, 5)
        self.assertEqual(path.read_bytes(), b"preserve")
        self.session.get.assert_not_called()

    def test_empty_output_is_not_the_working_directory(self):
        self.assertEqual(self.invoke(
            "attachment", "Example", "--attachment", "7",
            "--output", "")[0], 5)
        self.session.get.assert_not_called()

    def test_force_replaces_only_a_regular_file(self):
        path = self.root / "chosen.bin"
        path.write_bytes(b"original")
        self.assertEqual(self.invoke(
            "attachment", "Example", "--attachment", "7",
            "--output", str(path), "--force")[0], 0)
        self.assertEqual(path.read_bytes(), b"payload")

    def test_symlink_is_rejected_even_with_force(self):
        outside = self.root / "outside"
        outside.write_bytes(b"preserve")
        link = self.root / "link"
        link.symlink_to(outside)
        self.assertEqual(self.invoke(
            "attachment", "Example", "--attachment", "7",
            "--output", str(link), "--force")[0], 5)
        self.assertEqual(outside.read_bytes(), b"preserve")
        self.session.get.assert_not_called()

    def test_context_mismatch_prevents_binary_download(self):
        self.assertEqual(self.invoke(
            "attachment", "Example", "--post", "101",
            "--attachment", "8")[0], 7)
        self.assertEqual(self.responses, [])

    def test_bad_resource_arguments_do_not_contact_board(self):
        for args in (("attachments", "Example"),
                     ("attachments", "Example", "--post", "101",
                      "--message", "5"),
                     ("attachments", "Example", "--message", "5", "--guest"),
                     ("attachments", "Example", "--post", "101",
                      "--folder", "inbox"),
                     ("attachment", "Example", "--attachment", "0")):
            with self.subTest(args=args):
                self.assertEqual(self.invoke(*args)[0], 2)
        self.session.get.assert_not_called()

    def test_guest_download_does_not_authenticate(self):
        self.assertEqual(self.invoke("attachment", "Example", "--attachment",
                                     "7", "--guest")[0], 0)
        self.session.get.assert_called_once()
        self.assertTrue(self.session.get.call_args.args[0].endswith(
            'file.php'))

    def test_existing_directory_filename_is_not_overwritten(self):
        directory = self.root / "downloads"
        directory.mkdir()
        (directory / "café.txt").write_bytes(b"preserve")
        self.assertEqual(self.invoke("attachment", "Example",
                                     "--attachment", "7")[0], 5)
        self.assertEqual((directory / "café.txt").read_bytes(), b"preserve")
        self.responses[0].iter_content.assert_not_called()

    def test_file_appearing_during_transfer_is_not_overwritten(self):
        original = self.get_response
        path = self.root / "late.bin"

        def response(url, **kwargs):
            result = original(url, **kwargs)
            if url.endswith('file.php'):
                def stream():
                    yield b"payload"
                    path.write_bytes(b"appeared during download")
                result.iter_content.return_value = stream()
            return result

        self.session.get.side_effect = response
        self.assertEqual(self.invoke(
            "attachment", "Example", "--attachment", "7",
            "--output", str(path))[0], 5)
        self.assertEqual(path.read_bytes(), b"appeared during download")
        self.assertEqual(list(self.root.glob('.phpbb-tui-*')), [])

    def test_stream_failure_preserves_forced_destination(self):
        original = self.get_response

        def response(url, **kwargs):
            result = original(url, **kwargs)
            if url.endswith('file.php'):
                def interrupted():
                    yield b"partial"
                    raise requests.ConnectionError(SECRET)
                result.iter_content.return_value = interrupted()
            return result

        self.session.get.side_effect = response
        path = self.root / "chosen.bin"
        path.write_bytes(b"preserve")
        self.assertEqual(self.invoke(
            "attachment", "Example", "--attachment", "7",
            "--output", str(path), "--force")[0], 9)
        self.assertEqual(path.read_bytes(), b"preserve")

    def test_all_upload_files_are_checked_before_http(self):
        first = self.fixture("first.txt")
        self.assertEqual(self.invoke(*self.post_args(
            "--attach", first, "--attach", str(self.root / "missing")))[0], 5)
        self.session.get.assert_not_called()
        self.assertEqual(self.uploads, [])

    def test_multiple_uploads_are_carried_into_one_final_post(self):
        first, second = self.fixture("first.txt"), self.fixture("café.txt")
        status, data = self.invoke(*self.post_args(
            "--attach", first, "--attach", second))
        self.assertEqual(status, 0)
        self.assertEqual(data["attachments"], ["first.txt", "café.txt"])
        self.assertEqual(len(self.final_sends()), 1)
        fields = self.final_sends()[0].kwargs["data"]
        self.assertEqual(fields['attachment_data[0][real_filename]'],
                         'first.txt')
        self.assertEqual(fields['attachment_data[1][real_filename]'],
                         'café.txt')

    def test_edit_preserves_existing_attachment_metadata(self):
        original = self.get_response
        self.session.get.side_effect = lambda url, **kw: (
            Mock(text=form(files=("existing.txt",)))
            if url.endswith('posting.php') else original(url, **kw))
        status, data = self.invoke("edit", "Example", "--post", "201",
                                   "--body", "Replacement", "--attach",
                                   self.fixture("new.txt"))
        self.assertEqual(status, 0)
        self.assertEqual(data['attachments'], ['new.txt'])
        self.assertIn('attachment_data[0][real_filename]',
                      self.final_sends()[0].kwargs['data'])

    def test_dry_run_does_not_upload(self):
        status, data = self.invoke(*self.post_args(
            "--attach", self.fixture("first.txt"), "--dry-run"))
        self.assertEqual(status, 0)
        self.assertEqual(data['attachments'], ['first.txt'])
        self.assertEqual(self.uploads, [])
        self.session.post.assert_not_called()

    def test_preview_uploads_without_final_submission(self):
        self.assertEqual(self.invoke(*self.post_args(
            "--attach", self.fixture("first.txt"), "--preview"))[0], 0)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.final_sends(), [])

    def test_disabled_attachments_fail_without_uploading_or_sending(self):
        self.enabled = False
        self.assertEqual(self.invoke(*self.post_args(
            "--attach", self.fixture("first.txt")))[0], 8)
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.final_sends(), [])

    def test_pm_uploads_use_the_same_workflow(self):
        status, data = self.invoke("pm", "Example", "--to", "Alice",
                                   "--subject", "PM", "--body", "Body",
                                   "--attach", self.fixture("first.txt"))
        self.assertEqual(status, 0)
        self.assertEqual(data['attachments'], ['first.txt'])
        self.assertEqual(data['message_id'], 22)
        self.assertEqual(len(self.final_sends()), 1)

    def test_reply_and_pm_reply_accept_uploads(self):
        for args in (("reply", "Example", "--topic", "42", "--body", "Reply"),
                     ("pm-reply", "Example", "--message", "5",
                      "--body", "PM")):
            with self.subTest(args=args):
                self.assertEqual(self.invoke(
                    *args, "--attach", self.fixture("reply.png"))[0], 0)

    def test_upload_network_failure_never_performs_final_submit(self):
        original = self.post_response

        def response(url, **kwargs):
            if 'add_file' in kwargs['data']:
                raise requests.ReadTimeout(SECRET)
            return original(url, **kwargs)

        self.session.post.side_effect = response
        self.assertEqual(self.invoke(*self.post_args(
            "--attach", self.fixture("first.txt")))[0], 9)
        self.assertEqual(self.final_sends(), [])

    def test_unavailable_pm_module_is_not_a_download_success(self):
        response = requests.Response()
        response.status_code = 503
        self.session.get.side_effect = requests.HTTPError(response=response)
        self.assertEqual(self.invoke(
            "attachments", "Example", "--message", "5")[0], 8)

    def test_disabled_pm_attachments_do_not_send_message(self):
        self.enabled = False
        self.assertEqual(self.invoke(
            "pm", "Example", "--to", "Alice", "--subject", "PM",
            "--body", "Body", "--attach", self.fixture("first.txt"))[0], 8)
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.final_sends(), [])

    def test_failed_second_upload_prevents_final_submission(self):
        original = self.post_response

        def response(url, **kwargs):
            files = kwargs.get('files')
            if files and files['fileupload'][0] == 'bad.txt':
                return Mock(text='<p class="error">Upload rejected</p>')
            return original(url, **kwargs)

        self.session.post.side_effect = response
        self.assertEqual(self.invoke(*self.post_args(
            "--attach", self.fixture("first.txt"),
            "--attach", self.fixture("bad.txt")))[0], 4)
        self.assertEqual(len(self.uploads), 1)
        self.assertEqual(self.final_sends(), [])

    def test_unconfirmed_upload_is_not_reported_as_success(self):
        original = self.post_response
        self.session.post.side_effect = lambda url, **kw: (
            Mock(text=form()) if 'add_file' in kw['data']
            else original(url, **kw))
        self.assertEqual(self.invoke(*self.post_args(
            "--attach", self.fixture("first.txt")))[0], 4)
        self.assertEqual(self.final_sends(), [])
