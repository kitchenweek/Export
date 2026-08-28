import asyncio
import base64
import hashlib
import logging
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import asyncpg
import qrcode
from cryptography.fernet import Fernet, InvalidToken
from telethon import Button, TelegramClient, events, types
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.custom import Message


API_ID = int(os.getenv("API_ID", "32200104"))
API_HASH = os.getenv("API_HASH", "4c657a43a0c2419cd5b18c44d09e68c1")
BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8797332751:AAE_WMFhyYtNXrhyIq-xCky50Dzynlz3Xco",
)
DATABASE_URL = os.getenv("DATABASE_URL", "")
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")

MIN_DELAY = 1.0
MAX_DELAY = 3.0
MAX_REPORT_PART = 3900
CHANNELS_PER_PAGE = 8
QR_TIMEOUT = 180

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("channel_sync")


@dataclass
class Post:
    message: Message
    date: datetime
    link: str
    album_size: int = 1
    caption_message: Optional[Message] = None

    @property
    def text(self) -> str:
        message = self.caption_message or self.message
        return message.message or ""

    @property
    def entities(self):
        message = self.caption_message or self.message
        return message.entities


@dataclass
class SyncIssue:
    kind: str
    source_link: str
    target_link: str
    details: str


@dataclass
class ChannelSelection:
    channels: dict[int, types.Channel]
    source: Optional[types.Channel] = None
    phase: str = "source"


bot_client = TelegramClient("channel_sync_bot", API_ID, API_HASH)
database: Optional[asyncpg.Pool] = None
fernet: Optional[Fernet] = None
user_clients: dict[int, TelegramClient] = {}
client_locks: dict[int, asyncio.Lock] = {}
sync_locks: dict[int, asyncio.Lock] = {}
login_tasks: dict[int, asyncio.Task] = {}
selections: dict[int, ChannelSelection] = {}


def get_lock(storage: dict[int, asyncio.Lock], user_id: int) -> asyncio.Lock:
    lock = storage.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        storage[user_id] = lock
    return lock


def build_fernet() -> Fernet:
    secret = SESSION_ENCRYPTION_KEY or BOT_TOKEN
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    return Fernet(key)


