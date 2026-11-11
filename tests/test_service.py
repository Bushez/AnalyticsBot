import asyncio
import copy
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from analyticsbot import db, sheets, telegram
from analyticsbot.config import load_config
from analyticsbot.main import backfill, login, login_qr, minimum_row, process_jobs, reserve_row


TARGET = {
    "spreadsheet_id": "spreadsheet",
    "worksheet": "Stats' sheet",
    "start_row": 2,
    "columns": {"post_key": "Z", "views": "D", "reactions": "E", "forwards": "F", "comments": "G", "subscribers": "H"},
}
ENTITY = SimpleNamespace(id=123, title="Channel", username="channel")


def message(message_id=10, grouped_id=None, date=None, views=100, reactions=5):
    return SimpleNamespace(
        id=message_id, grouped_id=grouped_id, post=True, action=None,
        date=date or datetime.now(timezone.utc) - timedelta(hours=25),
        views=views, forwards=3, replies=SimpleNamespace(replies=2),
        reactions=SimpleNamespace(results=[SimpleNamespace(count=reactions), SimpleNamespace(count=1)]),
    )


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "jobs.sqlite3"
        self.database = db.connect(self.path)
        db.channel_state(self.database, ENTITY.id, 1)

    def tearDown(self):
        self.database.close()
        self.directory.cleanup()

    def add(self, msg=None):
        db.remember(self.database, ENTITY.id, msg or message())
        return db.due_jobs(self.database, datetime.now(timezone.utc).timestamp())[0]

    def test_album_and_repeated_history_are_one_job(self):
        self.add(message(11, 999))
        self.add(message(10, 999))
        self.add(message(11, 999))
        jobs = db.due_jobs(self.database, datetime.now(timezone.utc).timestamp())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["post_key"], "123:g:999")
        self.assertEqual(json.loads(jobs[0]["message_ids"]), [10, 11])
        self.assertEqual(jobs[0]["post_id"], 10)
        self.assertEqual(db.channel_state(self.database, ENTITY.id, 999)["last_message_id"], 11)

    def test_channel_id_is_part_of_unique_key(self):
        self.add()
        db.channel_state(self.database, 456, 1)
        db.remember(self.database, 456, message())
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_pending_and_cursor_survive_restart(self):
        self.add()
        self.database.close()
        self.database = db.connect(self.path)
        self.assertEqual(len(db.due_jobs(self.database, datetime.now(timezone.utc).timestamp())), 1)
        self.assertEqual(db.channel_state(self.database, 123, 999)["initial_since"], 1)
        self.assertEqual(db.channel_state(self.database, 123, 999)["last_message_id"], 10)

    def test_no_collection_before_24_hours(self):
        db.remember(self.database, 123, message(date=datetime.now(timezone.utc) - timedelta(hours=23)))
        self.assertEqual(db.due_jobs(self.database, datetime.now(timezone.utc).timestamp()), [])
        self.database.execute("UPDATE jobs SET due_at = 0")
        self.database.commit()
        self.assertEqual(db.due_jobs(self.database, datetime.now(timezone.utc).timestamp()), [])

    def test_snapshot_is_immutable_and_done_not_rescheduled(self):
        job = self.add()
        db.save_snapshot(self.database, job["post_key"], {"record": 1})
        db.save_snapshot(self.database, job["post_key"], {"record": 2})
        self.assertEqual(json.loads(self.database.execute("SELECT snapshot FROM jobs").fetchone()[0]), {"record": 1})
        db.finish(self.database, job["post_key"], "done", 2)
        db.remember(self.database, 123, message())
        self.assertEqual(db.due_jobs(self.database, datetime.now(timezone.utc).timestamp()), [])

    def test_service_messages_only_advance_cursor(self):
        msg = message()
        msg.action = object()
        db.remember(self.database, 123, msg)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(db.channel_state(self.database, 123, 999)["last_message_id"], 10)

    def test_single_instance_lock(self):
        with db.single_instance(self.path):
            with self.assertRaises(RuntimeError):
                with db.single_instance(self.path):
                    pass
        with db.single_instance(self.path):
            pass

    def test_failed_delivery_reuses_snapshot_and_reserved_row(self):
        job = self.add()
        config = {"retry_seconds": 1}
        channels = {123: (ENTITY, {"sheet": TARGET})}
        record = telegram.make_record(job, ENTITY, [message()], 200, datetime.now(timezone.utc))
        collect = AsyncMock(return_value=record)
        with patch("analyticsbot.main.telegram.collect", collect), patch("analyticsbot.main.sheets.inspect", return_value=(2, False)), patch("analyticsbot.main.sheets.write", side_effect=TimeoutError("lost response")):
            with self.assertLogs("analyticsbot.main", level="ERROR"):
                asyncio.run(process_jobs(None, self.database, None, channels, config))
        saved = self.database.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(saved["sheet_row"], 2)
        self.assertEqual(json.loads(saved["snapshot"])["record"], record)
        self.assertEqual(minimum_row(self.database, TARGET), 3)
        self.database.execute("UPDATE jobs SET next_attempt = 0")
        self.database.commit()
        with patch("analyticsbot.main.telegram.collect", collect), patch("analyticsbot.main.sheets.inspect", return_value=(2, True)) as inspect, patch("analyticsbot.main.sheets.write") as write:
            asyncio.run(process_jobs(None, self.database, None, {}, config))
        self.assertEqual(collect.await_count, 1)
        self.assertEqual(inspect.call_args.args[3], 2)
        write.assert_not_called()
        self.assertEqual(self.database.execute("SELECT status FROM jobs").fetchone()[0], "done")

    def test_retry_before_success_delivers_identical_record(self):
        job = self.add()
        record = telegram.make_record(job, ENTITY, [message()], 200, datetime.now(timezone.utc))
        snapshot = {"record": record, "target": TARGET}
        db.save_snapshot(self.database, job["post_key"], snapshot)
        reserve_row(self.database, job["post_key"], 4)
        with patch("analyticsbot.main.telegram.collect", AsyncMock()) as collect, patch("analyticsbot.main.sheets.inspect", return_value=(4, False)), patch("analyticsbot.main.sheets.write") as write:
            asyncio.run(process_jobs(None, self.database, None, {}, {"retry_seconds": 1}))
        collect.assert_not_awaited()
        self.assertEqual(write.call_args.args[2:], (record, 4))

    def test_deleted_post_is_terminal(self):
        self.add()
        with patch("analyticsbot.main.telegram.collect", AsyncMock(return_value=None)), patch("analyticsbot.main.sheets.write") as write:
            with self.assertLogs("analyticsbot.main", level="WARNING"):
                asyncio.run(process_jobs(None, self.database, None, {123: (ENTITY, {"sheet": TARGET})}, {"retry_seconds": 1}))
        write.assert_not_called()
        self.assertEqual(self.database.execute("SELECT status FROM jobs").fetchone()[0], "deleted")

    def test_pending_snapshot_layout_changes_without_changing_stats(self):
        job = self.add()
        original = {"record": {"views": 42}, "target": copy.deepcopy(TARGET)}
        db.save_snapshot(self.database, job["post_key"], original)
        updated = copy.deepcopy(TARGET)
        updated["columns"]["views"] = "C"
        db.update_pending_layout(self.database, [updated])
        snapshot = json.loads(self.database.execute("SELECT snapshot FROM jobs").fetchone()[0])
        self.assertEqual(snapshot["record"], {"views": 42})
        self.assertEqual(snapshot["target"], updated)

    def test_reserved_snapshot_layout_cannot_be_changed(self):
        job = self.add()
        db.save_snapshot(self.database, job["post_key"], {"record": {}, "target": TARGET})
        reserve_row(self.database, job["post_key"], 2)
        changed = copy.deepcopy(TARGET)
        changed["columns"]["views"] = "C"
        with self.assertRaises(RuntimeError):
            db.update_pending_layout(self.database, [changed])

    def test_scan_resumes_without_lookback_limit(self):
        self.add()
        seen = {}

        async def history(entity, **options):
            seen.update(options)
            yield message(11)
            yield message(12)

        client = SimpleNamespace(iter_messages=history)
        asyncio.run(telegram.scan(client, self.database, ENTITY, 48))
        self.assertEqual(seen, {"min_id": 10, "reverse": True})
        self.assertEqual(db.channel_state(self.database, 123, 1)["last_message_id"], 12)
        self.assertEqual(self.database.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)

    def test_backfill_skips_sheet_duplicates_and_preserves_history_cursor(self):
        entity = SimpleNamespace(id=123, title="Channel", username="channel", broadcast=True)
        self.database.execute("UPDATE channels SET last_message_id = 5 WHERE channel_id = 123")
        self.database.commit()
        now = datetime.now(timezone.utc)
        published = now - timedelta(hours=25)
        posts = [("123:m:10", [message(10, date=published)]), ("123:m:11", [message(11, date=published)])]
        client = SimpleNamespace(get_entity=AsyncMock(return_value=entity))
        async def collect(_, job, __):
            return telegram.make_record(job, entity, [message(job["post_id"], date=published)], 1000, now)
        with patch("analyticsbot.main.telegram.recent_posts", AsyncMock(return_value=posts)), patch("analyticsbot.main.telegram.collect", side_effect=collect) as collect_mock, patch("analyticsbot.main.sheets.inspect", side_effect=[(2, True), (3, False), (3, False)]), patch("analyticsbot.main.sheets.write") as write:
            asyncio.run(backfill(client, self.database, None, {"source": "@channel", "sheet": TARGET}, 2, {"retry_seconds": 1}))
        self.assertEqual(collect_mock.await_count, 1)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(db.channel_state(self.database, 123, 1)["last_message_id"], 5)
        job = self.database.execute("SELECT status FROM jobs WHERE post_key = '123:m:11'").fetchone()
        self.assertEqual(job["status"], "done")
        self.assertIsNone(self.database.execute("SELECT * FROM jobs WHERE post_key = '123:m:10'").fetchone())

    def test_backfill_restores_deleted_sheet_row_from_saved_snapshot(self):
        entity = SimpleNamespace(id=123, title="Channel", username="channel", broadcast=True)
        job = self.add()
        record = telegram.make_record(job, entity, [message()], 1000, datetime.now(timezone.utc))
        db.save_snapshot(self.database, job["post_key"], {"record": record, "target": TARGET})
        db.finish(self.database, job["post_key"], "done", 2)
        client = SimpleNamespace(get_entity=AsyncMock(return_value=entity))
        with patch("analyticsbot.main.telegram.recent_posts", AsyncMock(return_value=[(job["post_key"], [message()])])), patch("analyticsbot.main.telegram.collect", AsyncMock()) as collect, patch("analyticsbot.main.sheets.inspect", side_effect=[(2, False), (2, False)]), patch("analyticsbot.main.sheets.write") as write:
            asyncio.run(backfill(client, self.database, None, {"source": "@channel", "sheet": TARGET}, 1, {"retry_seconds": 1}))
        collect.assert_not_awaited()
        self.assertEqual(write.call_args.args[2], record)
        self.assertEqual(self.database.execute("SELECT status FROM jobs").fetchone()[0], "done")


