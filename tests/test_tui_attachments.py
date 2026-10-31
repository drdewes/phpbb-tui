"""Exercise inline resolution and the TUI compose loop without a terminal."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from phpbb_tui.board import Entwurf
from phpbb_tui.i18n import TEXTE
from phpbb_tui import entwuerfe, tui_attachments
from phpbb_tui.ui import Oberflaeche


def form(*items):
    """Build synthetic form fields in phpBB's current attachment order."""
    fields = {"subject": "Subject"}
    for index, (aid, name) in enumerate(items):
        fields[f"attachment_data[{index}][attach_id]"] = str(aid)
        fields[f"attachment_data[{index}][real_filename]"] = name
    return Entwurf(fields, "posting.php", {})


class InlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        drafts = patch("phpbb_tui.entwuerfe.ORDNER", Path(self.temp.name))
        drafts.start()
        self.addCleanup(drafts.stop)

    def test_existing_tags_follow_ids_when_uploads_reorder_files(self):
        draft = form((12, "first.png"), (34, "same.png"))
        body = tui_attachments.editor_body(
            draft, "[attachment=0]first.png[/attachment]")
        draft.felder = form((34, "same.png"), (12, "first.png")).felder
        self.assertEqual(tui_attachments.render(draft, body),
                         "[attachment=1]first.png[/attachment]")

    def test_duplicate_names_and_markdown_sensitive_names(self):
        draft = form((12, "a_b_[x].png"), (34, "a_b_[x].png"))
        self.assertEqual(
            tui_attachments.render(draft, "{{attach:id34}} {{attach:id12}}"),
            "[attachment=1]a_b_&#91;x&#93;.png[/attachment] "
            "[attachment=0]a_b_&#91;x&#93;.png[/attachment]")

    def test_removed_and_malformed_references_fail(self):
        for body in ("{{attach:id99}}", "{{attach:bad}}", "{{attach:id12"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                tui_attachments.render(form((12, "a.png")), body)

    def test_plain_markdown_preserves_existing_conversion(self):
        self.assertEqual(tui_attachments.render(form(), "**hello**"),
                         "[b]hello[/b]")

    def interface(self, draft):
        board = Mock()
        board.name = "test"
        board.submit.return_value = '<div id="message">Stored</div>'
        board._fehlertext.return_value = ""
        board.submission_success.return_value = (True, "Sent")
        board._redirected_post_id.return_value = 101
        board.pm_submission_id.return_value = 42
        board.angehaengte_dateien.side_effect = lambda e: [
            name for _, name in tui_attachments.attachment_fields(e).values()]
        board.preview_text.return_value = "Preview"
        board.submit_result.return_value = (True, "Sent")
        ui = Oberflaeche(Mock(), board, Mock())
        ui.scr.getmaxyx.return_value = (24, 80)
        ui.warten = Mock()
        ui.bestaetigen = Mock(return_value=True)
        ui.liste = Mock(return_value=("\n", 0))
        return ui, board

    def test_select_edit_preview_and_send_for_posts_and_pms(self):
        for target in ("posting.php", "ucp.php"):
            with self.subTest(target=target):
                draft = form((12, "file.png"))
                draft.ziel = target
                ui, board = self.interface(draft)
                ui._editor = Mock(side_effect=[
                    ("Subject", "Before"),
                    ("Subject", "Before {{attach:id12}} after")])
                ui.seite_zeigen = Mock(side_effect=["i", "a"])
                self.assertTrue(ui._verfassen(draft))
                self.assertIn("{{attach:id12}}", ui._editor.call_args.args[1])
                self.assertEqual(board.submit.call_count, 3)
                final = board.submit.call_args
                self.assertEqual(final.args[2],
                                 "Before [attachment=0]file.png[/attachment] "
                                 "after")
                self.assertFalse(final.kwargs)

    def test_upload_reordering_updates_preview_and_final_index(self):
        draft = form((12, "original.png"))
        draft.felder["message"] = (
            "[attachment=0]original.png[/attachment]")
        ui, board = self.interface(draft)
        ui._editor = Mock(return_value=("Subject", "{{attach:id12}}"))
        ui._anhaengen = Mock(side_effect=lambda e: e.felder.update(
            form((34, "new.png"), (12, "original.png")).felder))
        ui.seite_zeigen = Mock(side_effect=["d", "a"])
        self.assertTrue(ui._verfassen(draft))
        self.assertIn("{{attach:id12}}", ui._editor.call_args.args[1])
        self.assertEqual(board.submit.call_args.args[2],
                         "[attachment=1]original.png[/attachment]")

    def test_cancel_selection_stays_in_preview(self):
        draft = form((12, "file.png"))
        ui, board = self.interface(draft)
        ui._editor = Mock(return_value=("Subject", "Body"))
        ui.liste.return_value = ("q", 0)
        ui.seite_zeigen = Mock(side_effect=["i", "q"])
        self.assertFalse(ui._verfassen(draft))
        ui._editor.assert_called_once()
        self.assertTrue(all(c.kwargs.get("preview")
                            for c in board.submit.call_args_list))

    def test_removed_reference_never_reaches_server(self):
        ui, board = self.interface(form())
        ui._editor = Mock(side_effect=[
            ("Subject", "{{attach:id99}}"), ("Subject", "")])
        self.assertFalse(ui._verfassen(form()))
        board.submit.assert_not_called()

    def test_corrected_inline_reference_is_saved_on_uncertain_submission(self):
        draft = form((12, "file.png"))
        ui, board = self.interface(draft)
        ui._editor = Mock(side_effect=[
            ("Subject", "{{attach:id99}}"),
            ("Subject", "Corrected {{attach:id12}}")])
        ui.seite_zeigen = Mock(side_effect=["a", "q"])
        with patch("phpbb_tui.tui_submission.submit",
                   return_value=("uncertain", "Unknown")):
            self.assertFalse(ui._verfassen(draft))
        saved = entwuerfe.laden("test", draft.ziel, draft.params)
        self.assertEqual(saved[1], "Corrected {{attach:id12}}")
        self.assertIn("inline", ui._editor.call_args.args[2].lower())

    def test_inline_help_fits_standard_terminal_in_both_languages(self):
        for language in ("en", "de"):
            help_text = TEXTE[language]["hilfe_vorschau_inline"]
            self.assertLessEqual(len(help_text), 78)
            self.assertIn("i ", help_text)

    def test_no_attachments_has_no_inline_action(self):
        ui, board = self.interface(form())
        ui._editor = Mock(return_value=("Subject", "Body"))
        ui.seite_zeigen = Mock(return_value="q")
        self.assertFalse(ui._verfassen(form()))
        self.assertNotIn("i", ui.seite_zeigen.call_args.kwargs["tasten"])
