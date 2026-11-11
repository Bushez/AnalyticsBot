import json
from datetime import datetime, timezone

from telethon.tl.functions.channels import GetFullChannelRequest

from . import db


async def scan(client, database, entity, lookback_hours):
    since = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
    state = db.channel_state(database, entity.id, since)
    options = {"min_id": state["last_message_id"], "reverse": True}
    if not state["last_message_id"]:
        options["offset_date"] = datetime.fromtimestamp(state["initial_since"], timezone.utc)
    async for message in client.iter_messages(entity, **options):
        db.remember(database, entity.id, message)


async def recent_posts(client, entity, limit):
    groups = {}
    cutoff = datetime.now(timezone.utc).timestamp() - 86400
    async for message in client.iter_messages(entity):
        if getattr(message, "action", None) is not None or not getattr(message, "post", False):
            continue
        if message.date.timestamp() > cutoff:
            continue
        group = str(message.grouped_id) if message.grouped_id is not None else None
        key = f"{entity.id}:g:{group}" if group else f"{entity.id}:m:{message.id}"
        if key not in groups and len(groups) == limit:
            break
        groups.setdefault(key, []).append(message)
    return list(reversed(list(groups.items())))


def make_record(job, entity, messages, subscribers, collected_at):
    published_at = datetime.fromtimestamp(job["published_at"], timezone.utc)
    age = (collected_at - published_at).total_seconds()
    note = f"Фактический возраст: {int(age // 3600)} ч {int(age % 3600 // 60)} мин" if age > 86700 else ""
    post_id = job["post_id"]
    username = getattr(entity, "username", None)
    url = f"https://t.me/{username}/{post_id}" if username else f"https://t.me/c/{entity.id}/{post_id}"
    return {
        "post_key": job["post_key"],
        "channel": entity.title,
        "channel_id": str(entity.id),
        "post_id": post_id,
        "post_title": next((" ".join(message.message.split())[:80] for message in sorted(messages, key=lambda item: item.id) if getattr(message, "message", None) and message.message.strip()), f"Пост №{post_id}"),
        "grouped_id": job["grouped_id"] or "",
        "url": url,
        "published_at": published_at.isoformat(timespec="seconds"),
        "collected_at": collected_at.isoformat(timespec="seconds"),
        "age_hours": round(age / 3600, 4),
        "age_seconds": age,
        "views": max((message.views or 0 for message in messages), default=0),
        "forwards": max((message.forwards or 0 for message in messages), default=0),
        "comments": max((message.replies.replies if message.replies else 0 for message in messages), default=0),
        "reactions": max((sum(result.count for result in message.reactions.results) if message.reactions else 0 for message in messages), default=0),
        "subscribers": subscribers,
        "note": note,
    }


async def collect(client, job, entity):
    ids = json.loads(job["message_ids"])
    if job["grouped_id"]:
        ids = list(range(max(1, min(ids) - 10), max(ids) + 11))
    messages = await client.get_messages(entity, ids=ids)
    messages = [message for message in messages if message and getattr(message, "date", None) is not None]
    if job["grouped_id"]:
        messages = [message for message in messages if str(message.grouped_id) == job["grouped_id"]]
    if not messages:
        return None
    job = dict(job)
    job["post_id"] = min(job["post_id"], min(message.id for message in messages))
    job["published_at"] = min(job["published_at"], min(message.date.timestamp() for message in messages))
    full = await client(GetFullChannelRequest(entity))
    subscribers = full.full_chat.participants_count
    if subscribers is None:
        raise RuntimeError("Telegram did not return the subscriber count")
    return make_record(job, entity, messages, subscribers, datetime.now(timezone.utc))