class SheetsTests(unittest.TestCase):
    def api_with_values(self, **values):
        api = MagicMock()
        api.spreadsheets().values().batchGet().execute.return_value = {
            "valueRanges": [{"values": values.get(field, [])} for field in TARGET["columns"]]
        }
        return api

    def test_existing_key_prevents_duplicate(self):
        api = self.api_with_values(post_key=[["other"], ["123:m:10"]])
        self.assertEqual(sheets.inspect(api, TARGET, {"post_key": "123:m:10"}), (3, True))

    def test_next_row_uses_all_mapped_columns(self):
        api = self.api_with_values(views=[[100], [], ["=1+2"]])
        self.assertEqual(sheets.inspect(api, TARGET, {"post_key": "new"}), (5, False))
        self.assertIn("'Stats'' sheet'!Z2:Z", api.spreadsheets().values().batchGet.call_args.kwargs["ranges"])

    def test_pending_reservations_are_not_reused(self):
        api = self.api_with_values()
        self.assertEqual(sheets.inspect(api, TARGET, {"post_key": "new"}, minimum_row=9), (9, False))

    def test_ambiguous_write_reuses_empty_reserved_row(self):
        api = self.api_with_values(post_key=[["other"]])
        self.assertEqual(sheets.inspect(api, TARGET, {"post_key": "new"}, reserved_row=7), (7, False))

    def test_occupied_reservation_is_never_overwritten(self):
        api = self.api_with_values(views=[["=SUM(A1:A9)"]])
        with self.assertRaises(RuntimeError):
            sheets.inspect(api, TARGET, {"post_key": "new"}, reserved_row=2)

    def test_write_is_atomic_sparse_and_preserves_format(self):
        api = MagicMock()
        api.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {
            "sheetId": 7, "title": TARGET["worksheet"], "gridProperties": {"rowCount": 2, "columnCount": 26}
        }}]}
        record = {field: 1 for field in TARGET["columns"]}
        record.update(post_key="123:m:10", post_id=10, url="https://t.me/channel/10", published_at="2026-01-01T00:00:00+00:00", age_hours=25)
        sheets.write(api, TARGET, record, 3)
        requests = api.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"]
        self.assertEqual(requests[0], {"appendDimension": {"sheetId": 7, "dimension": "ROWS", "length": 1}})
        self.assertEqual(len(requests), len(TARGET["columns"]) + 1)
        updates = [item["updateCells"] for item in requests[1:]]
        self.assertEqual({update["start"]["columnIndex"] for update in updates}, {25, 3, 4, 5, 6, 7})
        self.assertTrue(all(update["fields"] in ("userEnteredValue", "userEnteredValue,note") for update in updates))
        self.assertIn("25", updates[1]["rows"][0]["values"][0]["note"])

    def test_display_values_and_delay_threshold(self):
        record = {"post_id": 10, "url": "https://t.me/channel/10", "published_at": "2026-01-01T00:00:00+00:00", "age_hours": 24, "age_seconds": 86700, "views": 100, "reactions": 10, "forwards": 3, "comments": 2, "subscribers": 300}
        result = sheets.display_record(record, {"timezone": "Europe/Moscow"})
        self.assertNotIn("er", result)
        self.assertNotIn("err", result)
        self.assertEqual(result["note"], "")
        self.assertAlmostEqual(result["published_at"], 46023.125)
        record.update(age_seconds=86701, views=0, subscribers=0)
        result = sheets.display_record(record, {"timezone": "Europe/Moscow"})
        self.assertTrue(result["note"])

    def test_setup_adds_formulas_using_configured_columns(self):
        api = MagicMock()
        api.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": TARGET["worksheet"], "gridProperties": {"rowCount": 100, "columnCount": 26}}}]}
        api.spreadsheets().values().batchGet().execute.return_value = {"valueRanges": [{"values": [["key1"], ["key2"]]}, {}, {}]}
        target = copy.deepcopy(TARGET)
        target["columns"].update(er="I", err="J")
        sheets.setup(api, target)
        requests = api.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"]
        formulas = {(request["updateCells"]["start"]["columnIndex"], request["updateCells"]["start"]["rowIndex"]): request["updateCells"]["rows"][0]["values"][0]["userEnteredValue"]["formulaValue"] for request in requests if "updateCells" in request}
        self.assertEqual(formulas[(8, 1)], "=(E2+F2+G2)/D2")
        self.assertEqual(formulas[(9, 1)], "=(E2+F2+G2)/H2")
        self.assertEqual(formulas[(8, 2)], "=(E3+F3+G3)/D3")
        self.assertEqual(formulas[(9, 2)], "=(E3+F3+G3)/H3")

    def test_setup_preserves_existing_formula_and_values(self):
        api = MagicMock()
        api.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": TARGET["worksheet"], "gridProperties": {"rowCount": 100, "columnCount": 26}}}]}
        target = copy.deepcopy(TARGET)
        target["columns"]["er"] = "I"
        api.spreadsheets().values().batchGet().execute.return_value = {"valueRanges": [{"values": [["key1"], ["key2"]]}, {"values": [["=custom_formula"], [0.5]]}]}
        sheets.setup(api, target)
        requests = api.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"]
        self.assertFalse(any("updateCells" in request for request in requests))

    def test_setup_replaces_our_array_with_individual_formulas(self):
        api = MagicMock()
        api.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": TARGET["worksheet"], "gridProperties": {"rowCount": 100, "columnCount": 26}}}]}
        target = copy.deepcopy(TARGET)
        target["columns"]["er"] = "I"
        old_formula = "=ARRAYFORMULA(ЕСЛИОШИБКА((E2:E+F2:F+G2:G)/D2:D))"
        api.spreadsheets().values().batchGet().execute.return_value = {"valueRanges": [{"values": [["key1"], ["key2"]]}, {"values": [[old_formula], [0.5]]}]}
        sheets.setup(api, target)
        requests = [r["updateCells"] for r in api.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"] if "updateCells" in r]
        self.assertEqual(requests[0]["rows"][0]["values"], [{}])
        self.assertEqual(requests[1]["rows"][0]["values"][0]["userEnteredValue"]["formulaValue"], "=(E2+F2+G2)/D2")
        self.assertEqual(requests[2]["rows"][0]["values"][0]["userEnteredValue"]["formulaValue"], "=(E3+F3+G3)/D3")

    def test_write_adds_formulas_and_preserves_existing_formula(self):
        api = MagicMock()
        api.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": TARGET["worksheet"], "gridProperties": {"rowCount": 100, "columnCount": 26}}}]}
        target = copy.deepcopy(TARGET)
        target["columns"].update(er="I", err="J")
        api.spreadsheets().values().batchGet().execute.return_value = {"valueRanges": [{}, {}, {"values": [["=custom"]]}, {}]}
        record = {"post_key": "123:m:10", "post_id": 10, "url": "https://t.me/channel/10", "published_at": "2026-01-01T00:00:00+00:00", "age_hours": 24, "views": 100, "reactions": 10, "forwards": 3, "comments": 2, "subscribers": 300}
        sheets.write(api, target, record, 2)
        requests = api.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"]
        indexes = [item["updateCells"]["start"]["columnIndex"] for item in requests]
        self.assertNotIn(8, indexes)
        self.assertIn(9, indexes)
        cell = next(item["updateCells"]["rows"][0]["values"][0] for item in requests if item["updateCells"]["start"]["columnIndex"] == 9)
        self.assertEqual(cell["userEnteredValue"]["formulaValue"], "=(E2+F2+G2)/H2")

    def test_inspect_ignores_formula_columns_when_selecting_row(self):
        api = self.api_with_values()
        target = copy.deepcopy(TARGET)
        target["columns"].update(er="I", err="J")
        self.assertEqual(sheets.inspect(api, target, {"post_key": "new"}), (2, False))
        ranges = api.spreadsheets().values().batchGet.call_args.kwargs["ranges"]
        self.assertTrue(all("!I2:I" not in item and "!J2:J" not in item for item in ranges))

    def test_column_indexes(self):
        self.assertEqual([sheets.column_index(col) for col in ("A", "Z", "AA", "AZ")], [0, 25, 26, 51])

    def test_setup_creates_headers_and_hides_technical_fields(self):
        api = MagicMock()
        api.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": TARGET["worksheet"], "gridProperties": {"rowCount": 100, "columnCount": 26}}}]}
        sheets.setup(api, TARGET)
        requests = api.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"]
        self.assertEqual(requests[0]["updateSheetProperties"]["properties"]["gridProperties"]["frozenRowCount"], 1)
        dimensions = [r["updateDimensionProperties"] for r in requests if "updateDimensionProperties" in r]
        key_column = next(r for r in dimensions if r["range"]["dimension"] == "COLUMNS" and r["range"]["startIndex"] == 25)
        self.assertTrue(key_column["properties"]["hiddenByUser"])

    def test_post_title_is_written_with_hyperlink(self):
        api = MagicMock()
        api.spreadsheets().get().execute.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": TARGET["worksheet"], "gridProperties": {"rowCount": 100, "columnCount": 26}}}]}
        target = copy.deepcopy(TARGET)
        target["columns"]["post_title"] = "B"
        record = {"post_key": "123:m:10", "post_id": 10, "post_title": "Короткий текст", "url": "https://t.me/channel/10", "published_at": "2026-01-01T00:00:00+00:00", "age_hours": 24, "views": 100, "reactions": 10, "forwards": 3, "comments": 2, "subscribers": 300}
        sheets.write(api, target, record, 2)
        requests = api.spreadsheets().batchUpdate.call_args.kwargs["body"]["requests"]
        cell = next(r["updateCells"]["rows"][0]["values"][0] for r in requests if r["updateCells"]["start"]["columnIndex"] == 1)
        self.assertEqual(cell["userEnteredValue"]["stringValue"], record["post_title"])
        self.assertEqual(cell["textFormatRuns"][0]["format"]["link"]["uri"], record["url"])


class TelegramTests(unittest.TestCase):
    def test_recent_posts_counts_albums_as_one(self):
        async def history(_):
            for item in (message(16, date=datetime.now(timezone.utc)), message(15), message(14, 999), message(13, 999), message(12), message(11)):
                yield item

        posts = asyncio.run(telegram.recent_posts(SimpleNamespace(iter_messages=history), ENTITY, 2))
        self.assertEqual([key for key, _ in posts], ["123:g:999", "123:m:15"])
        self.assertEqual([item.id for item in posts[0][1]], [14, 13])

    def test_qr_login_waits_for_scan_and_refreshes_expired_code(self):
        qr = SimpleNamespace(url="tg://login?token=secret", wait=AsyncMock(side_effect=[asyncio.TimeoutError(), None]))
        client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False), qr_login=AsyncMock(return_value=qr))
        with patch("qrcode.QRCode") as render, contextlib.redirect_stdout(io.StringIO()) as output:
            asyncio.run(login_qr(client))
        self.assertEqual(qr.wait.await_count, 2)
        self.assertEqual(client.qr_login.await_count, 2)
        self.assertEqual(render.return_value.add_data.call_args.args, (qr.url,))
        self.assertNotIn("secret", output.getvalue())

    def test_qr_login_accepts_two_step_password(self):
        from telethon import errors

        qr = SimpleNamespace(url="tg://login?token=secret", wait=AsyncMock(side_effect=errors.SessionPasswordNeededError(None)))
        client = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=False), qr_login=AsyncMock(return_value=qr), sign_in=AsyncMock())
        with patch("qrcode.QRCode"), patch("getpass.getpass", return_value="password"), contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(login_qr(client))
        client.sign_in.assert_awaited_once_with(password="password")

    def test_login_displays_delivery_method_without_exposing_response(self):
        client = SimpleNamespace()
        original = AsyncMock(return_value=SimpleNamespace(type=type("SentCodeTypeApp", (), {})(), phone_code_hash="secret"))
        client.send_code_request = original

        async def start():
            result = await client.send_code_request("+70000000000")
            self.assertEqual(result.phone_code_hash, "secret")

        client.start = start
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            asyncio.run(login(client))
        self.assertIn("Telegram app", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        self.assertIs(client.send_code_request, original)
        original.assert_awaited_once_with("+70000000000")

    def test_album_counts_are_not_multiplied_and_age_is_noted(self):
        published = datetime(2026, 1, 1, tzinfo=timezone.utc)
        job = {"published_at": published.timestamp(), "post_id": 10, "grouped_id": "999", "post_key": "123:g:999"}
        record = telegram.make_record(job, ENTITY, [message(10, 999), message(11, 999, views=110, reactions=7)], 1000, published + timedelta(hours=25))
        self.assertEqual((record["views"], record["reactions"], record["forwards"], record["comments"]), (110, 8, 3, 2))
        self.assertEqual(record["age_hours"], 25)
        self.assertIn("25", record["note"])
        self.assertEqual(record["subscribers"], 1000)

    def test_album_collection_recovers_neighbours(self):
        job = {"published_at": message().date.timestamp(), "post_id": 11, "grouped_id": "999", "post_key": "123:g:999", "message_ids": "[11]"}
        client = AsyncMock()
        client.get_messages.return_value = [message(10, 999), message(11, 999), message(12, None, views=10000)]
        client.return_value = SimpleNamespace(full_chat=SimpleNamespace(participants_count=1000))
        record = asyncio.run(telegram.collect(client, job, ENTITY))
        self.assertEqual(record["post_id"], 10)
        self.assertEqual(record["views"], 100)
        self.assertIn(10, client.get_messages.call_args.kwargs["ids"])

    def test_missing_subscribers_is_retried(self):
        client = AsyncMock()
        client.get_messages.return_value = [message()]
        client.return_value = SimpleNamespace(full_chat=SimpleNamespace(participants_count=None))
        with self.assertRaises(RuntimeError):
            asyncio.run(telegram.collect(client, {"message_ids": "[10]", "grouped_id": None, "post_id": 10, "published_at": 1}, ENTITY))


class ConfigTests(unittest.TestCase):
    def load(self, target):
        import yaml

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump({"channels": [{"source": "@channel", "sheet": target}]}), encoding="utf-8")
            with patch.dict(os.environ, {"TELEGRAM_API_ID": "123", "TELEGRAM_API_HASH": "hash", "GOOGLE_APPLICATION_CREDENTIALS": "key.json"}):
                return load_config(path)

    def test_defaults(self):
        self.assertEqual(self.load(copy.deepcopy(TARGET))["poll_seconds"], 60)

    def test_key_is_required(self):
        target = copy.deepcopy(TARGET)
        del target["columns"]["post_key"]
        with self.assertRaises(ValueError):
            self.load(target)

    def test_duplicate_columns_rejected(self):
        target = copy.deepcopy(TARGET)
        target["columns"]["views"] = "z"
        with self.assertRaises(ValueError):
            self.load(target)


if __name__ == "__main__":
    unittest.main()
