import copy
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import dotenv_values

from . import db
from .config import validate_config


DEFAULT_COLUMNS = {
    "published_at": "A", "post_title": "B", "views": "C", "reactions": "D",
    "forwards": "E", "comments": "F", "subscribers": "G", "er": "H",
    "err": "I", "post_id": "J", "collected_at": "K", "age_hours": "L", "post_key": "M",
}


def data_dir():
    root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
    path = root / "user_data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_settings():
    return {"api_id": "", "api_hash": "", "phone": "", "credentials": "", "channels": [],
            "poll_seconds": 60, "retry_seconds": 300, "initial_lookback_hours": 48, "timezone": "Europe/Moscow", "start_service": False,
            "setup_complete": False, "setup_step": 0, "telegram_verified": False,
            "google_verified": False, "channel_verified": False, "telegram_account": "", "google_email": ""}


def load_settings(path):
    settings = default_settings()
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            stored = json.load(stream)
        if not isinstance(stored, dict):
            raise ValueError("Файл настроек повреждён")
        settings.update(stored)
    return settings


def save_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def channel_source(value):
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.hostname not in ("t.me", "telegram.me", "www.t.me", "www.telegram.me"):
            raise ValueError("Укажите ссылку вида https://t.me/username или @username")
        value = parsed.path.strip("/")
        if re.fullmatch(r"(?:\+|joinchat/)[A-Za-z0-9_-]+", value):
            return "https://t.me/" + value
    if value.lstrip("-").isdigit():
        return int(value)
    value = value.lstrip("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
        raise ValueError("Укажите адрес @username, ссылку-приглашение или числовой ID канала")
    return "@" + value


def spreadsheet_id(value):
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.hostname != "docs.google.com":
            raise ValueError("Укажите ссылку на Google Sheets")
        match = re.match(r"^/spreadsheets/d/([A-Za-z0-9_-]+)(?:/|$)", parsed.path)
        if not match:
            raise ValueError("Не удалось определить ID Google-таблицы из ссылки")
        return match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Укажите ссылку или ID Google-таблицы")
    return value


def runtime_config(settings, root):
    try:
        api_id = int(settings["api_id"])
        if api_id <= 0 or not settings["api_hash"].strip():
            raise ValueError
    except (ValueError, TypeError, KeyError):
        raise ValueError("Укажите корректные Telegram API ID и API hash") from None
    credentials = Path(settings.get("credentials", ""))
    if not credentials.is_absolute():
        credentials = root / credentials
    if not credentials.is_file():
        raise ValueError("Выберите JSON-ключ сервисного аккаунта Google")
    channels = []
    for channel in settings["channels"]:
        channels.append({"source": channel_source(channel["source"]), "sheet": {
            "spreadsheet_id": spreadsheet_id(channel["spreadsheet"]), "worksheet": channel["worksheet"].strip(),
            "start_row": 2, "timezone": settings["timezone"], "columns": copy.deepcopy(channel["columns"]),
        }})
    config = validate_config({"channels": channels, "poll_seconds": int(settings["poll_seconds"]),
                              "retry_seconds": int(settings["retry_seconds"]), "initial_lookback_hours": int(settings["initial_lookback_hours"])})
    config.update(api_id=api_id, api_hash=settings["api_hash"].strip(), credentials=str(credentials),
                  session=str(root / "telegram"), database=str(root / "jobs.sqlite3"))
    return config


def import_cli(source, destination):
    source, destination = Path(source), Path(destination)
    for name in ("settings.json", "jobs.sqlite3", "telegram.session", "google-service-account.json"):
        if (destination / name).exists():
            raise ValueError("Импорт доступен только в пустую папку данных приложения")
    config_path, env_path = source / "config.yaml", source / ".env"
    if not config_path.is_file() or not env_path.is_file():
        raise ValueError("В выбранной папке должны находиться config.yaml и .env")
    config = validate_config(yaml.safe_load(config_path.read_text(encoding="utf-8")))
    env = dotenv_values(env_path)
    if not env.get("TELEGRAM_API_ID") or not env.get("TELEGRAM_API_HASH") or not env.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise ValueError("В существующей конфигурации отсутствуют ключи Telegram или путь к ключу Google")
    def resolve(value):
        path = Path(value)
        return path if path.is_absolute() else source / path

    original_db = resolve(env.get("SQLITE_PATH") or "data/jobs.sqlite3")
    original_session = resolve(env.get("TELEGRAM_SESSION") or "data/telegram")
    if original_session.suffix != ".session":
        original_session = Path(str(original_session) + ".session")
    original_google = resolve(env["GOOGLE_APPLICATION_CREDENTIALS"])
    if not original_google.is_file():
        raise ValueError("JSON-ключ Google из старых настроек не найден")
    settings = default_settings()
    settings.update(api_id=env["TELEGRAM_API_ID"], api_hash=env["TELEGRAM_API_HASH"], credentials="google-service-account.json",
                    poll_seconds=config["poll_seconds"], retry_seconds=config["retry_seconds"],
                    initial_lookback_hours=config["initial_lookback_hours"], timezone=config["channels"][0]["sheet"]["timezone"])
    for channel in config["channels"]:
        target = channel["sheet"]
        if target["timezone"] != settings["timezone"]:
            raise ValueError("Для импорта настройте одинаковый часовой пояс для всех каналов")
        settings["channels"].append({"source": str(channel["source"]), "spreadsheet": target["spreadsheet_id"],
                                     "worksheet": target["worksheet"], "columns": target["columns"]})
    destination.mkdir(parents=True, exist_ok=True)
    with db.single_instance(original_db), tempfile.TemporaryDirectory(dir=destination) as temporary:
        temporary = Path(temporary)
        shutil.copy2(original_google, temporary / "google-service-account.json")
        if original_db.exists():
            source_db = sqlite3.connect(f"{original_db.resolve().as_uri()}?mode=ro", uri=True)
            target_db = sqlite3.connect(temporary / "jobs.sqlite3")
            try:
                source_db.backup(target_db)
            finally:
                target_db.close()
                source_db.close()
        if original_session.exists():
            source_session = sqlite3.connect(f"{original_session.resolve().as_uri()}?mode=ro", uri=True)
            target_session = sqlite3.connect(temporary / "telegram.session")
            try:
                source_session.backup(target_session)
            finally:
                target_session.close()
                source_session.close()
        save_settings(temporary / "settings.json", settings)
        for path in temporary.iterdir():
            path.replace(destination / path.name)
    return settings