async def init_database():
    global database, fernet
    fernet = build_fernet()
    if not DATABASE_URL:
        log.error("DATABASE_URL Ð½Ðµ Ð·Ð°Ð´Ð°Ð½")
        return
    try:
        database = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        async with database.acquire() as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_user_sessions (
                    bot_user_id BIGINT PRIMARY KEY,
                    encrypted_session BYTEA NOT NULL,
                    telegram_account_id BIGINT,
                    account_name TEXT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
    except Exception:
        database = None
        log.exception("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¿Ð¾Ð´ÐºÐ»ÑÑÐ¸ÑÑÑÑ Ðº PostgreSQL")


async def save_session(user_id: int, session: str, account) -> None:
    if not database or not fernet:
        raise RuntimeError("PostgreSQL Ð½Ðµ Ð¿Ð¾Ð´ÐºÐ»ÑÑÑÐ½")
    encrypted = fernet.encrypt(session.encode("utf-8"))
    account_name = " ".join(
        part for part in (account.first_name, account.last_name) if part
    ) or account.username or str(account.id)
    await database.execute(
        """
        INSERT INTO telegram_user_sessions (
            bot_user_id, encrypted_session, telegram_account_id, account_name, updated_at
        ) VALUES ($1, $2, $3, $4, NOW())
        ON CONFLICT (bot_user_id) DO UPDATE SET
            encrypted_session = EXCLUDED.encrypted_session,
            telegram_account_id = EXCLUDED.telegram_account_id,
            account_name = EXCLUDED.account_name,
            updated_at = NOW()
        """,
        user_id,
        encrypted,
        account.id,
        account_name,
    )


async def load_session(user_id: int) -> Optional[str]:
    if not database or not fernet:
        return None
    encrypted = await database.fetchval(
        "SELECT encrypted_session FROM telegram_user_sessions WHERE bot_user_id = $1",
        user_id,
    )
    if not encrypted:
        return None
    try:
        return fernet.decrypt(bytes(encrypted)).decode("utf-8")
    except InvalidToken:
        log.error("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ ÑÐ°ÑÑÐ¸ÑÑÐ¾Ð²Ð°ÑÑ ÑÐµÑÑÐ¸Ñ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ %s", user_id)
        return None


async def delete_session(user_id: int) -> None:
    if database:
        await database.execute(
            "DELETE FROM telegram_user_sessions WHERE bot_user_id = $1",
            user_id,
        )


async def stored_account(user_id: int):
    if not database:
        return None
    return await database.fetchrow(
        """
        SELECT telegram_account_id, account_name, updated_at
        FROM telegram_user_sessions WHERE bot_user_id = $1
        """,
        user_id,
    )


async def get_user_client(user_id: int) -> Optional[TelegramClient]:
    async with get_lock(client_locks, user_id):
        cached = user_clients.get(user_id)
        if cached:
            if not cached.is_connected():
                await cached.connect()
            if await cached.is_user_authorized():
                return cached
            await cached.disconnect()
            user_clients.pop(user_id, None)

        session = await load_session(user_id)
        if not session:
            return None
        client = TelegramClient(StringSession(session), API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                await delete_session(user_id)
                return None
        except Exception:
            await client.disconnect()
            raise
        user_clients[user_id] = client
        return client


def main_menu():
    return [
        [Button.inline("â ÐÐ¾Ð´ÐºÐ»ÑÑÐ¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½Ñ", b"account:add")],
        [Button.inline("ð ÐÐµÑÐµÐ½ÐµÑÑÐ¸ Ð¿Ð¾ÑÑÑ", b"sync:start")],
        [
            Button.inline("ð¤ ÐÐ¾Ð¹ Ð°ÐºÐºÐ°ÑÐ½Ñ", b"account:show"),
            Button.inline("âï¸ ÐÐ¾Ð¼Ð¾ÑÑ", b"help"),
        ],
        [Button.inline("ðª ÐÑÐºÐ»ÑÑÐ¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½Ñ", b"account:remove")],
    ]


async def show_menu(event, text: str = "ÐÑÐ±ÐµÑÐ¸ÑÐµ Ð´ÐµÐ¹ÑÑÐ²Ð¸Ðµ:"):
    await event.respond(text, buttons=main_menu(), parse_mode=None)


async def begin_qr_login(event, user_id: int):
    old_task = login_tasks.get(user_id)
    if old_task and not old_task.done():
        await event.respond(
            "ÐÑÐ¾Ð´ ÑÐ¶Ðµ Ð·Ð°Ð¿ÑÑÐµÐ½. ÐÑÑÐºÐ°Ð½Ð¸ÑÑÐ¹ÑÐµ Ð¿Ð¾ÑÐ»ÐµÐ´Ð½Ð¸Ð¹ QR-ÐºÐ¾Ð´ Ð¸Ð»Ð¸ Ð½Ð°Ð¶Ð¼Ð¸ÑÐµ Â«ÐÑÐ¼ÐµÐ½Ð°Â».",
            buttons=[[Button.inline("ÐÑÐ¼ÐµÐ½Ð°", b"login:cancel")]],
            parse_mode=None,
        )
        return
    login_tasks[user_id] = asyncio.create_task(qr_login_flow(user_id))


async def qr_login_flow(user_id: int):
    login_client = TelegramClient(StringSession(), API_ID, API_HASH)
    qr_message = None
    try:
        await login_client.connect()
        qr_login = await login_client.qr_login()
        with tempfile.TemporaryDirectory(prefix="tg_qr_") as directory:
            qr_path = Path(directory) / "telegram-login.png"
            qrcode.make(qr_login.url).save(qr_path)
            qr_message = await bot_client.send_file(
                user_id,
                str(qr_path),
                caption=(
                    "ÐÑÐºÑÐ¾Ð¹ÑÐµ Telegram â ÐÐ°ÑÑÑÐ¾Ð¹ÐºÐ¸ â Ð£ÑÑÑÐ¾Ð¹ÑÑÐ²Ð° â "
                    "ÐÐ¾Ð´ÐºÐ»ÑÑÐ¸ÑÑ ÑÑÑÑÐ¾Ð¹ÑÑÐ²Ð¾ Ð¸ Ð¾ÑÑÐºÐ°Ð½Ð¸ÑÑÐ¹ÑÐµ QR-ÐºÐ¾Ð´.\n\n"
                    f"ÐÐ¾Ð´ Ð´ÐµÐ¹ÑÑÐ²ÑÐµÑ {QR_TIMEOUT // 60} Ð¼Ð¸Ð½ÑÑÑ. "
                    "ÐÐµ Ð¿ÐµÑÐµÑÑÐ»Ð°Ð¹ÑÐµ ÑÑÐ¾ ÑÐ¾Ð¾Ð±ÑÐµÐ½Ð¸Ðµ Ð´ÑÑÐ³Ð¸Ð¼ Ð»ÑÐ´ÑÐ¼."
                ),
                buttons=[
                    [Button.url("ð± ÐÑÐºÑÑÑÑ Ð²ÑÐ¾Ð´ Ð½Ð° ÑÑÐ¾Ð¼ ÑÑÑÑÐ¾Ð¹ÑÑÐ²Ðµ", qr_login.url)],
                    [Button.inline("ÐÑÐ¼ÐµÐ½Ð°", b"login:cancel")],
                ],
            )
        await qr_login.wait(timeout=QR_TIMEOUT)
        account = await login_client.get_me()
        await save_session(user_id, login_client.session.save(), account)

        previous = user_clients.pop(user_id, None)
        if previous:
            await previous.disconnect()
        user_clients[user_id] = login_client
        login_client = None

        if qr_message:
            try:
                await qr_message.delete()
            except Exception:
                pass
        name = " ".join(
            part for part in (account.first_name, account.last_name) if part
        ) or account.username or str(account.id)
        await bot_client.send_message(
            user_id,
            f"ÐÐºÐºÐ°ÑÐ½Ñ Ð¿Ð¾Ð´ÐºÐ»ÑÑÑÐ½: {name}",
            buttons=main_menu(),
            parse_mode=None,
        )
    except SessionPasswordNeededError:
        await bot_client.send_message(
            user_id,
            "ÐÐ° Ð°ÐºÐºÐ°ÑÐ½ÑÐµ Ð²ÐºÐ»ÑÑÑÐ½ Ð¾Ð±Ð»Ð°ÑÐ½ÑÐ¹ Ð¿Ð°ÑÐ¾Ð»Ñ Telegram. Ð ÑÐµÐ»ÑÑ Ð±ÐµÐ·Ð¾Ð¿Ð°ÑÐ½Ð¾ÑÑÐ¸ "
            "Ð±Ð¾Ñ Ð½Ðµ Ð¿ÑÐ¾ÑÐ¸Ñ Ð¿ÑÐ¸ÑÑÐ»Ð°ÑÑ Ð¿Ð°ÑÐ¾Ð»Ñ Ð² ÑÐ°Ñ. ÐÐ»Ñ ÑÑÐ¾Ð³Ð¾ Ð°ÐºÐºÐ°ÑÐ½ÑÐ° Ð¿Ð¾Ð½Ð°Ð´Ð¾Ð±Ð¸ÑÑÑ "
            "Ð»Ð¾ÐºÐ°Ð»ÑÐ½Ð¾Ðµ ÑÐ¾Ð·Ð´Ð°Ð½Ð¸Ðµ ÑÐµÑÑÐ¸Ð¸.",
            buttons=main_menu(),
            parse_mode=None,
        )
    except asyncio.TimeoutError:
        await bot_client.send_message(
            user_id,
            "ÐÑÐµÐ¼Ñ QR-ÐºÐ¾Ð´Ð° Ð¸ÑÑÐµÐºÐ»Ð¾. ÐÐ°Ð¶Ð¼Ð¸ÑÐµ Â«ÐÐ¾Ð´ÐºÐ»ÑÑÐ¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½ÑÂ» Ð¸ Ð¿Ð¾Ð¿ÑÐ¾Ð±ÑÐ¹ÑÐµ ÑÐ½Ð¾Ð²Ð°.",
            buttons=main_menu(),
            parse_mode=None,
        )
    except asyncio.CancelledError:
        await bot_client.send_message(
            user_id,
            "ÐÐ¾Ð´ÐºÐ»ÑÑÐµÐ½Ð¸Ðµ Ð°ÐºÐºÐ°ÑÐ½ÑÐ° Ð¾ÑÐ¼ÐµÐ½ÐµÐ½Ð¾.",
            buttons=main_menu(),
            parse_mode=None,
        )
    except Exception as error:
        log.exception("ÐÑÐ¸Ð±ÐºÐ° QR-Ð²ÑÐ¾Ð´Ð° Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ %s", user_id)
        await bot_client.send_message(
            user_id,
            f"ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¿Ð¾Ð´ÐºÐ»ÑÑÐ¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½Ñ: {type(error).__name__}: {error}",
            buttons=main_menu(),
            parse_mode=None,
        )
    finally:
        if login_client:
            await login_client.disconnect()
        if login_tasks.get(user_id) is asyncio.current_task():
            login_tasks.pop(user_id, None)


def message_link(entity, message_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"
    channel_id = str(getattr(entity, "id", ""))
    return f"https://t.me/c/{channel_id}/{message_id}" if channel_id else "ÑÑÑÐ»ÐºÐ° Ð½ÐµÐ´Ð¾ÑÑÑÐ¿Ð½Ð°"


def is_real_post(message: Message) -> bool:
    return bool(message and not message.action and (message.message or message.media))


def first_photo(messages: list[Message]) -> Message:
    ordered = sorted(messages, key=lambda item: item.id)
    return next((item for item in ordered if item.photo), ordered[0])


def has_transferable_media(message: Message) -> bool:
    return bool(message.photo or message.document)


async def load_posts(client: TelegramClient, channel) -> list[Post]:
    entity = await client.get_entity(channel)
    singles: list[Message] = []
    albums: dict[int, list[Message]] = {}
    async for message in client.iter_messages(entity, reverse=True):
        if not is_real_post(message):
            continue
        if message.grouped_id:
            albums.setdefault(message.grouped_id, []).append(message)
        else:
            singles.append(message)

    posts = [
        Post(message=item, date=item.date, link=message_link(entity, item.id))
        for item in singles
    ]
    for messages in albums.values():
        ordered = sorted(messages, key=lambda item: item.id)
        media_message = first_photo(ordered)
        caption_message = next((item for item in ordered if item.message), media_message)
        posts.append(
            Post(
                message=media_message,
                caption_message=caption_message,
                date=ordered[0].date,
                link=message_link(entity, ordered[0].id),
                album_size=len(ordered),
            )
        )
    posts.sort(key=lambda item: (item.date, item.message.id))
    return posts


def nearest_unused(source: Post, targets: list[Post], used: set[int]) -> Optional[Post]:
    available = [item for item in targets if item.message.id not in used]
    if not available:
        return None
    source_day = source.date.date()
    return min(
        available,
        key=lambda item: (
            abs((item.date.date() - source_day).days),
            abs((item.date - source.date).total_seconds()),
            item.message.id,
        ),
    )


async def edit_with_flood_retry(client, target_entity, target, source, file):
    kwargs = {
        "entity": target_entity,
        "message": target.message.id,
        "text": source.text,
        "formatting_entities": source.entities,
        "file": file,
        "link_preview": True,
    }
    try:
        await client.edit_message(**kwargs)
    except FloodWaitError as error:
        await asyncio.sleep(error.seconds + 1)
        await client.edit_message(**kwargs)


async def replace_post(client, target_entity, target, source, temp_dir: Path):
    media_file = None
    downloaded_path: Optional[Path] = None
    if has_transferable_media(source.message):
        downloaded = await client.download_media(source.message, file=str(temp_dir))
        if not downloaded:
            raise RuntimeError("Ð½Ðµ ÑÐ´Ð°Ð»Ð¾ÑÑ ÑÐºÐ°ÑÐ°ÑÑ Ð¼ÐµÐ´Ð¸Ð° Ð¸ÑÑÐ¾Ð´Ð½Ð¾Ð³Ð¾ Ð¿Ð¾ÑÑÐ°")
        downloaded_path = Path(downloaded)
        media_file = str(downloaded_path)
    elif source.message.media and not isinstance(
        source.message.media,
        (types.MessageMediaWebPage, types.MessageMediaEmpty),
    ):
        raise RuntimeError(
            f"ÑÐ¸Ð¿ Ð¼ÐµÐ´Ð¸Ð° {type(source.message.media).__name__} Ð½ÐµÐ»ÑÐ·Ñ Ð¿ÐµÑÐµÐ½ÐµÑÑÐ¸ ÑÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°Ð½Ð¸ÐµÐ¼"
        )
    elif has_transferable_media(target.message):
        media_file = types.InputMediaEmpty()
    try:
        await edit_with_flood_retry(client, target_entity, target, source, media_file)
    finally:
        if downloaded_path:
            downloaded_path.unlink(missing_ok=True)


async def run_sync(client, source_channel, target_channel, progress_callback=None):
    source_entity = await client.get_entity(source_channel)
    target_entity = await client.get_entity(target_channel)
    if source_entity.id == target_entity.id:
        raise RuntimeError("Ð¾ÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ Ð¸ Ð²ÑÐ¾ÑÐ¾Ð¹ ÐºÐ°Ð½Ð°Ð» Ð½Ðµ Ð´Ð¾Ð»Ð¶Ð½Ñ ÑÐ¾Ð²Ð¿Ð°Ð´Ð°ÑÑ")
    source_posts, target_posts = await asyncio.gather(
        load_posts(client, source_entity),
        load_posts(client, target_entity),
    )
    if progress_callback:
        await progress_callback(
            f"ÐÐ°Ð¹Ð´ÐµÐ½Ð¾ Ð¿Ð¾ÑÑÐ¾Ð²: Ð¾ÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ ÐºÐ°Ð½Ð°Ð» â {len(source_posts)}, "
            f"Ð²ÑÐ¾ÑÐ¾Ð¹ ÐºÐ°Ð½Ð°Ð» â {len(target_posts)}. ÐÐ°ÑÐ¸Ð½Ð°Ñ Ð·Ð°Ð¼ÐµÐ½Ñ."
        )

    issues: list[SyncIssue] = []
    used_targets: set[int] = set()
    changed = 0
    with tempfile.TemporaryDirectory(prefix="tg_channel_sync_") as directory:
        temp_dir = Path(directory)
        for number, source in enumerate(source_posts, start=1):
            target = nearest_unused(source, target_posts, used_targets)
            if target is None:
                issues.append(
                    SyncIssue(
                        "ÐÐ¨ÐÐÐÐ", source.link, "Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½",
                        "Ð²Ð¾ Ð²ÑÐ¾ÑÐ¾Ð¼ ÐºÐ°Ð½Ð°Ð»Ðµ Ð·Ð°ÐºÐ¾Ð½ÑÐ¸Ð»Ð¸ÑÑ ÑÐ²Ð¾Ð±Ð¾Ð´Ð½ÑÐµ Ð¿Ð¾ÑÑÑ",
                    )
                )
                continue
            used_targets.add(target.message.id)
            await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            try:
                await replace_post(client, target_entity, target, source, temp_dir)
                changed += 1
                if target.album_size > 1:
                    issues.append(
                        SyncIssue(
                            "ÐÐ ÐÐÐ£ÐÐ ÐÐÐÐÐÐÐ", source.link, target.link,
                            "Ð¸Ð·Ð¼ÐµÐ½ÑÐ½ Ð¿ÐµÑÐ²ÑÐ¹ ÑÐ»ÐµÐ¼ÐµÐ½Ñ ÑÐµÐ»ÐµÐ²Ð¾Ð³Ð¾ Ð°Ð»ÑÐ±Ð¾Ð¼Ð°; Ð¾ÑÑÐ°Ð»ÑÐ½ÑÐµ Ð½Ðµ ÑÐ´Ð°Ð»ÑÐ»Ð¸ÑÑ",
                        )
                    )
            except Exception as error:
                log.exception("ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð·Ð°Ð¼ÐµÐ½Ð¸ÑÑ %s -> %s", source.link, target.link)
                issues.append(
                    SyncIssue(
                        "ÐÐ¨ÐÐÐÐ", source.link, target.link,
                        f"{type(error).__name__}: {error}",
                    )
                )
            if progress_callback and number % 50 == 0:
                await progress_callback(
                    f"ÐÐ±ÑÐ°Ð±Ð¾ÑÐ°Ð½Ð¾ {number}/{len(source_posts)}, Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ð¾ {changed}."
                )
    return changed, len(source_posts), issues


def build_report(changed: int, total: int, issues: list[SyncIssue]) -> str:
    errors = sum(item.kind == "ÐÐ¨ÐÐÐÐ" for item in issues)
    warnings = sum(item.kind == "ÐÐ ÐÐÐ£ÐÐ ÐÐÐÐÐÐÐ" for item in issues)
    lines = [
        "Ð¡Ð¸Ð½ÑÑÐ¾Ð½Ð¸Ð·Ð°ÑÐ¸Ñ Ð·Ð°Ð²ÐµÑÑÐµÐ½Ð°.",
        f"Ð£ÑÐ¿ÐµÑÐ½Ð¾ Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ð¾: {changed}/{total}",
        f"ÐÑÐ¸Ð±Ð¾Ðº: {errors}",
        f"ÐÑÐµÐ´ÑÐ¿ÑÐµÐ¶Ð´ÐµÐ½Ð¸Ð¹: {warnings}",
    ]
    if issues:
        lines.append("\nÐ¡Ð¿Ð¸ÑÐ¾Ðº Ð¾ÑÐ¸Ð±Ð¾Ðº Ð¸ Ð¿ÑÐµÐ´ÑÐ¿ÑÐµÐ¶Ð´ÐµÐ½Ð¸Ð¹:")
        for index, item in enumerate(issues, start=1):
            lines.extend(
                [
                    f"\n{index}. {item.kind}: {item.details}",
                    f"ÐÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ ÐºÐ°Ð½Ð°Ð»: {item.source_link}",
                    f"ÐÑÐ¾ÑÐ¾Ð¹ ÐºÐ°Ð½Ð°Ð»: {item.target_link}",
                ]
            )
    return "\n".join(lines)


def split_report(text: str, limit: int = MAX_REPORT_PART) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            parts.append(current)
            current = ""
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        current += line
    if current:
        parts.append(current)
    return parts


def can_edit_channel(channel: types.Channel) -> bool:
    if getattr(channel, "creator", False):
        return True
    rights = getattr(channel, "admin_rights", None)
    return bool(rights and rights.edit_messages)


async def available_channels(client) -> dict[int, types.Channel]:
    result: list[types.Channel] = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, types.Channel) and entity.broadcast:
            result.append(entity)
    result.sort(key=lambda item: (item.title or "").casefold())
    return {item.id: item for item in result}


def channel_page(state: ChannelSelection, page: int):
    channels = list(state.channels.values())
    if state.phase == "target":
        channels = [
            item for item in channels
            if can_edit_channel(item) and (not state.source or item.id != state.source.id)
        ]
    total_pages = max(1, (len(channels) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * CHANNELS_PER_PAGE
    visible = channels[start : start + CHANNELS_PER_PAGE]
    buttons = []
    for channel in visible:
        icon = "âï¸" if can_edit_channel(channel) else "ð"
        title = (channel.title or "ÐÐµÐ· Ð½Ð°Ð·Ð²Ð°Ð½Ð¸Ñ")[:45]
        buttons.append(
            [Button.inline(f"{icon} {title}", f"pick:{state.phase}:{channel.id}".encode())]
        )
    navigation = []
    if page > 0:
        navigation.append(Button.inline("â¬ï¸", f"page:{state.phase}:{page - 1}".encode()))
    navigation.append(Button.inline(f"{page + 1}/{total_pages}", b"noop"))
    if page + 1 < total_pages:
        navigation.append(Button.inline("â¡ï¸", f"page:{state.phase}:{page + 1}".encode()))
    buttons.append(navigation)
    buttons.append([Button.inline("â ÐÑÐ¼ÐµÐ½Ð°", b"sync:cancel")])
    action = "Ð¾ÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ ÐºÐ°Ð½Ð°Ð»" if state.phase == "source" else "Ð²ÑÐ¾ÑÐ¾Ð¹ ÐºÐ°Ð½Ð°Ð»"
    return f"ÐÑÐ±ÐµÑÐ¸ÑÐµ {action}:\nâï¸ â Ð¼Ð¾Ð¶Ð½Ð¾ Ð¸Ð·Ð¼ÐµÐ½ÑÑÑ, ð â ÑÐ¾Ð»ÑÐºÐ¾ ÑÑÐµÐ½Ð¸Ðµ", buttons


async def show_channel_page(event, state: ChannelSelection, page: int = 0, edit=False):
    text, buttons = channel_page(state, page)
    if edit:
        await event.edit(text, buttons=buttons, parse_mode=None)
    else:
        await event.respond(text, buttons=buttons, parse_mode=None)


async def start_channel_selection(event, user_id: int):
    client = await get_user_client(user_id)
    if not client:
        await event.respond(
            "Ð¡Ð½Ð°ÑÐ°Ð»Ð° Ð¿Ð¾Ð´ÐºÐ»ÑÑÐ¸ÑÐµ Telegram-Ð°ÐºÐºÐ°ÑÐ½Ñ.",
            buttons=[[Button.inline("â ÐÐ¾Ð´ÐºÐ»ÑÑÐ¸ÑÑ", b"account:add")]],
            parse_mode=None,
        )
        return
    if get_lock(sync_locks, user_id).locked():
        await event.respond("Ð£ Ð²Ð°Ñ ÑÐ¶Ðµ Ð²ÑÐ¿Ð¾Ð»Ð½ÑÐµÑÑÑ Ð¿ÐµÑÐµÐ½Ð¾Ñ.", parse_mode=None)
        return
    try:
        channels = await available_channels(client)
    except Exception as error:
        await event.respond(
            f"ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð·Ð°Ð³ÑÑÐ·Ð¸ÑÑ ÐºÐ°Ð½Ð°Ð»Ñ: {type(error).__name__}: {error}",
            parse_mode=None,
        )
        return
    if len(channels) < 2:
        await event.respond(
            "Ð Ð¿Ð¾Ð´ÐºÐ»ÑÑÑÐ½Ð½Ð¾Ð¼ Ð°ÐºÐºÐ°ÑÐ½ÑÐµ Ð´Ð¾Ð»Ð¶Ð½Ð¾ Ð±ÑÑÑ Ð½Ðµ Ð¼ÐµÐ½ÐµÐµ Ð´Ð²ÑÑ ÐºÐ°Ð½Ð°Ð»Ð¾Ð².",
            parse_mode=None,
        )
        return
    state = ChannelSelection(channels=channels)
    selections[user_id] = state
    await show_channel_page(event, state)


async def execute_sync(event, user_id: int, source, target):
    lock = get_lock(sync_locks, user_id)
    async with lock:
        client = await get_user_client(user_id)
        if not client:
            await event.respond("Ð¡ÐµÑÑÐ¸Ñ Ð°ÐºÐºÐ°ÑÐ½ÑÐ° Ð½ÐµÐ´ÐµÐ¹ÑÑÐ²Ð¸ÑÐµÐ»ÑÐ½Ð°.", parse_mode=None)
            return
        await event.respond(
            f"ÐÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ ÐºÐ°Ð½Ð°Ð»: {source.title}\n"
            f"ÐÑÐ¾ÑÐ¾Ð¹ ÐºÐ°Ð½Ð°Ð»: {target.title}\n"
            "ÐÐ°Ð³ÑÑÐ¶Ð°Ñ Ð¿Ð¾ÑÑÑâ¦",
            parse_mode=None,
        )

        async def progress(text: str):
            await event.respond(text, parse_mode=None)

        try:
            changed, total, issues = await run_sync(client, source, target, progress)
            report = build_report(changed, total, issues)
        except Exception as error:
            log.exception("ÐÑÐ¸ÑÐ¸ÑÐµÑÐºÐ°Ñ Ð¾ÑÐ¸Ð±ÐºÐ° ÑÐ¸Ð½ÑÑÐ¾Ð½Ð¸Ð·Ð°ÑÐ¸Ð¸ Ð¿Ð¾Ð»ÑÐ·Ð¾Ð²Ð°ÑÐµÐ»Ñ %s", user_id)
            report = f"Ð¡Ð¸Ð½ÑÑÐ¾Ð½Ð¸Ð·Ð°ÑÐ¸Ñ Ð¾ÑÑÐ°Ð½Ð¾Ð²Ð»ÐµÐ½Ð°:\n{type(error).__name__}: {error}"
        for part in split_report(report):
            await event.respond(part, link_preview=False, parse_mode=None)
        await show_menu(event)


@bot_client.on(events.NewMessage(pattern=r"^/(start|menu)(?:@\w+)?$"))
async def start_handler(event):
    if event.is_private:
        await show_menu(
            event,
            "ÐÐ¾Ñ Ð¿ÐµÑÐµÐ½Ð¾ÑÐ¸Ñ ÑÐ¾Ð´ÐµÑÐ¶Ð¸Ð¼Ð¾Ðµ Ð¿Ð¾ÑÑÐ¾Ð² Ð¼ÐµÐ¶Ð´Ñ Ð²Ð°ÑÐ¸Ð¼Ð¸ ÐºÐ°Ð½Ð°Ð»Ð°Ð¼Ð¸ Ð¿Ð¾ Ð±Ð»Ð¸Ð¶Ð°Ð¹ÑÐ¸Ð¼ Ð´Ð°ÑÐ°Ð¼.",
        )


@bot_client.on(events.NewMessage(pattern=r"^/sync(?:@\w+)?$"))
async def sync_command(event):
    if event.is_private:
        await start_channel_selection(event, event.sender_id)


@bot_client.on(events.CallbackQuery(data=b"account:add"))
async def add_account_handler(event):
    await event.answer()
    if get_lock(sync_locks, event.sender_id).locked():
        await event.respond(
            "ÐÐ¾Ð¶Ð´Ð¸ÑÐµÑÑ Ð¾ÐºÐ¾Ð½ÑÐ°Ð½Ð¸Ñ ÑÐµÐºÑÑÐµÐ³Ð¾ Ð¿ÐµÑÐµÐ½Ð¾ÑÐ° Ð¿ÐµÑÐµÐ´ Ð¿Ð¾Ð´ÐºÐ»ÑÑÐµÐ½Ð¸ÐµÐ¼ Ð´ÑÑÐ³Ð¾Ð³Ð¾ Ð°ÐºÐºÐ°ÑÐ½ÑÐ°.",
            parse_mode=None,
        )
        return
    if not database:
        await event.respond(
            "PostgreSQL Ð½Ðµ Ð¿Ð¾Ð´ÐºÐ»ÑÑÑÐ½. Ð£ÐºÐ°Ð¶Ð¸ÑÐµ DATABASE_URL Ð½Ð° ÑÐ¾ÑÑÐ¸Ð½Ð³Ðµ.",
            parse_mode=None,
        )
        return
    await begin_qr_login(event, event.sender_id)


@bot_client.on(events.CallbackQuery(data=b"login:cancel"))
async def cancel_login_handler(event):
    await event.answer()
    task = login_tasks.get(event.sender_id)
    if task and not task.done():
        task.cancel()
    else:
        await show_menu(event, "ÐÐºÑÐ¸Ð²Ð½Ð¾Ð³Ð¾ Ð¿Ð¾Ð´ÐºÐ»ÑÑÐµÐ½Ð¸Ñ Ð½ÐµÑ.")


@bot_client.on(events.CallbackQuery(data=b"account:show"))
async def show_account_handler(event):
    await event.answer()
    row = await stored_account(event.sender_id)
    if not row:
        await event.respond(
            "Telegram-Ð°ÐºÐºÐ°ÑÐ½Ñ ÐµÑÑ Ð½Ðµ Ð¿Ð¾Ð´ÐºÐ»ÑÑÑÐ½.", buttons=main_menu(), parse_mode=None
        )
        return
    await event.respond(
        f"ÐÐ¾Ð´ÐºÐ»ÑÑÑÐ½ Ð°ÐºÐºÐ°ÑÐ½Ñ: {row['account_name']}\n"
        f"Telegram ID: {row['telegram_account_id']}",
        buttons=main_menu(), parse_mode=None,
    )


@bot_client.on(events.CallbackQuery(data=b"account:remove"))
async def remove_account_question(event):
    await event.answer()
    await event.respond(
        "ÐÑÐºÐ»ÑÑÐ¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½Ñ? Ð¢ÐµÐºÑÑÐ°Ñ ÑÐµÑÑÐ¸Ñ Ð±ÑÐ´ÐµÑ Ð¾ÑÐ¾Ð·Ð²Ð°Ð½Ð° Ð² Telegram.",
        buttons=[[
            Button.inline("ÐÐ°, Ð¾ÑÐºÐ»ÑÑÐ¸ÑÑ", b"account:remove:yes"),
            Button.inline("ÐÑÐ¼ÐµÐ½Ð°", b"menu"),
        ]],
        parse_mode=None,
    )


@bot_client.on(events.CallbackQuery(data=b"account:remove:yes"))
async def remove_account_handler(event):
    await event.answer()
    user_id = event.sender_id
    if get_lock(sync_locks, user_id).locked():
        await event.respond(
            "ÐÐµÐ»ÑÐ·Ñ Ð¾ÑÐºÐ»ÑÑÐ¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½Ñ Ð²Ð¾ Ð²ÑÐµÐ¼Ñ Ð¿ÐµÑÐµÐ½Ð¾ÑÐ°. ÐÐ¾Ð¶Ð´Ð¸ÑÐµÑÑ Ð·Ð°Ð²ÐµÑÑÐµÐ½Ð¸Ñ.",
            buttons=main_menu(),
            parse_mode=None,
        )
        return
    task = login_tasks.get(user_id)
    if task and not task.done():
        task.cancel()
    client = user_clients.pop(user_id, None)
    if not client:
        client = await get_user_client(user_id)
        user_clients.pop(user_id, None)
    if client:
        try:
            await client.log_out()
        except Exception as error:
            await client.disconnect()
            await event.respond(
                f"ÐÐµ ÑÐ´Ð°Ð»Ð¾ÑÑ Ð¾ÑÐ¾Ð·Ð²Ð°ÑÑ ÑÐµÑÑÐ¸Ñ: {type(error).__name__}: {error}",
                buttons=main_menu(),
                parse_mode=None,
            )
            return
    await delete_session(user_id)
    selections.pop(user_id, None)
    await show_menu(event, "ÐÐºÐºÐ°ÑÐ½Ñ Ð¾ÑÐºÐ»ÑÑÑÐ½, ÑÐµÑÑÐ¸Ñ ÑÐ´Ð°Ð»ÐµÐ½Ð°.")


@bot_client.on(events.CallbackQuery(data=b"sync:start"))
async def sync_button_handler(event):
    await event.answer()
    await start_channel_selection(event, event.sender_id)


@bot_client.on(events.CallbackQuery(pattern=rb"page:(source|target):(\d+)"))
async def channel_page_handler(event):
    await event.answer()
    state = selections.get(event.sender_id)
    if not state:
        await show_menu(event, "ÐÑÐ±Ð¾Ñ ÑÑÑÐ°ÑÐµÐ». ÐÐ°ÑÐ½Ð¸ÑÐµ Ð·Ð°Ð½Ð¾Ð²Ð¾.")
        return
    phase, page = event.data.decode().split(":")[1:]
    if phase != state.phase:
        await event.answer("Ð­ÑÐ¾Ñ ÑÐ¿Ð¸ÑÐ¾Ðº ÑÐ¶Ðµ ÑÑÑÐ°ÑÐµÐ»", alert=True)
        return
    await show_channel_page(event, state, int(page), edit=True)


@bot_client.on(events.CallbackQuery(pattern=rb"pick:(source|target):(\d+)"))
async def channel_pick_handler(event):
    await event.answer()
    user_id = event.sender_id
    state = selections.get(user_id)
    if not state:
        await show_menu(event, "ÐÑÐ±Ð¾Ñ ÑÑÑÐ°ÑÐµÐ». ÐÐ°ÑÐ½Ð¸ÑÐµ Ð·Ð°Ð½Ð¾Ð²Ð¾.")
        return
    _, phase, channel_id_text = event.data.decode().split(":")
    if phase != state.phase:
        await event.answer("Ð­ÑÐ¾Ñ ÑÐ¿Ð¸ÑÐ¾Ðº ÑÐ¶Ðµ ÑÑÑÐ°ÑÐµÐ»", alert=True)
        return
    channel = state.channels.get(int(channel_id_text))
    if not channel:
        await event.answer("ÐÐ°Ð½Ð°Ð» Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½", alert=True)
        return
    if phase == "source":
        state.source = channel
        state.phase = "target"
        await show_channel_page(event, state, 0, edit=True)
        return
    source = state.source
    selections.pop(user_id, None)
    if not source:
        await show_menu(event, "ÐÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ ÐºÐ°Ð½Ð°Ð» Ð½Ðµ Ð²ÑÐ±ÑÐ°Ð½. ÐÐ°ÑÐ½Ð¸ÑÐµ Ð·Ð°Ð½Ð¾Ð²Ð¾.")
        return
    await event.edit(
        f"ÐÑÐ±ÑÐ°Ð½Ð¾:\n{source.title} â {channel.title}\n\nÐÐ°ÑÐ¸Ð½Ð°Ñ Ð¿ÐµÑÐµÐ½Ð¾Ñâ¦",
        buttons=None, parse_mode=None,
    )
    await execute_sync(event, user_id, source, channel)


@bot_client.on(events.CallbackQuery(data=b"sync:cancel"))
async def cancel_sync_handler(event):
    await event.answer()
    selections.pop(event.sender_id, None)
    await event.edit("ÐÑÐ±Ð¾Ñ ÐºÐ°Ð½Ð°Ð»Ð¾Ð² Ð¾ÑÐ¼ÐµÐ½ÑÐ½.", buttons=main_menu(), parse_mode=None)


@bot_client.on(events.CallbackQuery(data=b"help"))
async def help_handler(event):
    await event.answer()
    await event.respond(
        "1. ÐÐ°Ð¶Ð¼Ð¸ÑÐµ Â«ÐÐ¾Ð´ÐºÐ»ÑÑÐ¸ÑÑ Ð°ÐºÐºÐ°ÑÐ½ÑÂ» Ð¸ Ð¾ÑÑÐºÐ°Ð½Ð¸ÑÑÐ¹ÑÐµ QR-ÐºÐ¾Ð´ Ð² Telegram.\n"
        "2. ÐÐ°Ð¶Ð¼Ð¸ÑÐµ Â«ÐÐµÑÐµÐ½ÐµÑÑÐ¸ Ð¿Ð¾ÑÑÑÂ».\n"
        "3. ÐÑÐ±ÐµÑÐ¸ÑÐµ Ð¾ÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ ÐºÐ°Ð½Ð°Ð» Ð¸ ÐºÐ°Ð½Ð°Ð» Ð½Ð°Ð·Ð½Ð°ÑÐµÐ½Ð¸Ñ.\n\n"
        "Ð ÐºÐ°Ð½Ð°Ð»Ðµ Ð½Ð°Ð·Ð½Ð°ÑÐµÐ½Ð¸Ñ Ð°ÐºÐºÐ°ÑÐ½Ñ Ð´Ð¾Ð»Ð¶ÐµÐ½ Ð¸Ð¼ÐµÑÑ Ð¿ÑÐ°Ð²Ð¾ ÑÐµÐ´Ð°ÐºÑÐ¸ÑÐ¾Ð²Ð°ÑÑ Ð¿Ð¾ÑÑÑ.",
        buttons=main_menu(), parse_mode=None,
    )


@bot_client.on(events.CallbackQuery(data=b"menu"))
async def menu_handler(event):
    await event.answer()
    await show_menu(event)


@bot_client.on(events.CallbackQuery(data=b"noop"))
async def noop_handler(event):
    await event.answer()


async def main():
    if not API_HASH or not BOT_TOKEN:
        raise RuntimeError("ÐÐµ Ð·Ð°Ð¿Ð¾Ð»Ð½ÐµÐ½Ñ API_HASH Ð¸Ð»Ð¸ BOT_TOKEN")
    await init_database()
    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    log.info("ÐÐ¾Ñ @%s Ð·Ð°Ð¿ÑÑÐµÐ½", me.username)
    try:
        await bot_client.run_until_disconnected()
    finally:
        for task in list(login_tasks.values()):
            task.cancel()
        for client in list(user_clients.values()):
            await client.disconnect()
        if database:
            await database.close()


if __name__ == "__main__":
    asyncio.run(main())