import asyncio
import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from analyticsbot.gui_config import DEFAULT_COLUMNS, channel_source, default_settings, import_cli, load_settings, runtime_config, save_settings, spreadsheet_id


class SettingsTests(unittest.TestCase):
    def test_settings_roundtrip_and_portable_runtime_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "key.json").write_text("{}")
            settings = default_settings()
            settings.update(api_id="12345", api_hash="testhash", credentials="key.json", channels=[{
                "source": "https://t.me/test_channel", "spreadsheet": "https://docs.google.com/spreadsheets/d/test_id/edit",
                "worksheet": "Лист1", "columns": copy.deepcopy(DEFAULT_COLUMNS),
            }])
            save_settings(root / "settings.json", settings)
            config = runtime_config(load_settings(root / "settings.json"), root)
            self.assertEqual(config["channels"][0]["source"], "@test_channel")
            self.assertEqual(config["channels"][0]["sheet"]["spreadsheet_id"], "test_id")
            self.assertEqual(config["credentials"], str(root / "key.json"))
            self.assertEqual(config["database"], str(root / "jobs.sqlite3"))

    def test_rejects_unexpected_links(self):
        with self.assertRaises(ValueError):
            channel_source("https://example.com/channel")
        with self.assertRaises(ValueError):
            spreadsheet_id("https://example.com/spreadsheets/d/abc")
        self.assertEqual(channel_source("https://t.me/+private_invite"), "https://t.me/+private_invite")

    def test_rejects_duplicate_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "key.json").write_text("{}")
            columns = copy.deepcopy(DEFAULT_COLUMNS)
            columns["views"] = columns["reactions"]
            settings = default_settings()
            settings.update(api_id="123", api_hash="test", credentials="key.json", channels=[{
                "source": "@channel", "spreadsheet": "abcd", "worksheet": "Sheet", "columns": columns}])
            with self.assertRaises(ValueError):
                runtime_config(settings, root)

    def test_import_copies_existing_database_and_session_without_overwriting(self):
        import sqlite3
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "old", Path(directory) / "portable"
            (source / "data").mkdir(parents=True)
            destination.mkdir()
            (source / ".env").write_text("TELEGRAM_API_ID=123\nTELEGRAM_API_HASH=hash\nGOOGLE_APPLICATION_CREDENTIALS=key.json\n", encoding="utf-8")
            (source / "key.json").write_text("{}")
            config = {"channels": [{"source": "@channel", "sheet": {"spreadsheet_id": "sheet", "worksheet": "Tab", "columns": DEFAULT_COLUMNS}}]}
            (source / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
            for name in ("jobs.sqlite3", "telegram.session"):
                connection = sqlite3.connect(source / "data" / name)
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('kept')")
                connection.commit()
                connection.close()
            imported = import_cli(source, destination)
            self.assertEqual(imported["channels"][0]["source"], "@channel")
            for name in ("jobs.sqlite3", "telegram.session"):
                connection = sqlite3.connect(destination / name)
                self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], "kept")
                connection.close()
            with self.assertRaises(ValueError):
                import_cli(source, destination)


try:
    import PySide6
except ImportError:
    PySide6 = None


@unittest.skipIf(PySide6 is None, "PySide6 optional dependency is not installed")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_first_launch_and_channel_dialog(self):
        from PySide6.QtWidgets import QSystemTrayIcon
        from analyticsbot.gui import ChannelDialog, MainWindow
        with tempfile.TemporaryDirectory() as directory, patch.object(QSystemTrayIcon, "show"):
            window = MainWindow(Path(directory), default_settings())
            self.assertEqual(window.status.text(), "Остановлен")
            self.assertEqual(window.channels_table.rowCount(), 0)
            dialog = ChannelDialog(window)
            self.assertEqual(dialog.value()["columns"], DEFAULT_COLUMNS)
            window.tray.hide()
            window.timer.stop()
            window.deleteLater()

    def test_worker_passes_service_config_and_uses_lock(self):
        from analyticsbot.gui_workers import ServiceWorker
        with tempfile.TemporaryDirectory() as directory:
            config = {"database": str(Path(directory) / "jobs.sqlite3")}
            worker = ServiceWorker(config)
            fake = AsyncMock()
            with patch("analyticsbot.gui_workers.run", fake):
                worker.run()
            fake.assert_awaited_once()
            self.assertIs(fake.await_args.args[0], config)
            self.assertTrue(callable(fake.await_args.kwargs["on_started"]))

    def test_backfill_worker_uses_existing_service_entrypoint(self):
        from analyticsbot.gui_workers import ServiceWorker
        with tempfile.TemporaryDirectory() as directory:
            config = {"database": str(Path(directory) / "jobs.sqlite3")}
            worker = ServiceWorker(config, "@channel", 10)
            fake = AsyncMock()
            with patch("analyticsbot.gui_workers.run", fake):
                worker.run()
            self.assertEqual(fake.await_args.kwargs, {"backfill_channel": "@channel", "backfill_count": 10})

    def test_worker_stops_background_coroutine(self):
        from analyticsbot.gui_workers import ServiceWorker
        with tempfile.TemporaryDirectory() as directory:
            worker = ServiceWorker({"database": str(Path(directory) / "jobs.sqlite3")})
            entered = threading.Event()

            async def service(config, on_started):
                entered.set()
                await asyncio.Event().wait()

            with patch("analyticsbot.gui_workers.run", service):
                worker.start()
                self.assertTrue(entered.wait(3))
                worker.stop()
                self.assertTrue(worker.wait(3000))

    def test_auth_code_and_two_factor_use_telethon_session(self):
        from types import SimpleNamespace
        from telethon import errors
        from analyticsbot.gui_workers import TelegramWorker
        worker = TelegramWorker(123, "hash", "test.session", "test.db", "+70000000000")
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
                                 send_code_request=AsyncMock(return_value=SimpleNamespace(type=object())),
                                 sign_in=AsyncMock(side_effect=[errors.SessionPasswordNeededError(None), None]),
                                 get_me=AsyncMock(return_value=SimpleNamespace(first_name="Test", id=123)))
        worker.answer("12345")
        worker.answer("password")
        with patch("analyticsbot.gui_workers.TelegramClient", return_value=client):
            asyncio.run(worker._work())
        self.assertEqual(client.sign_in.await_count, 2)
        self.assertEqual(client.sign_in.await_args.kwargs, {"password": "password"})
        client.disconnect.assert_awaited_once()

    def test_qr_auth_refreshes_expired_code_and_connects(self):
        from types import SimpleNamespace
        from analyticsbot.gui_workers import TelegramWorker
        worker = TelegramWorker(123, "hash", "test.session", "test.db", qr_mode=True)
        qr = SimpleNamespace(url="tg://login?token=secret", wait=AsyncMock(side_effect=[asyncio.TimeoutError(), None]))
        client = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False),
                                 qr_login=AsyncMock(return_value=qr), get_me=AsyncMock(return_value=SimpleNamespace(first_name="Test", id=123)))
        with patch("analyticsbot.gui_workers.TelegramClient", return_value=client):
            asyncio.run(worker._work())
        self.assertEqual(client.qr_login.await_count, 2)
        client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
