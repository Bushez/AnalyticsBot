import asyncio
import copy
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from analyticsbot.gui_config import DEFAULT_COLUMNS, default_settings, load_settings, save_settings


@unittest.skipIf(importlib.util.find_spec("PySide6") is None, "PySide6 is not installed")
class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.settings = default_settings()
        self.windows = []

    def tearDown(self):
        for window in self.windows:
            if hasattr(window, "timer"):
                window.timer.stop()
                window.tray.hide()
            window.deleteLater()
        self.app.processEvents()
        self.directory.cleanup()

    def wizard(self):
        from analyticsbot.gui_setup import SetupWizard
        wizard = SetupWizard(self.root, self.settings)
        self.windows.append(wizard)
        return wizard

    def connected_settings(self):
        (self.root / "telegram.session").touch()
        (self.root / "google-service-account.json").write_text("{}")
        self.settings.update(api_id="12345", api_hash="hash", telegram_verified=True, telegram_account="Test",
                             credentials="google-service-account.json", google_email="service@example.test")

    def fill_channel(self, wizard):
        wizard.channel.source.setText("@test_channel")
        wizard.channel.spreadsheet.setText("https://docs.google.com/spreadsheets/d/test_id/edit")
        wizard.channel.tabs_loaded(["Статистика", "Архив"])

    def wait_for(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.app.processEvents()
        self.assertTrue(predicate())

    def test_first_launch_hides_code_and_password_and_blocks_next(self):
        wizard = self.wizard()
        self.assertEqual(wizard.stack.currentIndex(), 0)
        self.assertEqual(wizard.heading.text(), "Подключим ваш аккаунт")
        self.assertTrue(wizard.telegram.account.isHidden())
        self.assertTrue(wizard.telegram.status.isHidden())
        self.assertTrue(wizard.telegram.help_text.isHidden())
        self.assertEqual(wizard.telegram.phone_button.objectName(), "primary")
        self.assertFalse(hasattr(wizard, "import_toggle"))
        self.assertFalse(wizard.next_button.isEnabled())
        self.assertTrue(wizard.telegram.code_box.isHidden())
        self.assertTrue(wizard.telegram.password_box.isHidden())
        self.assertTrue(wizard.telegram.qr_image.isHidden())

    def test_login_status_appears_only_after_attempt(self):
        wizard = self.wizard()
        wizard.telegram.begin("phone")
        self.assertFalse(wizard.telegram.status.isHidden())
        self.assertTrue(wizard.telegram.account.isHidden())
        wizard.telegram.account_received("Test")
        wizard.telegram.result(True, "Telegram подключён: Test")
        self.assertTrue(wizard.telegram.status.isHidden())
        self.assertFalse(wizard.telegram.account.isHidden())
        self.assertEqual(wizard.telegram.account.text(), "Подключён: Test")
        self.assertNotEqual(wizard.telegram.phone_button.objectName(), "primary")

    def test_auth_fields_appear_only_when_requested(self):
        wizard = self.wizard()
        wizard.telegram.stage("code", "code sent")
        self.assertFalse(wizard.telegram.code_box.isHidden())
        self.assertTrue(wizard.telegram.password_box.isHidden())
        wizard.telegram.stage("password", "password needed")
        self.assertTrue(wizard.telegram.code_box.isHidden())
        self.assertFalse(wizard.telegram.password_box.isHidden())
        wizard.telegram.code.setText("private code")
        wizard.telegram.password.setText("private password")
        wizard.changed()
        stored = (self.root / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("private code", stored)
        self.assertNotIn("private password", stored)

    def test_progress_and_channel_draft_survive_restart(self):
        self.connected_settings()
        wizard = self.wizard()
        wizard.next()
        self.assertEqual(wizard.heading.text(), "Подключим вашу таблицу")
        self.assertTrue(wizard.google.help_text.isHidden())
        wizard.google.help_toggle.setChecked(True)
        self.assertFalse(wizard.google.help_text.isHidden())
        wizard.next()
        self.assertEqual(wizard.heading.text(), "Соединим Telegram и вашу таблицу")
        self.fill_channel(wizard)
        self.assertEqual(load_settings(self.root / "settings.json")["setup_step"], 2)
        self.settings = load_settings(self.root / "settings.json")
        resumed = self.wizard()
        self.assertEqual(resumed.stack.currentIndex(), 2)
        self.assertEqual(resumed.channel.source.text(), "@test_channel")
        self.assertFalse(resumed.next_button.isEnabled())

    def test_finish_requires_verified_channel_and_marks_complete(self):
        self.connected_settings()
        wizard = self.wizard()
        wizard.next()
        wizard.next()
        self.fill_channel(wizard)
        self.assertFalse(wizard.next_button.isEnabled())
        wizard.next()
        self.assertFalse(self.settings["setup_complete"])
        wizard.channel.result(True, "checked")
        self.assertTrue(wizard.next_button.isEnabled())
        wizard.next()
        stored = load_settings(self.root / "settings.json")
        self.assertTrue(stored["setup_complete"], wizard.hint.text())
        self.assertTrue(stored["start_service"])
        self.assertEqual(stored["channels"][0]["worksheet"], "Статистика")

    def test_editing_verified_values_invalidates_finish(self):
        self.connected_settings()
        wizard = self.wizard()
        wizard.next()
        wizard.next()
        self.fill_channel(wizard)
        wizard.channel.result(True, "checked")
        self.assertTrue(wizard.next_button.isEnabled())
        wizard.channel.source.setText("@other_channel")
        self.assertFalse(wizard.next_button.isEnabled())
        self.assertFalse(self.settings["channel_verified"])

    def test_api_change_invalidates_previous_channel_verification(self):
        self.connected_settings()
        wizard = self.wizard()
        wizard.next()
        wizard.next()
        self.fill_channel(wizard)
        wizard.channel.result(True, "checked")
        wizard.telegram.api_id.setText("999")
        wizard.telegram.result(True, "reauthorized")
        self.assertFalse(wizard.next_button.isEnabled())

    def test_google_key_cancel_preserves_original_file_and_settings(self):
        from analyticsbot.gui_setup import ConnectionDialog
        self.connected_settings()
        original = (self.root / "google-service-account.json").read_bytes()
        dialog = ConnectionDialog(self.root, self.settings, "google")
        self.windows.append(dialog)
        new_key = self.root / "new-source.json"
        new_key.write_text("new key")
        with patch("analyticsbot.gui_setup.QFileDialog.getOpenFileName", return_value=(str(new_key), "")), patch("analyticsbot.gui_setup.Credentials.from_service_account_file", return_value=SimpleNamespace(service_account_email="new@example.test")):
            dialog.panel.choose()
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.reject()
        self.assertEqual((self.root / "google-service-account.json").read_bytes(), original)
        self.assertEqual(self.settings["google_email"], "service@example.test")
        self.assertFalse((self.root / "google-key-pending.json").exists())

    def test_main_window_has_no_initial_auth_form_and_autostarts(self):
        from analyticsbot.gui import MainWindow
        self.connected_settings()
        self.settings["setup_complete"] = True
        with patch.object(MainWindow, "start_service") as start:
            window = MainWindow(self.root, self.settings)
            self.windows.append(window)
            self.app.processEvents()
            start.assert_called_once()
        self.assertFalse(hasattr(window, "api_id"))
        self.assertFalse(hasattr(window, "code"))
        self.assertEqual(window.telegram_summary.text(), "Test")

    def test_first_launch_routes_to_wizard_before_main_window(self):
        from analyticsbot.gui import main
        from PySide6.QtWidgets import QDialog
        with patch("analyticsbot.gui.QApplication"), patch("analyticsbot.gui.data_dir", return_value=self.root), patch("analyticsbot.gui_setup.SetupWizard.exec", return_value=QDialog.DialogCode.Rejected), patch("analyticsbot.gui.MainWindow") as window:
            self.assertEqual(main(), 0)
        window.assert_not_called()

    def test_completed_setup_skips_wizard_on_next_launch(self):
        from analyticsbot.gui import main
        self.settings["setup_complete"] = True
        save_settings(self.root / "settings.json", self.settings)
        with patch("analyticsbot.gui.QApplication") as app, patch("analyticsbot.gui.data_dir", return_value=self.root), patch("analyticsbot.gui_setup.SetupWizard.exec", side_effect=AssertionError("wizard must not reopen")), patch("analyticsbot.gui.MainWindow") as window:
            app.return_value.exec.return_value = 0
            self.assertEqual(main(), 0)
        window.assert_called_once()

    def test_verified_new_google_key_is_committed(self):
        from analyticsbot.gui_setup import ConnectionDialog
        self.connected_settings()
        dialog = ConnectionDialog(self.root, self.settings, "google")
        self.windows.append(dialog)
        (self.root / "google-key-pending.json").write_text("replacement")
        dialog.draft.update(credentials="google-key-pending.json", google_email="new@example.test", google_verified=False)
        dialog.changed()
        self.assertFalse(dialog.save_button.isEnabled())
        dialog.panel.result(True, "checked")
        self.assertTrue(dialog.save_button.isEnabled())
        dialog.commit()
        self.assertEqual((self.root / "google-service-account.json").read_text(), "replacement")
        self.assertEqual(self.settings["google_email"], "new@example.test")
        self.assertFalse((self.root / "google-key-pending.json").exists())

    def test_channel_check_reads_telegram_and_selected_google_tab(self):
        from analyticsbot.gui_workers import ChannelCheckWorker
        config = {"api_id": 123, "api_hash": "hash", "session": "unused", "database": "unused", "credentials": "key.json"}
        channel = {"source": "@test_channel", "sheet": {"spreadsheet_id": "sheet", "worksheet": "Tab", "columns": DEFAULT_COLUMNS}}
        worker = ChannelCheckWorker(config, channel)
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                                 get_entity=AsyncMock(return_value=SimpleNamespace(broadcast=True)), get_messages=AsyncMock())
        api = MagicMock()
        api.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"title": "Tab", "gridProperties": {"columnCount": 26}}}]}
        with patch("analyticsbot.gui_workers.TelegramClient", return_value=client), patch("analyticsbot.gui_workers.sheets.create_api", return_value=api):
            asyncio.run(worker.check())
        client.get_messages.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        self.assertEqual(api.spreadsheets().get.call_args.kwargs["spreadsheetId"], "sheet")

    def test_threaded_channel_check_enables_finish_only_after_success(self):
        self.connected_settings()
        wizard = self.wizard()
        wizard.next()
        wizard.next()
        self.fill_channel(wizard)
        with patch("analyticsbot.gui_workers.ChannelCheckWorker.check", AsyncMock()):
            wizard.channel.verify()
            self.wait_for(lambda: not wizard.channel.busy() and wizard.channel.checked)
        self.assertTrue(wizard.next_button.isEnabled())

    def test_worksheet_list_comes_from_google_metadata(self):
        self.connected_settings()
        wizard = self.wizard()
        wizard.channel.spreadsheet.setText("https://docs.google.com/spreadsheets/d/test_id/edit")
        api = MagicMock()
        api.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"title": "Actual tab"}}]}
        with patch("analyticsbot.gui_workers.sheets.create_api", return_value=api):
            wizard.channel.load_tabs()
            self.wait_for(lambda: not wizard.channel.busy() and wizard.channel.worksheet.currentText() == "Actual tab")
        self.assertFalse(wizard.channel.checked)

    def test_closing_during_login_cancels_worker_and_saves_progress(self):
        self.settings.update(api_id="123", api_hash="hash", phone="+70000000000")
        wizard = self.wizard()
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
                                 send_code_request=AsyncMock(return_value=SimpleNamespace(type=object())))
        with patch("analyticsbot.gui_workers.TelegramClient", return_value=client):
            wizard.telegram.begin("phone")
            self.wait_for(lambda: not wizard.telegram.code_box.isHidden())
            wizard.reject()
            self.wait_for(lambda: not wizard.busy())
            wizard.reject()
        self.assertFalse(load_settings(self.root / "settings.json")["setup_complete"])
        client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
