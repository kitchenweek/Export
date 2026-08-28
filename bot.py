import argparse
import asyncio
import logging
import os
import random
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from telethon import TelegramClient, events, types
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.custom import Message


# Можно изменить здесь или передать через переменные окружения.
API_ID = int(os.getenv("API_ID", "32200104"))
API_HASH = os.getenv("API_HASH", "4c657a43a0c2419cd5b18c44d09e68c1")
BOT_TOKEN = os.getenv(
    "BOT_TOKEN",
    "8797332751:AAE_WMFhyYtNXrhyIq-xCky50Dzynlz3Xco",
)

# Обязательно заполните. Примеры: @source_channel, -1001234567890.
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "@source_channel")
TARGET_CHANNEL = os.getenv("TARGET_CHANNEL", "@target_channel")

# Ваш цифровой Telegram ID. Узнать его можно у @userinfobot.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Сессия пользовательского аккаунта, который состоит в основном канале и имеет
# право редактировать второй. Создаётся командой: python bot.py --login
USER_SESSION = os.getenv("USER_SESSION", "")

MIN_DELAY = 1.0
MAX_DELAY = 3.0
MAX_REPORT_PART = 3900

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
        msg = self.caption_message or self.message
        return msg.message or ""

    @property
    def entities(self):
        msg = self.caption_message or self.message
        return msg.entities


@dataclass
class SyncIssue:
    kind: str
    source_link: str
    target_link: str
    details: str


bot_client = TelegramClient("channel_sync_bot", API_ID, API_HASH)
worker_client = TelegramClient(StringSession(USER_SESSION), API_ID, API_HASH)
sync_lock = asyncio.Lock()


def message_link(entity, message_id: int) -> str:
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}/{message_id}"

    channel_id = str(getattr(entity, "id", ""))
    if channel_id:
        return f"https://t.me/c/{channel_id}/{message_id}"
    return "ссылка недоступна"


def is_real_post(message: Message) -> bool:
    return bool(message and not message.action and (message.message or message.media))


def first_photo(messages: list[Message]) -> Message:
    ordered = sorted(messages, key=lambda item: item.id)
    return next((item for item in ordered if item.photo), ordered[0])


def has_transferable_media(message: Message) -> bool:
    return bool(message.photo or message.document)


async def load_posts(channel) -> list[Post]:
    entity = await worker_client.get_entity(channel)
    singles: list[Message] = []
    albums: dict[int, list[Message]] = {}

    async for message in worker_client.iter_messages(entity, reverse=True):
        if not is_real_post(message):
            continue
        if message.grouped_id:
            albums.setdefault(message.grouped_id, []).append(message)
        else:
            singles.append(message)

    posts: list[Post] = []
    for message in singles:
        posts.append(
            Post(
                message=message,
                date=message.date,
                link=message_link(entity, message.id),
            )
        )

    for album_messages in albums.values():
        ordered = sorted(album_messages, key=lambda item: item.id)
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


async def edit_with_flood_retry(target_entity, target: Post, source: Post, file):
    kwargs = {
        "entity": target_entity,
        "message": target.message.id,
        "text": source.text,
        "formatting_entities": source.entities,
        "file": file,
        "link_preview": True,
    }
    try:
        await worker_client.edit_message(**kwargs)
    except FloodWaitError as error:
        log.warning("FloodWait: ждём %s секунд", error.seconds)
        await asyncio.sleep(error.seconds + 1)
        await worker_client.edit_message(**kwargs)


async def replace_post(target_entity, target: Post, source: Post, temp_dir: Path):
    media_file = None
    downloaded_path: Optional[Path] = None
    if has_transferable_media(source.message):
        downloaded = await worker_client.download_media(
            source.message,
            file=str(temp_dir),
        )
        if not downloaded:
            raise RuntimeError("не удалось скачать медиа исходного поста")
        downloaded_path = Path(downloaded)
        media_file = str(downloaded_path)
    elif source.message.media and not isinstance(
        source.message.media,
        (types.MessageMediaWebPage, types.MessageMediaEmpty),
    ):
        raise RuntimeError(
            f"тип медиа {type(source.message.media).__name__} нельзя перенести редактированием"
        )
    elif has_transferable_media(target.message):
        # Убираем прежнее медиа, когда исходный пост содержит только текст.
        media_file = types.InputMediaEmpty()

    try:
        await edit_with_flood_retry(target_entity, target, source, media_file)
    finally:
        if downloaded_path:
            downloaded_path.unlink(missing_ok=True)


