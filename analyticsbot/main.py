import argparse
import asyncio
import getpass
import json
import logging
import sys
import time
from pathlib import Path

from telethon import TelegramClient, errors

from . import db, sheets, telegram
from .config import load_config


logger = logging.getLogger(__name__)


async def login(client):
    send_code = client.send_code_request

    async def send_code_with_info(*args, **kwargs):
        result = await send_code(*args, **kwargs)
        delivery = type(result.type).__name__
        descriptions = {
            "SentCodeTypeApp": "Telegram app: check the Telegram service chat on another logged-in device",
            "SentCodeTypeSms": "SMS",
            "SentCodeTypeSmsWord": "SMS word",
            "SentCodeTypeSmsPhrase": "SMS phrase",
            "SentCodeTypeEmailCode": "login email",
            "SentCodeTypeCall": "phone call",
            "SentCodeTypeFlashCall": "phone call",
            "SentCodeTypeMissedCall": "missed phone call",
            "SentCodeTypeFragmentSms": "Fragment",
            "SentCodeTypeFirebaseSms": "Firebase SMS",
            "SentCodeTypeSetUpEmailRequired": "email setup required",
        }
        print(f"Telegram delivery method: {descriptions.get(delivery, delivery)}", flush=True)
        return result

    client.send_code_request = send_code_with_info
    try:
        await client.start()
    finally:
        client.send_code_request = send_code


async def login_qr(client):
    import qrcode

    await client.connect()
    if await client.is_user_authorized():
        logger.info("Telegram session already authorized")
        return
    while True:
        qr = await client.qr_login()
        code = qrcode.QRCode(border=2)
        code.add_data(qr.url)
        try:
            "█▀▄".encode(sys.stdout.encoding or "utf-8")
        except UnicodeEncodeError:
            for row in code.get_matrix():
                print("".join("  " if cell else "##" for cell in row))
        else:
            code.print_ascii(invert=True)
        print("Telegram: Settings > Devices > Link Desktop Device. Scan the QR code above.", flush=True)
        try:
            await qr.wait()
            return
        except asyncio.TimeoutError:
            print("QR code expired; generating another one.", flush=True)
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=getpass.getpass("Telegram two-step verification password: "))
            return


def reserve_row(database, key, row):
    with database:
        database.execute("UPDATE jobs SET sheet_row = ? WHERE post_key = ?", (row, key))


def minimum_row(database, target):
    maximum = target["start_row"] - 1
    for job in database.execute("SELECT sheet_row, snapshot FROM jobs WHERE status = 'pending' AND sheet_row IS NOT NULL AND snapshot IS NOT NULL"):
        previous = json.loads(job["snapshot"])["target"]
        if (previous["spreadsheet_id"], previous["worksheet"]) == (target["spreadsheet_id"], target["worksheet"]):
            maximum = max(maximum, job["sheet_row"])
    return maximum + 1


async def process_jobs(client, database, api, channels, config, selected_keys=None):
    for job in db.due_jobs(database, time.time(), selected_keys):
        try:
            if job["snapshot"]:
                snapshot = json.loads(job["snapshot"])
            else:
                if job["channel_id"] not in channels:
                    continue
                entity, channel = channels[job["channel_id"]]
                record = await telegram.collect(client, job, entity)
                if record is None:
                    db.finish(database, job["post_key"], "deleted")
                    logger.warning("Post deleted: %s", job["post_key"])
                    continue
                snapshot = {"record": record, "target": channel["sheet"]}
                db.save_snapshot(database, job["post_key"], snapshot)
            target, record = snapshot["target"], snapshot["record"]
            row, exists = await asyncio.to_thread(
                sheets.inspect, api, target, record, job["sheet_row"], minimum_row(database, target)
            )
            if not exists:
                reserve_row(database, job["post_key"], row)
                await asyncio.to_thread(sheets.write, api, target, record, row)
            db.finish(database, job["post_key"], "done", row)
            logger.info("Saved %s to row %s%s", job["post_key"], row, " (already present)" if exists else "")
        except errors.FloodWaitError as exc:
            db.retry(database, job["post_key"], time.time() + max(exc.seconds, config["retry_seconds"]), exc)
            logger.warning("Telegram flood wait for %s: %ss", job["post_key"], exc.seconds)
            break
        except Exception as exc:
            db.retry(database, job["post_key"], time.time() + config["retry_seconds"], exc)
            logger.exception("Will retry %s", job["post_key"])


