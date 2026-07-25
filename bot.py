import asyncio
import logging
import re
from datetime import timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, Message
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.types import MessageService


# =========================
# НАСТРОЙКИ
# =========================

API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"
BOT_TOKEN = "8498016557:AAFwjnX1Zcp96e1PCWVvmFplpmdEVUJMNZg"

# Telegram ID администратора
ADMIN_ID = 7517164478

SESSION_NAME = "telegram_user"
EXPORT_DIR = Path("exports")
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

telegram_client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH,
)


class LoginStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


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


async def resolve_entity(target: str):
    target = target.strip()

    if re.fullmatch(r"-?\d+", target):
        return await telegram_client.get_entity(int(target))

    target = target.replace("https://t.me/", "")
    target = target.replace("http://t.me/", "")
    target = target.replace("t.me/", "")
    target = target.strip("/")

    return await telegram_client.get_entity(target)


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
                current = current_path.open("w", encoding="utf-8")
                parts.append(current_path)

            current.write(line)
            current_size += line_size

    current.close()
    path.unlink(missing_ok=True)

    return parts


async def export_chat(
    target: str,
    progress: Optional[Message] = None,
) -> list[Path]:
    entity = await resolve_entity(target)

    title = (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or "telegram_chat"
    )

    output = EXPORT_DIR / f"{safe_filename(title)}.txt"
    count = 0

    with output.open("w", encoding="utf-8") as file:
        file.write(f"Чат: {title}\n")
        file.write(f"ID: {getattr(entity, 'id', 'неизвестно')}\n")
        file.write("=" * 80 + "\n\n")

        try:
            async for msg in telegram_client.iter_messages(
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
            return await export_chat(target, progress)

    return split_file(output)


@router.message(CommandStart())
async def start(message: Message):
    if not message.from_user:
        return

    if not is_allowed(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    authorized = await telegram_client.is_user_authorized()

    status = (
        "Пользовательский аккаунт подключён."
        if authorized
        else "Пользовательский аккаунт не подключён."
    )

    await message.answer(
        f"{status}\n\n"
        "Команды:\n"
        "/login — войти в Telegram-аккаунт\n"
        "/status — проверить авторизацию\n"
        "/logout — выйти из пользовательского аккаунта\n"
        "/export @username — выгрузить чат\n"
        "/cancel — отменить текущую операцию"
    )


@router.message(Command("status"))
async def status_command(message: Message):
    if not message.from_user or not is_allowed(message.from_user.id):
        return

    authorized = await telegram_client.is_user_authorized()

    if not authorized:
        await message.answer(
            "Пользовательский аккаунт не авторизован.\n"
            "Используйте /login."
        )
        return

    me = await telegram_client.get_me()

    name = " ".join(
        part
        for part in [
            getattr(me, "first_name", None),
            getattr(me, "last_name", None),
        ]
        if part
    ).strip()

    username = getattr(me, "username", None)

    await message.answer(
        "Аккаунт подключён.\n\n"
        f"Имя: {name or 'не указано'}\n"
        f"Username: @{username}" if username else
        "Аккаунт подключён.\n\n"
        f"Имя: {name or 'не указано'}\n"
        "Username: не указан"
    )


@router.message(Command("login"))
async def login_command(message: Message, state: FSMContext):
    if not message.from_user or not is_allowed(message.from_user.id):
        return

    if await telegram_client.is_user_authorized():
        await message.answer(
            "Пользовательский аккаунт уже авторизован."
        )
        return

    await state.clear()
    await state.set_state(LoginStates.waiting_phone)

    await message.answer(
        "Отправьте номер телефона пользовательского "
        "Telegram-аккаунта в международном формате.\n\n"
        "Пример: +79991234567"
    )


@router.message(LoginStates.waiting_phone, F.text)
async def login_phone(message: Message, state: FSMContext):
    if not message.from_user or not is_allowed(message.from_user.id):
        return

    phone = message.text.strip().replace(" ", "")

    if not re.fullmatch(r"\+\d{8,15}", phone):
        await message.answer(
            "Неверный формат номера.\n"
            "Пример: +79991234567"
        )
        return

    try:
        sent = await telegram_client.send_code_request(phone)

        await state.update_data(
            phone=phone,
            phone_code_hash=sent.phone_code_hash,
        )
        await state.set_state(LoginStates.waiting_code)

        await message.answer(
            "Код отправлен в Telegram.\n\n"
            "Отправьте код одним сообщением.\n"
            "Можно написать цифры через пробел, например: 1 2 3 4 5"
        )

    except PhoneNumberInvalidError:
        await message.answer("Telegram считает этот номер неверным.")
    except FloodWaitError as error:
        await message.answer(
            f"Слишком много попыток. Повторите через "
            f"{error.seconds} секунд."
        )
    except Exception as error:
        logger.exception("Ошибка отправки кода")
        await message.answer(
            f"Ошибка: {type(error).__name__}: {error}"
        )


@router.message(LoginStates.waiting_code, F.text)
async def login_code(message: Message, state: FSMContext):
    if not message.from_user or not is_allowed(message.from_user.id):
        return

    code = re.sub(r"\D", "", message.text)
    data = await state.get_data()

    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")

    if not phone or not phone_code_hash:
        await state.clear()
        await message.answer(
            "Данные авторизации потеряны. Начните заново: /login"
        )
        return

    try:
        await telegram_client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=phone_code_hash,
        )

        await state.clear()
        await message.answer(
            "Готово. Пользовательский Telegram-аккаунт подключён."
        )

    except SessionPasswordNeededError:
        await state.set_state(LoginStates.waiting_password)
        await message.answer(
            "На аккаунте включена двухэтапная аутентификация.\n\n"
            "Отправьте пароль 2FA."
        )

    except PhoneCodeInvalidError:
        await message.answer(
            "Неверный код. Попробуйте ещё раз."
        )

    except PhoneCodeExpiredError:
        await state.clear()
        await message.answer(
            "Срок действия кода истёк.\n"
            "Начните заново: /login"
        )

    except Exception as error:
        logger.exception("Ошибка входа по коду")
        await message.answer(
            f"Ошибка: {type(error).__name__}: {error}"
        )


@router.message(LoginStates.waiting_password, F.text)
async def login_password(message: Message, state: FSMContext):
    if not message.from_user or not is_allowed(message.from_user.id):
        return

    password = message.text

    try:
        await telegram_client.sign_in(password=password)

        await state.clear()
        await message.answer(
            "Готово. Пользовательский Telegram-аккаунт подключён."
        )

        try:
            await message.delete()
        except Exception:
            pass

    except Exception as error:
        logger.exception("Ошибка пароля 2FA")
        await message.answer(
            "Не удалось войти.\n"
            f"Ошибка: {type(error).__name__}: {error}"
        )


@router.message(Command("logout"))
async def logout_command(message: Message, state: FSMContext):
    if not message.from_user or not is_allowed(message.from_user.id):
        return

    await state.clear()

    if not await telegram_client.is_user_authorized():
        await message.answer("Аккаунт уже отключён.")
        return

    await telegram_client.log_out()
    await message.answer(
        "Пользовательский аккаунт отключён.\n"
        "Для повторного входа используйте /login."
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext):
    if not message.from_user or not is_allowed(message.from_user.id):
        return

    await state.clear()
    await message.answer("Текущая операция отменена.")


@router.message(Command("export"))
async def export_command(message: Message):
    if not message.from_user:
        return

    if not is_allowed(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    if not await telegram_client.is_user_authorized():
        await message.answer(
            "Сначала подключите пользовательский аккаунт: /login"
        )
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
            paths = await export_chat(target, progress)

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
    await telegram_client.connect()

    bot = Bot(BOT_TOKEN)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)

    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await telegram_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())