async def run_sync(progress_callback=None):
    source_entity = await worker_client.get_entity(SOURCE_CHANNEL)
    target_entity = await worker_client.get_entity(TARGET_CHANNEL)
    if source_entity.id == target_entity.id:
        raise RuntimeError("основной и второй канал не должны быть одним каналом")
    source_posts, target_posts = await asyncio.gather(
        load_posts(source_entity),
        load_posts(target_entity),
    )

    if progress_callback:
        await progress_callback(
            f"Найдено постов: исходный канал — {len(source_posts)}, "
            f"второй канал — {len(target_posts)}. Начинаю замену."
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
                        kind="ОШИБКА",
                        source_link=source.link,
                        target_link="не найден",
                        details="во втором канале закончились свободные посты",
                    )
                )
                continue

            used_targets.add(target.message.id)
            await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

            try:
                await replace_post(target_entity, target, source, temp_dir)
                changed += 1
                if target.album_size > 1:
                    issues.append(
                        SyncIssue(
                            kind="ПРЕДУПРЕЖДЕНИЕ",
                            source_link=source.link,
                            target_link=target.link,
                            details=(
                                "целевой пост был альбомом: изменён его первый элемент, "
                                "остальные элементы альбома не удалялись"
                            ),
                        )
                    )
            except Exception as error:
                log.exception("Не удалось заменить %s -> %s", source.link, target.link)
                issues.append(
                    SyncIssue(
                        kind="ОШИБКА",
                        source_link=source.link,
                        target_link=target.link,
                        details=f"{type(error).__name__}: {error}",
                    )
                )

            if progress_callback and number % 50 == 0:
                await progress_callback(
                    f"Обработано {number}/{len(source_posts)}, успешно изменено {changed}."
                )

    return changed, len(source_posts), issues


def build_report(changed: int, total: int, issues: list[SyncIssue]) -> str:
    error_count = sum(item.kind == "ОШИБКА" for item in issues)
    warning_count = sum(item.kind == "ПРЕДУПРЕЖДЕНИЕ" for item in issues)
    lines = [
        "Синхронизация завершена.",
        f"Успешно изменено: {changed}/{total}",
        f"Ошибок: {error_count}",
        f"Предупреждений: {warning_count}",
    ]

    if issues:
        lines.append("\nСписок ошибок и предупреждений:")
        for index, item in enumerate(issues, start=1):
            lines.extend(
                [
                    f"\n{index}. {item.kind}: {item.details}",
                    f"Основной канал: {item.source_link}",
                    f"Второй канал: {item.target_link}",
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


def authorized(event) -> bool:
    return bool(ADMIN_ID and event.is_private and event.sender_id == ADMIN_ID)


@bot_client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
async def start_handler(event):
    if not authorized(event):
        await event.respond(
            "Доступ запрещён. Владелец должен указать свой ADMIN_ID в настройках.",
            parse_mode=None,
        )
        return
    await event.respond(
        "Бот готов. Команда /sync запускает перенос, /status показывает настройки.",
        parse_mode=None,
    )


@bot_client.on(events.NewMessage(pattern=r"^/status(?:@\w+)?$"))
async def status_handler(event):
    if not authorized(event):
        return
    await event.respond(
        "Настройки:\n"
        f"Основной канал: {SOURCE_CHANNEL}\n"
        f"Второй канал: {TARGET_CHANNEL}\n"
        f"Задержка: {MIN_DELAY:.2f}–{MAX_DELAY:.2f} сек.",
        parse_mode=None,
    )


@bot_client.on(events.NewMessage(pattern=r"^/sync(?:@\w+)?$"))
async def sync_handler(event):
    if not authorized(event):
        return
    if sync_lock.locked():
        await event.respond("Синхронизация уже выполняется.", parse_mode=None)
        return

    async with sync_lock:
        await event.respond("Загружаю списки постов…", parse_mode=None)

        async def send_progress(text: str):
            await event.respond(text, parse_mode=None)

        try:
            changed, total, issues = await run_sync(send_progress)
            report = build_report(changed, total, issues)
        except Exception as error:
            log.exception("Критическая ошибка синхронизации")
            report = (
                "Синхронизация остановлена критической ошибкой:\n"
                f"{type(error).__name__}: {error}"
            )

        for part in split_report(report):
            await event.respond(part, link_preview=False, parse_mode=None)


async def create_user_session():
    login_client = TelegramClient(StringSession(), API_ID, API_HASH)
    await login_client.start()
    session_string = login_client.session.save()
    print("\nUSER_SESSION (никому не показывайте эту строку):")
    print(session_string)
    print("\nСохраните её в переменной окружения USER_SESSION.")
    await login_client.disconnect()


async def main():
    if not API_HASH or not BOT_TOKEN:
        raise RuntimeError("Не заполнены API_HASH или BOT_TOKEN")
    if ADMIN_ID <= 0:
        raise RuntimeError("Укажите ADMIN_ID — ваш цифровой Telegram ID")
    if SOURCE_CHANNEL == "@source_channel" or TARGET_CHANNEL == "@target_channel":
        raise RuntimeError("Укажите SOURCE_CHANNEL и TARGET_CHANNEL")
    if not USER_SESSION:
        raise RuntimeError(
            "Не заполнен USER_SESSION. Один раз выполните локально: python bot.py --login"
        )

    await worker_client.connect()
    if not await worker_client.is_user_authorized():
        raise RuntimeError("USER_SESSION недействительна или отозвана")

    await bot_client.start(bot_token=BOT_TOKEN)
    me = await bot_client.get_me()
    worker = await worker_client.get_me()
    log.info("Бот @%s запущен; рабочий аккаунт: %s", me.username, worker.id)
    try:
        await bot_client.run_until_disconnected()
    finally:
        await worker_client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Синхронизация постов двух каналов")
    parser.add_argument(
        "--login",
        action="store_true",
        help="создать USER_SESSION для пользовательского аккаунта",
    )
    arguments = parser.parse_args()
    asyncio.run(create_user_session() if arguments.login else main())