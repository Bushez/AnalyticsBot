import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import load_dotenv


FIELDS = {
    "post_key", "channel", "channel_id", "post_id", "grouped_id", "url",
    "published_at", "collected_at", "age_hours", "views", "reactions",
    "forwards", "comments", "subscribers", "note", "post_title", "er", "err",
}
REQUIRED_COLUMNS = {"post_key", "views", "reactions", "forwards", "comments", "subscribers"}


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def load_config(path):
    load_dotenv()
    with Path(path).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    config = validate_config(config)
    for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "GOOGLE_APPLICATION_CREDENTIALS"):
        if not os.getenv(name):
            raise ValueError(f"Missing environment variable: {name}")
    config["api_id"] = positive(int(os.environ["TELEGRAM_API_ID"]), "TELEGRAM_API_ID")
    config["api_hash"] = os.environ["TELEGRAM_API_HASH"]
    config["credentials"] = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    config["session"] = os.getenv("TELEGRAM_SESSION", "data/telegram")
    config["database"] = os.getenv("SQLITE_PATH", "data/jobs.sqlite3")
    return config


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("Config must be a YAML mapping")
    for name, default in (("poll_seconds", 60), ("retry_seconds", 300), ("initial_lookback_hours", 48)):
        config[name] = positive(config.get(name, default), name)
    channels = config.get("channels")
    if not isinstance(channels, list) or not channels:
        raise ValueError("channels must be a nonempty list")
    targets = {}
    for channel in channels:
        if not isinstance(channel, dict) or not isinstance(channel.get("source"), (str, int)):
            raise ValueError("Each channel needs a source: @username or numeric channel ID")
        target = channel.get("sheet")
        if not isinstance(target, dict):
            raise ValueError("Each channel needs a sheet mapping")
        for field in ("spreadsheet_id", "worksheet"):
            if not isinstance(target.get(field), str) or not target[field].strip():
                raise ValueError(f"sheet.{field} must be a nonempty string")
        target["start_row"] = positive(target.get("start_row", 2), "sheet.start_row")
        if target["start_row"] < 2:
            raise ValueError("sheet.start_row must be at least 2")
        target["timezone"] = target.get("timezone", "Europe/Moscow")
        try:
            ZoneInfo(target["timezone"])
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError("Invalid sheet.timezone") from exc
        columns = target.get("columns")
        if not isinstance(columns, dict) or not REQUIRED_COLUMNS <= columns.keys():
            raise ValueError(f"sheet.columns requires {sorted(REQUIRED_COLUMNS)}")
        if columns.keys() - FIELDS:
            raise ValueError(f"Unknown columns: {sorted(columns.keys() - FIELDS)}")
        if any(not isinstance(col, str) or not re.fullmatch(r"[A-Za-z]{1,3}", col) for col in columns.values()):
            raise ValueError("Columns must be A1 letters, for example A or AA")
        target["columns"] = {key: col.upper() for key, col in columns.items()}
        if len(set(target["columns"].values())) != len(columns):
            raise ValueError("Each field must use a different column")
        identity = (target["spreadsheet_id"], target["worksheet"])
        if identity in targets and targets[identity] != target:
            raise ValueError("Channels writing to the same worksheet must use identical sheet settings")
        targets[identity] = target
    return config