async def backfill(client, database, api, channel, count, config):
    source = channel["source"]
    lookup = int(source) if str(source).lstrip("-").isdigit() else source
    entity = await client.get_entity(lookup)
    if not getattr(entity, "broadcast", False):
        raise ValueError(f"{source} is not a broadcast channel")
    target = channel["sheet"]
    posts = await telegram.recent_posts(client, entity, count)
    selected = set()
    skipped = 0
    for key, messages in posts:
        first_id = min(message.id for message in messages)
        username = getattr(entity, "username", None)
        url = f"https://t.me/{username}/{first_id}" if username else f"https://t.me/c/{entity.id}/{first_id}"
        row, exists = await asyncio.to_thread(sheets.inspect, api, target, {"post_key": key, "url": url})
        if exists:
            job = database.execute("SELECT status FROM jobs WHERE post_key = ?", (key,)).fetchone()
            if job and job["status"] == "pending":
                db.finish(database, key, "done", row)
            skipped += 1
            logger.info("Already in sheet: %s (row %s)", key, row)
            continue
        for message in messages:
            db.remember(database, entity.id, message, advance_cursor=False)
        job = database.execute("SELECT * FROM jobs WHERE post_key = ?", (key,)).fetchone()
        if job["published_at"] + 86400 > time.time():
            skipped += 1
            logger.info("Post is less than 24 hours old: %s", key)
            continue
        if job["status"] == "deleted" or (job["status"] == "done" and not job["snapshot"]):
            skipped += 1
            logger.warning("Cannot restore %s without a saved snapshot", key)
            continue
        with database:
            if job["status"] == "done":
                snapshot = json.loads(job["snapshot"])
                snapshot["target"] = target
                database.execute("UPDATE jobs SET status = 'pending', sheet_row = NULL, snapshot = ? WHERE post_key = ?", (json.dumps(snapshot, ensure_ascii=False), key))
            database.execute("UPDATE jobs SET next_attempt = 0 WHERE post_key = ?", (key,))
        selected.add(key)
    await process_jobs(client, database, api, {entity.id: (entity, channel)}, config, selected)
    saved = sum(database.execute("SELECT status FROM jobs WHERE post_key = ?", (key,)).fetchone()[0] == "done" for key in selected)
    logger.info("Backfill: found %s posts at least 24 hours old, saved %s, skipped %s, pending %s", len(posts), saved, skipped, len(selected) - saved)
    if len(selected) != saved:
        raise RuntimeError("Some posts could not be saved; see log. Pending jobs remain in SQLite.")


async def run(config, login_only=False, qr_login=False, backfill_channel=None, backfill_count=None, on_started=None):
    Path(config["session"]).parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(config["session"], config["api_id"], config["api_hash"], flood_sleep_threshold=0)
    try:
        if login_only or qr_login:
            if qr_login:
                await login_qr(client)
            else:
                await login(client)
            logger.info("Telegram session saved")
            return
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram login required: run python -m analyticsbot --login first")
        api = await asyncio.to_thread(sheets.create_api, config["credentials"])
        database = db.connect(config["database"])
        channels = {}
        scan_after = {}
        try:
            db.update_pending_layout(database, [channel["sheet"] for channel in config["channels"]])
            if backfill_channel is not None:
                matches = [channel for channel in config["channels"] if str(channel["source"]).casefold().lstrip("@") == backfill_channel.casefold().lstrip("@")]
                if len(matches) != 1:
                    raise ValueError("Channel must match exactly one source in config.yaml")
                await asyncio.to_thread(sheets.setup, api, matches[0]["sheet"])
                await backfill(client, database, api, matches[0], backfill_count, config)
                return
            seen_sheets = set()
            for channel in config["channels"]:
                target = channel["sheet"]
                identity = (target["spreadsheet_id"], target["worksheet"])
                if identity not in seen_sheets:
                    await asyncio.to_thread(sheets.setup, api, target)
                    seen_sheets.add(identity)
            if on_started is not None:
                on_started()
            while True:
                for channel in config["channels"]:
                    source = channel["source"]
                    if scan_after.get(str(source), 0) > time.time():
                        continue
                    try:
                        lookup = int(source) if str(source).lstrip("-").isdigit() else source
                        entity = await client.get_entity(lookup)
                        if not getattr(entity, "broadcast", False):
                            raise ValueError(f"{source} is not a broadcast channel")
                        previous = channels.get(entity.id)
                        if previous and previous[1]["sheet"] != channel["sheet"]:
                            raise ValueError(f"Duplicate channel with different targets: {source}")
                        channels[entity.id] = (entity, channel)
                        await telegram.scan(client, database, entity, config["initial_lookback_hours"])
                    except errors.FloodWaitError as exc:
                        scan_after[str(source)] = time.time() + exc.seconds
                        logger.warning("Telegram flood wait for %s: %ss", source, exc.seconds)
                        break
                    except Exception:
                        scan_after[str(source)] = time.time() + config["retry_seconds"]
                        logger.exception("Cannot scan %s; will retry", source)
                await process_jobs(client, database, api, channels, config)
                await asyncio.sleep(config["poll_seconds"])
        finally:
            database.close()
    finally:
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description="Telegram statistics to Google Sheets")
    parser.add_argument("--config", default="config.yaml")
    login_options = parser.add_mutually_exclusive_group()
    login_options.add_argument("--login", action="store_true", help="Create a Telegram user session interactively")
    login_options.add_argument("--login-qr", action="store_true", help="Create a Telegram user session with a QR code")
    parser.add_argument("--channel", help="Configured channel username or ID for one-time backfill")
    parser.add_argument("--last", type=int, help="Number of latest posts at least 24 hours old to backfill")
    args = parser.parse_args()
    if (args.channel is None) != (args.last is None) or (args.last is not None and args.last < 1):
        parser.error("--channel and a positive --last must be supplied together")
    if (args.login or args.login_qr) and args.channel is not None:
        parser.error("Backfill cannot be combined with login")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    try:
        with db.single_instance(config["database"]):
            asyncio.run(run(config, args.login, args.login_qr, args.channel, args.last))
    except KeyboardInterrupt:
        logger.info("Stopped")


if __name__ == "__main__":
    main()
