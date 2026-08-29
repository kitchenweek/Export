import asyncio
import base64
import hashlib
import logging
import os
import random
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

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
SESSION_ENCRYPTION_KEY = os.getenv("SESSION_ENCRYPTION_KEY", "")
DATA_DIR = Path(os.getenv("DATA_DIR", "."))
SQLITE_PATH = DATA_DIR / "channel_sync.sqlite3"

MIN_DELAY = 1.0
MAX_DELAY = 3.0
MAX_REPORT_PART = 3900
CHANNELS_PER_PAGE = 8
QR_TIMEOUT = 180
TEXT_MESSAGE_LIMIT = 4096

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
database: Optional[sqlite3.Connection] = None
fernet: Optional[Fernet] = None
database_lock = asyncio.Lock()
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(SQLITE_PATH, check_same_thread=False)
    database.row_factory = sqlite3.Row
    database.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_user_sessions (
            bot_user_id INTEGER PRIMARY KEY,
            encrypted_session BLOB NOT NULL,
            telegram_account_id INTEGER,
            account_name TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    database.commit()
    log.info("SQLite: %s", SQLITE_PATH)


async def save_session(user_id: int, session: str, account) -> None:
    if not database or not fernet:
        raise RuntimeError("SQLite \u043d\u0435 \u0438\u043d\u0438\u0446\u0438\u0430\u043b\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u043d")
    encrypted = fernet.encrypt(session.encode("utf-8"))
    account_name = " ".join(
        part for part in (account.first_name, account.last_name) if part
    ) or account.username or str(account.id)
    async with database_lock:
        database.execute(
            """
            INSERT INTO telegram_user_sessions (
                bot_user_id, encrypted_session, telegram_account_id, account_name, updated_at
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (bot_user_id) DO UPDATE SET
                encrypted_session = excluded.encrypted_session,
                telegram_account_id = excluded.telegram_account_id,
                account_name = excluded.account_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, encrypted, account.id, account_name),
        )
        database.commit()


async def load_session(user_id: int) -> Optional[str]:
    if not database or not fernet:
        return None
    async with database_lock:
        row = database.execute(
            "SELECT encrypted_session FROM telegram_user_sessions WHERE bot_user_id = ?",
            (user_id,),
        ).fetchone()
    encrypted = row["encrypted_session"] if row else None
    if not encrypted:
        return None
    try:
        return fernet.decrypt(bytes(encrypted)).decode("utf-8")
    except InvalidToken:
        log.error("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0440\u0430\u0441\u0448\u0438\u0444\u0440\u043e\u0432\u0430\u0442\u044c \u0441\u0435\u0441\u0441\u0438\u044e \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f %s", user_id)
        return None


async def delete_session(user_id: int) -> None:
    if database:
        async with database_lock:
            database.execute(
                "DELETE FROM telegram_user_sessions WHERE bot_user_id = ?",
                (user_id,),
            )
            database.commit()


async def stored_account(user_id: int):
    if not database:
        return None
    async with database_lock:
        return database.execute(
            """
            SELECT telegram_account_id, account_name, updated_at
            FROM telegram_user_sessions WHERE bot_user_id = ?
            """,
            (user_id,),
        ).fetchone()


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
        [Button.inline("\u2795 \u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0430\u043a\u043a\u0430\u0443\u043d\u0442", b"account:add")],
        [Button.inline("\U0001f504 \u041f\u0435\u0440\u0435\u043d\u0435\u0441\u0442\u0438 \u043f\u043e\u0441\u0442\u044b", b"sync:start")],
        [Button.inline("\U0001f9f9 \u0417\u0430\u043c\u0435\u043d\u0438\u0442\u044c \u0432\u0441\u0435 \u043f\u043e\u0441\u0442\u044b \u043d\u0430 \u00ab.\u00bb", b"wipe:start")],
        [
            Button.inline("\U0001f464 \u041c\u043e\u0439 \u0430\u043a\u043a\u0430\u0443\u043d\u0442", b"account:show"),
            Button.inline("\u2699\ufe0f \u041f\u043e\u043c\u043e\u0449\u044c", b"help"),
        ],
        [Button.inline("\U0001f6aa \u041e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0430\u043a\u043a\u0430\u0443\u043d\u0442", b"account:remove")],
    ]


async def show_menu(event, text: str = "\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435:"):
    await event.respond(text, buttons=main_menu(), parse_mode=None)


async def begin_qr_login(event, user_id: int):
    old_task = login_tasks.get(user_id)
    if old_task and not old_task.done():
        await event.respond(
            "\u0412\u0445\u043e\u0434 \u0443\u0436\u0435 \u0437\u0430\u043f\u0443\u0449\u0435\u043d. \u041e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 QR-\u043a\u043e\u0434 \u0438\u043b\u0438 \u043d\u0430\u0436\u043c\u0438\u0442\u0435 \u00ab\u041e\u0442\u043c\u0435\u043d\u0430\u00bb.",
            buttons=[[Button.inline("\u041e\u0442\u043c\u0435\u043d\u0430", b"login:cancel")]],
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
                    "\u041e\u0442\u043a\u0440\u043e\u0439\u0442\u0435 Telegram \u2192 \u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u2192 \u0423\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0430 \u2192 "
                    "\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u043e \u0438 \u043e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 QR-\u043a\u043e\u0434.\n\n"
                    f"\u041a\u043e\u0434 \u0434\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442 {QR_TIMEOUT // 60} \u043c\u0438\u043d\u0443\u0442\u044b. "
                    "\u041d\u0435 \u043f\u0435\u0440\u0435\u0441\u044b\u043b\u0430\u0439\u0442\u0435 \u044d\u0442\u043e \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u0434\u0440\u0443\u0433\u0438\u043c \u043b\u044e\u0434\u044f\u043c."
                ),
                buttons=[
                    [Button.url("\U0001f4f1 \u041e\u0442\u043a\u0440\u044b\u0442\u044c \u0432\u0445\u043e\u0434 \u043d\u0430 \u044d\u0442\u043e\u043c \u0443\u0441\u0442\u0440\u043e\u0439\u0441\u0442\u0432\u0435", qr_login.url)],
                    [Button.inline("\u041e\u0442\u043c\u0435\u043d\u0430", b"login:cancel")],
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
            f"\u0410\u043a\u043a\u0430\u0443\u043d\u0442 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d: {name}",
            buttons=main_menu(),
            parse_mode=None,
        )
    except SessionPasswordNeededError:
        await bot_client.send_message(
            user_id,
            "\u041d\u0430 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0435 \u0432\u043a\u043b\u044e\u0447\u0451\u043d \u043e\u0431\u043b\u0430\u0447\u043d\u044b\u0439 \u043f\u0430\u0440\u043e\u043b\u044c Telegram. \u0412 \u0446\u0435\u043b\u044f\u0445 \u0431\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0441\u0442\u0438 "
            "\u0431\u043e\u0442 \u043d\u0435 \u043f\u0440\u043e\u0441\u0438\u0442 \u043f\u0440\u0438\u0441\u044b\u043b\u0430\u0442\u044c \u043f\u0430\u0440\u043e\u043b\u044c \u0432 \u0447\u0430\u0442. \u0414\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 \u043f\u043e\u043d\u0430\u0434\u043e\u0431\u0438\u0442\u0441\u044f "
            "\u043b\u043e\u043a\u0430\u043b\u044c\u043d\u043e\u0435 \u0441\u043e\u0437\u0434\u0430\u043d\u0438\u0435 \u0441\u0435\u0441\u0441\u0438\u0438.",
            buttons=main_menu(),
            parse_mode=None,
        )
    except asyncio.TimeoutError:
        await bot_client.send_message(
            user_id,
            "\u0412\u0440\u0435\u043c\u044f QR-\u043a\u043e\u0434\u0430 \u0438\u0441\u0442\u0435\u043a\u043b\u043e. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u00ab\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u00bb \u0438 \u043f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0441\u043d\u043e\u0432\u0430.",
            buttons=main_menu(),
            parse_mode=None,
        )
    except asyncio.CancelledError:
        await bot_client.send_message(
            user_id,
            "\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 \u043e\u0442\u043c\u0435\u043d\u0435\u043d\u043e.",
            buttons=main_menu(),
            parse_mode=None,
        )
    except Exception as error:
        log.exception("\u041e\u0448\u0438\u0431\u043a\u0430 QR-\u0432\u0445\u043e\u0434\u0430 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f %s", user_id)
        await bot_client.send_message(
            user_id,
            f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0430\u043a\u043a\u0430\u0443\u043d\u0442: {type(error).__name__}: {error}",
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
    return f"https://t.me/c/{channel_id}/{message_id}" if channel_id else "\u0441\u0441\u044b\u043b\u043a\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430"


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


def is_text_target(post: Post) -> bool:
    media = post.message.media
    return not media or isinstance(
        media,
        (types.MessageMediaWebPage, types.MessageMediaEmpty),
    )


def nearest_unused_text_target(
    source: Post,
    targets: list[Post],
    used: set[int],
) -> Optional[Post]:
    available = [
        item
        for item in targets
        if item.message.id not in used and is_text_target(item)
    ]
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


def is_caption_limit_error(error: Exception) -> bool:
    value = f"{type(error).__name__}: {error}".lower().replace("_", "")
    markers = (
        "mediacaptiontoolong",
        "captiontoolong",
        "messagetoolong",
        "caption is too long",
        "message was too long",
    )
    return any(marker in value for marker in markers)


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


async def replace_post(
    client,
    target_entity,
    target,
    source,
    temp_dir: Path,
    text_only: bool = False,
):
    media_file = None
    downloaded_path: Optional[Path] = None
    if not text_only and has_transferable_media(source.message):
        downloaded = await client.download_media(source.message, file=str(temp_dir))
        if not downloaded:
            raise RuntimeError("\u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043a\u0430\u0447\u0430\u0442\u044c \u043c\u0435\u0434\u0438\u0430 \u0438\u0441\u0445\u043e\u0434\u043d\u043e\u0433\u043e \u043f\u043e\u0441\u0442\u0430")
        downloaded_path = Path(downloaded)
        media_file = str(downloaded_path)
    elif not text_only and source.message.media and not isinstance(
        source.message.media,
        (types.MessageMediaWebPage, types.MessageMediaEmpty),
    ):
        raise RuntimeError(
            f"\u0442\u0438\u043f \u043c\u0435\u0434\u0438\u0430 {type(source.message.media).__name__} \u043d\u0435\u043b\u044c\u0437\u044f \u043f\u0435\u0440\u0435\u043d\u0435\u0441\u0442\u0438 \u0440\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435\u043c"
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
        raise RuntimeError("\u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u0438 \u0432\u0442\u043e\u0440\u043e\u0439 \u043a\u0430\u043d\u0430\u043b \u043d\u0435 \u0434\u043e\u043b\u0436\u043d\u044b \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u0442\u044c")
    source_posts, target_posts = await asyncio.gather(
        load_posts(client, source_entity),
        load_posts(client, target_entity),
    )
    if progress_callback:
        await progress_callback(
            f"\u041d\u0430\u0439\u0434\u0435\u043d\u043e \u043f\u043e\u0441\u0442\u043e\u0432: \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u043a\u0430\u043d\u0430\u043b \u2014 {len(source_posts)}, "
            f"\u0432\u0442\u043e\u0440\u043e\u0439 \u043a\u0430\u043d\u0430\u043b \u2014 {len(target_posts)}. \u041d\u0430\u0447\u0438\u043d\u0430\u044e \u0437\u0430\u043c\u0435\u043d\u0443."
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
                        "\u041e\u0428\u0418\u0411\u041a\u0410", source.link, "\u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d",
                        "\u0432\u043e \u0432\u0442\u043e\u0440\u043e\u043c \u043a\u0430\u043d\u0430\u043b\u0435 \u0437\u0430\u043a\u043e\u043d\u0447\u0438\u043b\u0438\u0441\u044c \u0441\u0432\u043e\u0431\u043e\u0434\u043d\u044b\u0435 \u043f\u043e\u0441\u0442\u044b",
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
                            "\u041f\u0420\u0415\u0414\u0423\u041f\u0420\u0415\u0416\u0414\u0415\u041d\u0418\u0415", source.link, target.link,
                            "\u0438\u0437\u043c\u0435\u043d\u0451\u043d \u043f\u0435\u0440\u0432\u044b\u0439 \u044d\u043b\u0435\u043c\u0435\u043d\u0442 \u0446\u0435\u043b\u0435\u0432\u043e\u0433\u043e \u0430\u043b\u044c\u0431\u043e\u043c\u0430; \u043e\u0441\u0442\u0430\u043b\u044c\u043d\u044b\u0435 \u043d\u0435 \u0443\u0434\u0430\u043b\u044f\u043b\u0438\u0441\u044c",
                        )
                    )
            except Exception as error:
                can_retry_as_text = (
                    bool(source.message.photo)
                    and len(source.text) <= TEXT_MESSAGE_LIMIT
                    and is_caption_limit_error(error)
                )
                if can_retry_as_text:
                    used_targets.discard(target.message.id)
                    fallback = nearest_unused_text_target(
                        source,
                        target_posts,
                        used_targets,
                    )
                    if fallback:
                        used_targets.add(fallback.message.id)
                        await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                        try:
                            await replace_post(
                                client,
                                target_entity,
                                fallback,
                                source,
                                temp_dir,
                                text_only=True,
                            )
                            changed += 1
                            issues.append(
                                SyncIssue(
                                    "\u041f\u0420\u0415\u0414\u0423\u041f\u0420\u0415\u0416\u0414\u0415\u041d\u0418\u0415",
                                    source.link,
                                    fallback.link,
                                    "\u043f\u043e\u0434\u043f\u0438\u0441\u044c \u043a \u0444\u043e\u0442\u043e\u0433\u0440\u0430\u0444\u0438\u0438 \u043f\u0440\u0435\u0432\u044b\u0441\u0438\u043b\u0430 \u043b\u0438\u043c\u0438\u0442; "
                                    "\u0442\u0435\u043a\u0441\u0442 \u043f\u0435\u0440\u0435\u043d\u0435\u0441\u0451\u043d \u0432 \u0431\u043b\u0438\u0436\u0430\u0439\u0448\u0438\u0439 \u043f\u043e\u0441\u0442 \u0431\u0435\u0437 \u043c\u0435\u0434\u0438\u0430, "
                                    "\u0444\u043e\u0442\u043e\u0433\u0440\u0430\u0444\u0438\u044f \u043f\u0440\u043e\u043f\u0443\u0449\u0435\u043d\u0430",
                                )
                            )
                            continue
                        except Exception as fallback_error:
                            used_targets.discard(fallback.message.id)
                            error = fallback_error
                            target = fallback
                log.exception("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u043c\u0435\u043d\u0438\u0442\u044c %s -> %s", source.link, target.link)
                issues.append(
                    SyncIssue(
                        "\u041e\u0428\u0418\u0411\u041a\u0410", source.link, target.link,
                        f"{type(error).__name__}: {error}",
                    )
                )
            if progress_callback and number % 50 == 0:
                await progress_callback(
                    f"\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u043d\u043e {number}/{len(source_posts)}, \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u043e {changed}."
                )
    return changed, len(source_posts), issues


def build_report(changed: int, total: int, issues: list[SyncIssue]) -> str:
    errors = sum(item.kind == "\u041e\u0428\u0418\u0411\u041a\u0410" for item in issues)
    warnings = sum(item.kind == "\u041f\u0420\u0415\u0414\u0423\u041f\u0420\u0415\u0416\u0414\u0415\u041d\u0418\u0415" for item in issues)
    lines = [
        "\u0421\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430.",
        f"\u0423\u0441\u043f\u0435\u0448\u043d\u043e \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u043e: {changed}/{total}",
        f"\u041e\u0448\u0438\u0431\u043e\u043a: {errors}",
        f"\u041f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0439: {warnings}",
    ]
    if issues:
        lines.append("\n\u0421\u043f\u0438\u0441\u043e\u043a \u043e\u0448\u0438\u0431\u043e\u043a \u0438 \u043f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0435\u043d\u0438\u0439:")
        for index, item in enumerate(issues, start=1):
            lines.extend(
                [
                    f"\n{index}. {item.kind}: {item.details}",
                    f"\u041e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u043a\u0430\u043d\u0430\u043b: {item.source_link}",
                    f"\u0412\u0442\u043e\u0440\u043e\u0439 \u043a\u0430\u043d\u0430\u043b: {item.target_link}",
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
    if state.phase in ("target", "wipe"):
        channels = [
            item for item in channels
            if can_edit_channel(item)
            and (
                state.phase != "target"
                or not state.source
                or item.id != state.source.id
            )
        ]
    total_pages = max(1, (len(channels) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * CHANNELS_PER_PAGE
    visible = channels[start : start + CHANNELS_PER_PAGE]
    buttons = []
    for channel in visible:
        icon = "\u270f\ufe0f" if can_edit_channel(channel) else "\U0001f441"
        title = (channel.title or "\u0411\u0435\u0437 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u044f")[:45]
        buttons.append(
            [Button.inline(f"{icon} {title}", f"pick:{state.phase}:{channel.id}".encode())]
        )
    navigation = []
    if page > 0:
        navigation.append(Button.inline("\u2b05\ufe0f", f"page:{state.phase}:{page - 1}".encode()))
    navigation.append(Button.inline(f"{page + 1}/{total_pages}", b"noop"))
    if page + 1 < total_pages:
        navigation.append(Button.inline("\u27a1\ufe0f", f"page:{state.phase}:{page + 1}".encode()))
    buttons.append(navigation)
    buttons.append([Button.inline("\u274c \u041e\u0442\u043c\u0435\u043d\u0430", b"sync:cancel")])
    if state.phase == "source":
        action = "\u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u043a\u0430\u043d\u0430\u043b"
    elif state.phase == "wipe":
        action = "\u043a\u0430\u043d\u0430\u043b \u0434\u043b\u044f \u0437\u0430\u043c\u0435\u043d\u044b \u0432\u0441\u0435\u0445 \u043f\u043e\u0441\u0442\u043e\u0432 \u043d\u0430 \u00ab.\u00bb"
    else:
        action = "\u0432\u0442\u043e\u0440\u043e\u0439 \u043a\u0430\u043d\u0430\u043b"
    return f"\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 {action}:\n\u270f\ufe0f \u2014 \u043c\u043e\u0436\u043d\u043e \u0438\u0437\u043c\u0435\u043d\u044f\u0442\u044c, \U0001f441 \u2014 \u0442\u043e\u043b\u044c\u043a\u043e \u0447\u0442\u0435\u043d\u0438\u0435", buttons


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
            "\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u0435 Telegram-\u0430\u043a\u043a\u0430\u0443\u043d\u0442.",
            buttons=[[Button.inline("\u2795 \u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c", b"account:add")]],
            parse_mode=None,
        )
        return
    if get_lock(sync_locks, user_id).locked():
        await event.respond("\u0423 \u0432\u0430\u0441 \u0443\u0436\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u043f\u0435\u0440\u0435\u043d\u043e\u0441.", parse_mode=None)
        return
    try:
        channels = await available_channels(client)
    except Exception as error:
        await event.respond(
            f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043a\u0430\u043d\u0430\u043b\u044b: {type(error).__name__}: {error}",
            parse_mode=None,
        )
        return
    if len(channels) < 2:
        await event.respond(
            "\u0412 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d\u043d\u043e\u043c \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0435 \u0434\u043e\u043b\u0436\u043d\u043e \u0431\u044b\u0442\u044c \u043d\u0435 \u043c\u0435\u043d\u0435\u0435 \u0434\u0432\u0443\u0445 \u043a\u0430\u043d\u0430\u043b\u043e\u0432.",
            parse_mode=None,
        )
        return
    state = ChannelSelection(channels=channels)
    selections[user_id] = state
    await show_channel_page(event, state)


async def start_wipe_selection(event, user_id: int):
    client = await get_user_client(user_id)
    if not client:
        await event.respond(
            "\u0421\u043d\u0430\u0447\u0430\u043b\u0430 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u0435 Telegram-\u0430\u043a\u043a\u0430\u0443\u043d\u0442.",
            buttons=[[Button.inline("\u2795 \u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c", b"account:add")]],
            parse_mode=None,
        )
        return
    if get_lock(sync_locks, user_id).locked():
        await event.respond(
            "\u0423 \u0432\u0430\u0441 \u0443\u0436\u0435 \u0432\u044b\u043f\u043e\u043b\u043d\u044f\u0435\u0442\u0441\u044f \u0434\u0440\u0443\u0433\u0430\u044f \u043e\u043f\u0435\u0440\u0430\u0446\u0438\u044f.",
            parse_mode=None,
        )
        return
    try:
        channels = await available_channels(client)
    except Exception as error:
        await event.respond(
            f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043a\u0430\u043d\u0430\u043b\u044b: {type(error).__name__}: {error}",
            parse_mode=None,
        )
        return
    if not any(can_edit_channel(channel) for channel in channels.values()):
        await event.respond(
            "\u041d\u0435\u0442 \u043a\u0430\u043d\u0430\u043b\u043e\u0432, \u0432 \u043a\u043e\u0442\u043e\u0440\u044b\u0445 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u043c\u043e\u0436\u0435\u0442 \u0440\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u043f\u043e\u0441\u0442\u044b.",
            parse_mode=None,
        )
        return
    state = ChannelSelection(channels=channels, phase="wipe")
    selections[user_id] = state
    await show_channel_page(event, state)


async def edit_dot_with_retry(client, entity, message):
    try:
        await client.edit_message(entity, message.id, ".")
    except FloodWaitError as error:
        await asyncio.sleep(error.seconds + 1)
        await client.edit_message(entity, message.id, ".")


async def wipe_channel(event, user_id: int, channel):
    lock = get_lock(sync_locks, user_id)
    async with lock:
        client = await get_user_client(user_id)
        if not client:
            await event.respond(
                "\u0421\u0435\u0441\u0441\u0438\u044f \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 \u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u044c\u043d\u0430.",
                parse_mode=None,
            )
            return
        entity = await client.get_entity(channel)
        changed = 0
        skipped = 0
        processed = 0
        errors = []
        async for message in client.iter_messages(entity, reverse=True):
            if not is_real_post(message):
                continue
            processed += 1
            if (message.message or "") == ".":
                skipped += 1
                continue
            await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            try:
                await edit_dot_with_retry(client, entity, message)
                changed += 1
            except Exception as error:
                errors.append(
                    (
                        message_link(entity, message.id),
                        f"{type(error).__name__}: {error}",
                    )
                )
            if processed % 50 == 0:
                await event.respond(
                    f"\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u0430\u043d\u043e: {processed}, \u0438\u0437\u043c\u0435\u043d\u0435\u043d\u043e: {changed}.",
                    parse_mode=None,
                )
        lines = [
            "\u0417\u0430\u043c\u0435\u043d\u0430 \u043f\u043e\u0441\u0442\u043e\u0432 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430.",
            f"\u0418\u0437\u043c\u0435\u043d\u0435\u043d\u043e: {changed}",
            f"\u0423\u0436\u0435 \u0431\u044b\u043b\u043e \u00ab.\u00bb: {skipped}",
            f"\u041e\u0448\u0438\u0431\u043e\u043a: {len(errors)}",
        ]
        for index, (link, details) in enumerate(errors, start=1):
            lines.extend(
                [
                    f"\n{index}. {details}",
                    f"\u041f\u043e\u0441\u0442: {link}",
                ]
            )
        for part in split_report("\n".join(lines)):
            await event.respond(part, parse_mode=None, link_preview=False)
        await show_menu(event)


async def execute_sync(event, user_id: int, source, target):
    lock = get_lock(sync_locks, user_id)
    async with lock:
        client = await get_user_client(user_id)
        if not client:
            await event.respond("\u0421\u0435\u0441\u0441\u0438\u044f \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 \u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u044c\u043d\u0430.", parse_mode=None)
            return
        await event.respond(
            f"\u041e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u043a\u0430\u043d\u0430\u043b: {source.title}\n"
            f"\u0412\u0442\u043e\u0440\u043e\u0439 \u043a\u0430\u043d\u0430\u043b: {target.title}\n"
            "\u0417\u0430\u0433\u0440\u0443\u0436\u0430\u044e \u043f\u043e\u0441\u0442\u044b\u2026",
            parse_mode=None,
        )

        async def progress(text: str):
            await event.respond(text, parse_mode=None)

        try:
            changed, total, issues = await run_sync(client, source, target, progress)
            report = build_report(changed, total, issues)
        except Exception as error:
            log.exception("\u041a\u0440\u0438\u0442\u0438\u0447\u0435\u0441\u043a\u0430\u044f \u043e\u0448\u0438\u0431\u043a\u0430 \u0441\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u0438 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f %s", user_id)
            report = f"\u0421\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0430\u0446\u0438\u044f \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0430:\n{type(error).__name__}: {error}"
        for part in split_report(report):
            await event.respond(part, link_preview=False, parse_mode=None)
        await show_menu(event)


@bot_client.on(events.NewMessage(pattern=r"^/(start|menu)(?:@\w+)?$"))
async def start_handler(event):
    if event.is_private:
        await show_menu(
            event,
            "\u0411\u043e\u0442 \u043f\u0435\u0440\u0435\u043d\u043e\u0441\u0438\u0442 \u0441\u043e\u0434\u0435\u0440\u0436\u0438\u043c\u043e\u0435 \u043f\u043e\u0441\u0442\u043e\u0432 \u043c\u0435\u0436\u0434\u0443 \u0432\u0430\u0448\u0438\u043c\u0438 \u043a\u0430\u043d\u0430\u043b\u0430\u043c\u0438 \u043f\u043e \u0431\u043b\u0438\u0436\u0430\u0439\u0448\u0438\u043c \u0434\u0430\u0442\u0430\u043c.",
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
            "\u0414\u043e\u0436\u0434\u0438\u0442\u0435\u0441\u044c \u043e\u043a\u043e\u043d\u0447\u0430\u043d\u0438\u044f \u0442\u0435\u043a\u0443\u0449\u0435\u0433\u043e \u043f\u0435\u0440\u0435\u043d\u043e\u0441\u0430 \u043f\u0435\u0440\u0435\u0434 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435\u043c \u0434\u0440\u0443\u0433\u043e\u0433\u043e \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430.",
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
        await show_menu(event, "\u0410\u043a\u0442\u0438\u0432\u043d\u043e\u0433\u043e \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u044f \u043d\u0435\u0442.")


@bot_client.on(events.CallbackQuery(data=b"account:show"))
async def show_account_handler(event):
    await event.answer()
    row = await stored_account(event.sender_id)
    if not row:
        await event.respond(
            "Telegram-\u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0435\u0449\u0451 \u043d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d.", buttons=main_menu(), parse_mode=None
        )
        return
    await event.respond(
        f"\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d \u0430\u043a\u043a\u0430\u0443\u043d\u0442: {row['account_name']}\n"
        f"Telegram ID: {row['telegram_account_id']}",
        buttons=main_menu(), parse_mode=None,
    )


@bot_client.on(events.CallbackQuery(data=b"account:remove"))
async def remove_account_question(event):
    await event.answer()
    await event.respond(
        "\u041e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0430\u043a\u043a\u0430\u0443\u043d\u0442? \u0422\u0435\u043a\u0443\u0449\u0430\u044f \u0441\u0435\u0441\u0441\u0438\u044f \u0431\u0443\u0434\u0435\u0442 \u043e\u0442\u043e\u0437\u0432\u0430\u043d\u0430 \u0432 Telegram.",
        buttons=[[
            Button.inline("\u0414\u0430, \u043e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c", b"account:remove:yes"),
            Button.inline("\u041e\u0442\u043c\u0435\u043d\u0430", b"menu"),
        ]],
        parse_mode=None,
    )


@bot_client.on(events.CallbackQuery(data=b"account:remove:yes"))
async def remove_account_handler(event):
    await event.answer()
    user_id = event.sender_id
    if get_lock(sync_locks, user_id).locked():
        await event.respond(
            "\u041d\u0435\u043b\u044c\u0437\u044f \u043e\u0442\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0432\u043e \u0432\u0440\u0435\u043c\u044f \u043f\u0435\u0440\u0435\u043d\u043e\u0441\u0430. \u0414\u043e\u0436\u0434\u0438\u0442\u0435\u0441\u044c \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0438\u044f.",
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
                f"\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u0442\u043e\u0437\u0432\u0430\u0442\u044c \u0441\u0435\u0441\u0441\u0438\u044e: {type(error).__name__}: {error}",
                buttons=main_menu(),
                parse_mode=None,
            )
            return
    await delete_session(user_id)
    selections.pop(user_id, None)
    await show_menu(event, "\u0410\u043a\u043a\u0430\u0443\u043d\u0442 \u043e\u0442\u043a\u043b\u044e\u0447\u0451\u043d, \u0441\u0435\u0441\u0441\u0438\u044f \u0443\u0434\u0430\u043b\u0435\u043d\u0430.")


@bot_client.on(events.CallbackQuery(data=b"sync:start"))
async def sync_button_handler(event):
    await event.answer()
    await start_channel_selection(event, event.sender_id)


@bot_client.on(events.CallbackQuery(data=b"wipe:start"))
async def wipe_button_handler(event):
    await event.answer()
    await start_wipe_selection(event, event.sender_id)


@bot_client.on(events.CallbackQuery(pattern=rb"page:(source|target|wipe):(\d+)"))
async def channel_page_handler(event):
    await event.answer()
    state = selections.get(event.sender_id)
    if not state:
        await show_menu(event, "\u0412\u044b\u0431\u043e\u0440 \u0443\u0441\u0442\u0430\u0440\u0435\u043b. \u041d\u0430\u0447\u043d\u0438\u0442\u0435 \u0437\u0430\u043d\u043e\u0432\u043e.")
        return
    phase, page = event.data.decode().split(":")[1:]
    if phase != state.phase:
        await event.answer("\u042d\u0442\u043e\u0442 \u0441\u043f\u0438\u0441\u043e\u043a \u0443\u0436\u0435 \u0443\u0441\u0442\u0430\u0440\u0435\u043b", alert=True)
        return
    await show_channel_page(event, state, int(page), edit=True)


@bot_client.on(events.CallbackQuery(pattern=rb"pick:(source|target|wipe):(\d+)"))
async def channel_pick_handler(event):
    await event.answer()
    user_id = event.sender_id
    state = selections.get(user_id)
    if not state:
        await show_menu(event, "\u0412\u044b\u0431\u043e\u0440 \u0443\u0441\u0442\u0430\u0440\u0435\u043b. \u041d\u0430\u0447\u043d\u0438\u0442\u0435 \u0437\u0430\u043d\u043e\u0432\u043e.")
        return
    _, phase, channel_id_text = event.data.decode().split(":")
    if phase != state.phase:
        await event.answer("\u042d\u0442\u043e\u0442 \u0441\u043f\u0438\u0441\u043e\u043a \u0443\u0436\u0435 \u0443\u0441\u0442\u0430\u0440\u0435\u043b", alert=True)
        return
    channel = state.channels.get(int(channel_id_text))
    if not channel:
        await event.answer("\u041a\u0430\u043d\u0430\u043b \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d", alert=True)
        return
    if phase == "source":
        state.source = channel
        state.phase = "target"
        await show_channel_page(event, state, 0, edit=True)
        return
    if phase == "wipe":
        state.source = channel
        state.phase = "wipe_confirm"
        await event.edit(
            f"\u041a\u0430\u043d\u0430\u043b: {channel.title}\n\n"
            "\u0412\u0441\u0435 \u0442\u0435\u043a\u0441\u0442\u044b \u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u0438 \u043f\u043e\u0441\u0442\u043e\u0432 \u0431\u0443\u0434\u0443\u0442 \u0437\u0430\u043c\u0435\u043d\u0435\u043d\u044b \u043d\u0430 \u00ab.\u00bb. "
            "\u041c\u0435\u0434\u0438\u0430 \u043e\u0441\u0442\u0430\u043d\u0443\u0442\u0441\u044f. \u0414\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043d\u0435\u043e\u0431\u0440\u0430\u0442\u0438\u043c\u043e.\n\n"
            "\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c?",
            buttons=[
                [Button.inline(
                    "\u0414\u0430, \u0437\u0430\u043c\u0435\u043d\u0438\u0442\u044c \u0432\u0441\u0435 \u043f\u043e\u0441\u0442\u044b",
                    f"wipe:confirm:{channel.id}".encode(),
                )],
                [Button.inline("\u274c \u041e\u0442\u043c\u0435\u043d\u0430", b"sync:cancel")],
            ],
            parse_mode=None,
        )
        return
    source = state.source
    selections.pop(user_id, None)
    if not source:
        await show_menu(event, "\u041e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u043a\u0430\u043d\u0430\u043b \u043d\u0435 \u0432\u044b\u0431\u0440\u0430\u043d. \u041d\u0430\u0447\u043d\u0438\u0442\u0435 \u0437\u0430\u043d\u043e\u0432\u043e.")
        return
    await event.edit(
        f"\u0412\u044b\u0431\u0440\u0430\u043d\u043e:\n{source.title} \u2192 {channel.title}\n\n\u041d\u0430\u0447\u0438\u043d\u0430\u044e \u043f\u0435\u0440\u0435\u043d\u043e\u0441\u2026",
        buttons=None, parse_mode=None,
    )
    await execute_sync(event, user_id, source, channel)


@bot_client.on(events.CallbackQuery(pattern=rb"wipe:confirm:(\d+)"))
async def wipe_confirm_handler(event):
    await event.answer()
    user_id = event.sender_id
    state = selections.get(user_id)
    if not state or state.phase != "wipe_confirm" or not state.source:
        await show_menu(
            event,
            "\u0412\u044b\u0431\u043e\u0440 \u0443\u0441\u0442\u0430\u0440\u0435\u043b. \u041d\u0430\u0447\u043d\u0438\u0442\u0435 \u0437\u0430\u043d\u043e\u0432\u043e.",
        )
        return
    channel_id = int(event.data.decode().rsplit(":", 1)[1])
    if state.source.id != channel_id:
        await event.answer("\u041a\u0430\u043d\u0430\u043b \u043d\u0435 \u0441\u043e\u0432\u043f\u0430\u0434\u0430\u0435\u0442", alert=True)
        return
    channel = state.source
    selections.pop(user_id, None)
    await event.edit(
        f"\u041a\u0430\u043d\u0430\u043b: {channel.title}\n\n\u041d\u0430\u0447\u0438\u043d\u0430\u044e \u0437\u0430\u043c\u0435\u043d\u0443 \u043f\u043e\u0441\u0442\u043e\u0432\u2026",
        buttons=None,
        parse_mode=None,
    )
    await wipe_channel(event, user_id, channel)


@bot_client.on(events.CallbackQuery(data=b"sync:cancel"))
async def cancel_sync_handler(event):
    await event.answer()
    selections.pop(event.sender_id, None)
    await event.edit("\u0412\u044b\u0431\u043e\u0440 \u043a\u0430\u043d\u0430\u043b\u043e\u0432 \u043e\u0442\u043c\u0435\u043d\u0451\u043d.", buttons=main_menu(), parse_mode=None)


@bot_client.on(events.CallbackQuery(data=b"help"))
async def help_handler(event):
    await event.answer()
    await event.respond(
        "1. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u00ab\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u00bb \u0438 \u043e\u0442\u0441\u043a\u0430\u043d\u0438\u0440\u0443\u0439\u0442\u0435 QR-\u043a\u043e\u0434 \u0432 Telegram.\n"
        "2. \u041d\u0430\u0436\u043c\u0438\u0442\u0435 \u00ab\u041f\u0435\u0440\u0435\u043d\u0435\u0441\u0442\u0438 \u043f\u043e\u0441\u0442\u044b\u00bb.\n"
        "3. \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u043a\u0430\u043d\u0430\u043b \u0438 \u043a\u0430\u043d\u0430\u043b \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f.\n\n"
        "\u0412 \u043a\u0430\u043d\u0430\u043b\u0435 \u043d\u0430\u0437\u043d\u0430\u0447\u0435\u043d\u0438\u044f \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0434\u043e\u043b\u0436\u0435\u043d \u0438\u043c\u0435\u0442\u044c \u043f\u0440\u0430\u0432\u043e \u0440\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u043f\u043e\u0441\u0442\u044b.",
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
        raise RuntimeError("\u041d\u0435 \u0437\u0430\u043f\u043e\u043b\u043d\u0435\u043d\u044b API_HASH \u0438\u043b\u0438 BOT_TOKEN")
    await init_database()
    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    log.info("\u0411\u043e\u0442 @%s \u0437\u0430\u043f\u0443\u0449\u0435\u043d", me.username)
    try:
        await bot_client.run_until_disconnected()
    finally:
        for task in list(login_tasks.values()):
            task.cancel()
        for client in list(user_clients.values()):
            await client.disconnect()
        if database:
            database.close()


if __name__ == "__main__":
    asyncio.run(main())