import asyncio
import logging
import re
from datetime import timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageService


# =========================
# НАСТРОЙКИ
# =========================

API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"
BOT_TOKEN = "8498016557:AAFwjnX1Zcp96e1PCWVvmFplpmdEVUJMNZg"

# Telegram ID администратора, которому разрешено использовать бота
ADMIN_ID = 7517164478

# Имя файла пользовательской Telegram-сессии
SESSION_NAME = "telegram_user"

# Папка для временных TXT-файлов
EXPORT_DIR = Path("exports")

# Максимальный размер одной части файла
MAX_PART_SIZE = 45 * 1024 * 1024


# =========================
# ИНИЦИАЛИЗАЦИЯ
# =========================

EXPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)
router = Router()
export_locks: dict[int, asyncio.Lock] = {}


def is_allowed(user_id: int) -> bool:
    return user_id == ADMIN_ID


def safe_filename(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", value).strip(" ._")
    return value[:80] or "telegram_chat"


def message_text(message) -> str:
    if isinstance(message, MessageService):
        return f"[СЛУЖЕБНОЕ СООБЩЕНИЕ: {message.action}]"

    text = message.message or ""

    if message.media:
        media_name = type(message.media).__name__
        text = f"{text}\n[ВЛОЖЕНИЕ: {media_name}]".strip()

    if not text:
        text = "[ПУСТОЕ СООБЩЕНИЕ]"

    return text


async def sender_name(message) -> str:
    sender = await message.get_sender()

    if sender is None:
        return "Неизвестный отправитель"

    first_name = getattr(sender, "first_name", None)
    last_name = getattr(sender, "last_name", None)
    username = getattr(sender, "username", None)
    sender_id = getattr(sender, "id", None)

    name = " ".join(
        part for part in [first_name, last_name] if part
    ).strip()

    if username:
        name = f"{name} (@{username})".strip()

    return name or str(sender_id or "Неизвестно")


async def resolve_entity(client: TelegramClient, target: str):
    target = target.strip()

    if re.fullmatch(r"-?\d+", target):
        return await client.get_entity(int(target))

    target = target.replace("https://t.me/", "")
    target = target.replace("http://t.me/", "")
    target = target.replace("t.me/", "")
    target = target.strip("/")

    return await client.get_entity(target)


def split_file(path: Path) -> list[Path]:
    if path.stat().st_size <= MAX_PART_SIZE:
        return [path]

    parts: list[Path] = []
    part_number = 1
    current_size = 0

    current_path = path.with_name(
        f"{path.stem}_part_{part_number}.txt"
    )
    current = current_path.open("w", encoding="utf-8")
    parts.append(current_path)

    with path.open("r", encoding="utf-8") as source:
        for line in source:
            line_size = len(line.encode("utf-8"))

            if (
                current_size + line_size > MAX_PART_SIZE
                and current_size > 0
            ):
                current.close()

                part_number += 1
                current_size = 0

                current_path = path.with_name(
                    f"{path.stem}_part_{part_number}.txt"
                )
                current = current_path.open(
                    "w",
                    encoding="utf-8",
                )
                parts.append(current_path)

            current.write(line)
            current_size += line_size

    current.close()
    path.unlink(missing_ok=True)

    return parts


async def export_chat(
    client: TelegramClient,
    target: str,
    progress: Optional[Message] = None,
) -> list[Path]:
    entity = await resolve_entity(client, target)

    title = (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or "telegram_chat"
    )

    output = EXPORT_DIR / f"{safe_filename(title)}.txt"
    count = 0

    with output.open("w", encoding="utf-8") as file:
        file.write(f"Чат: {title}\n")
        file.write(
            f"ID: {getattr(entity, 'id', 'неизвестно')}\n"
        )
        file.write("=" * 80 + "\n\n")

        try:
            async for msg in client.iter_messages(
                entity,
                reverse=True,
            ):
                count += 1

                date = msg.date.astimezone(
                    timezone.utc
                ).strftime("%d.%m.%Y %H:%M:%S UTC")

                sender = await sender_name(msg)

                reply = ""
                if msg.reply_to_msg_id:
                    reply = (
                        f" | ответ на сообщение "
                        f"#{msg.reply_to_msg_id}"
                    )

                text = message_text(msg)

                file.write(
                    f"[{date}] #{msg.id} | "
                    f"{sender}{reply}\n"
                )
                file.write(text)
                file.write("\n" + "-" * 80 + "\n")

                if progress and count % 1000 == 0:
                    try:
                        await progress.edit_text(
                            "Выгружено сообщений: "
                            f"{count:,}".replace(",", " ")
                        )
                    except Exception:
                        pass

        except FloodWaitError as error:
            await asyncio.sleep(error.seconds)
            return await export_chat(
                client,
                target,
                progress,
            )

    return split_file(output)


@router.message(CommandStart())
async def start(message: Message):
    if not message.from_user:
        return

    if not is_allowed(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    await message.answer(
        "Бот готов.\n\n"
        "Отправьте команду:\n"
        "/export @username\n"
        "/export https://t.me/username\n"
        "/export -1001234567890\n\n"
        "При первом запуске в консоли потребуется "
        "войти в пользовательский Telegram-аккаунт."
    )


@router.message(Command("export"))
async def export_command(
    message: Message,
    client: TelegramClient,
):
    if not message.from_user:
        return

    if not is_allowed(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    args = message.text.split(maxsplit=1) if message.text else []

    if len(args) < 2:
        await message.answer(
            "Укажите чат после команды.\n\n"
            "Пример:\n"
            "/export @username"
        )
        return

    target = args[1].strip()

    lock = export_locks.setdefault(
        message.from_user.id,
        asyncio.Lock(),
    )

    if lock.locked():
        await message.answer(
            "Для вас уже выполняется выгрузка."
        )
        return

    async with lock:
        progress = await message.answer(
            "Начинаю выгрузку…"
        )

        paths: list[Path] = []

        try:
            paths = await export_chat(
                client,
                target,
                progress,
            )

            await progress.edit_text(
                "Выгрузка завершена. Отправляю файл…"
            )

            for file_path in paths:
                await message.answer_document(
                    FSInputFile(file_path),
                    caption=f"Готово: {file_path.name}",
                )

            await progress.delete()

        except Exception as error:
            logger.exception("Ошибка выгрузки")

            await progress.edit_text(
                "Не удалось выгрузить чат.\n\n"
                f"Ошибка: {type(error).__name__}: {error}"
            )

        finally:
            for file_path in paths:
                file_path.unlink(missing_ok=True)


async def main():
    client = TelegramClient(
        SESSION_NAME,
        API_ID,
        API_HASH,
    )

    # При первом запуске Telethon сам попросит:
    # номер телефона, код из Telegram и пароль 2FA
    await client.start()

    me = await client.get_me()

    logger.info(
        "Пользовательская сессия запущена: %s",
        getattr(me, "id", "неизвестно"),
    )

    bot = Bot(BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    try:
        await dispatcher.start_polling(
            bot,
            client=client,
        )
    finally:
        await bot.session.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())