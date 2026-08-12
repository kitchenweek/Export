# ================= НАСТРОЙКИ =================
API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"
BOT_TOKEN = "8961878352:AAGcRX9m6VHWTjdzf9R0NZmfi5f8uCIMVGQ"
ADMIN_ID = 0  # Впишите сюда свой Telegram ID. 0 = доступ разрешён первому пользователю.
# =============================================

import asyncio
import logging
import os
import re
from collections import defaultdict
from datetime import timezone
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.tl.custom.dialog import Dialog

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_DIR = BASE_DIR / "sessions"
EXPORTS_DIR = BASE_DIR / "exports"
SESSIONS_DIR.mkdir(exist_ok=True)
EXPORTS_DIR.mkdir(exist_ok=True)

MAX_TXT_SIZE = 45 * 1024 * 1024
DIALOGS_PER_PAGE = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("month_export_bot")

router = Router()
clients: dict[int, TelegramClient] = {}
dialog_cache: dict[int, dict[int, Any]] = defaultdict(dict)
dialog_pages: dict[int, list[Dialog]] = defaultdict(list)


class LoginState(StatesGroup):
    phone = State()
    code = State()
    password = State()


class ExportState(StatesGroup):
    target = State()


OWNER_FILE = BASE_DIR / "owner_id.txt"


def is_admin(user_id: int | None) -> bool:
    if not user_id:
        return False
    if ADMIN_ID:
        return user_id == ADMIN_ID
    if OWNER_FILE.exists():
        try:
            return user_id == int(OWNER_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return False
    try:
        OWNER_FILE.write_text(str(user_id), encoding="utf-8")
        return True
    except OSError:
        return False


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔐 Войти в аккаунт"), KeyboardButton(text="👤 Статус")],
            [KeyboardButton(text="📚 Выбрать чат"), KeyboardButton(text="🔎 Чат по ссылке/ID")],
            [KeyboardButton(text="🚪 Выйти из аккаунта"), KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def session_path(user_id: int) -> str:
    return str(SESSIONS_DIR / f"user_{user_id}")


def get_client(user_id: int) -> TelegramClient:
    client = clients.get(user_id)
    if client is None:
        client = TelegramClient(session_path(user_id), API_ID, API_HASH)
        clients[user_id] = client
    return client


async def ensure_connected(client: TelegramClient) -> None:
    if not client.is_connected():
        await client.connect()


async def authorized_client(user_id: int) -> TelegramClient | None:
    client = get_client(user_id)
    await ensure_connected(client)
    if not await client.is_user_authorized():
        return None
    return client


def sanitize_filename(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", value).strip(" ._")
    return value[:80] or "chat"


def sender_label(message: Any) -> str:
    sender = getattr(message, "sender", None)
    if sender is None:
        sender_id = getattr(message, "sender_id", None)
        return f"ID {sender_id}" if sender_id else "Системное сообщение"

    first = getattr(sender, "first_name", None) or ""
    last = getattr(sender, "last_name", None) or ""
    title = getattr(sender, "title", None) or ""
    username = getattr(sender, "username", None)
    name = " ".join(part for part in (first, last) if part).strip() or title
    if username:
        return f"{name or username} (@{username})"
    sender_id = getattr(sender, "id", None)
    return name or (f"ID {sender_id}" if sender_id else "Неизвестный автор")


def media_label(message: Any) -> str | None:
    if not getattr(message, "media", None):
        return None
    if getattr(message, "photo", None):
        return "Фото"
    if getattr(message, "video", None):
        return "Видео"
    if getattr(message, "voice", None):
        return "Голосовое сообщение"
    if getattr(message, "video_note", None):
        return "Видеосообщение"
    if getattr(message, "audio", None):
        return "Аудио"
    if getattr(message, "sticker", None):
        return "Стикер"
    if getattr(message, "gif", None):
        return "GIF"
    if getattr(message, "document", None):
        file_name = getattr(getattr(message, "file", None), "name", None)
        return f"Файл: {file_name}" if file_name else "Файл"
    if getattr(message, "poll", None):
        return "Опрос"
    return "Медиа/вложение"


def format_message(message: Any) -> str:
    dt = message.date
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    timestamp = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    author = sender_label(message)
    text = message.message or ""
    media = media_label(message)

    lines = [f"[{timestamp}] {author}", f"ID: {message.id}"]
    if getattr(message, "reply_to_msg_id", None):
        lines.append(f"Ответ на сообщение: {message.reply_to_msg_id}")
    if media:
        lines.append(f"Вложение: {media}")
    lines.append(text if text else "[без текста]")
    lines.append("-" * 70)
    return "\n".join(lines) + "\n"


def split_large_file(path: Path) -> list[Path]:
    if path.stat().st_size <= MAX_TXT_SIZE:
        return [path]

    parts: list[Path] = []
    part_number = 1
    current_size = 0
    current_path = path.with_name(f"{path.stem}_part{part_number}{path.suffix}")
    current = current_path.open("w", encoding="utf-8")
    parts.append(current_path)

    try:
        with path.open("r", encoding="utf-8") as source:
            for line in source:
                encoded_size = len(line.encode("utf-8"))
                if current_size + encoded_size > MAX_TXT_SIZE and current_size > 0:
                    current.close()
                    part_number += 1
                    current_size = 0
                    current_path = path.with_name(
                        f"{path.stem}_part{part_number}{path.suffix}"
                    )
                    current = current_path.open("w", encoding="utf-8")
                    parts.append(current_path)
                current.write(line)
                current_size += encoded_size
    finally:
        current.close()

    path.unlink(missing_ok=True)
    return parts


async def export_chat_by_month(
    bot_message: Message,
    client: TelegramClient,
    entity: Any,
) -> None:
    chat = await client.get_entity(entity)
    title = (
        getattr(chat, "title", None)
        or " ".join(
            x for x in (getattr(chat, "first_name", ""), getattr(chat, "last_name", "")) if x
        ).strip()
        or getattr(chat, "username", None)
        or str(getattr(chat, "id", "chat"))
    )
    safe_title = sanitize_filename(title)
    job_dir = EXPORTS_DIR / f"{bot_message.from_user.id}_{getattr(chat, 'id', 'chat')}"
    job_dir.mkdir(parents=True, exist_ok=True)

    for old_file in job_dir.glob("*.txt"):
        old_file.unlink(missing_ok=True)

    await bot_message.answer(
        f"⏳ Начинаю экспорт чата «{title}». Сообщения будут распределены по месяцам."
    )

    open_files: dict[str, Any] = {}
    paths: dict[str, Path] = {}
    counts: dict[str, int] = defaultdict(int)
    total = 0

    try:
        async for item in client.iter_messages(chat, reverse=True):
            if not item.date:
                continue
            month_key = item.date.strftime("%Y-%m")
            if month_key not in open_files:
                path = job_dir / f"{month_key}_{safe_title}.txt"
                file_obj = path.open("w", encoding="utf-8")
                file_obj.write(f"Чат: {title}\n")
                file_obj.write(f"Месяц: {month_key}\n")
                file_obj.write("Часовой пояс дат: UTC\n")
                file_obj.write("=" * 70 + "\n\n")
                open_files[month_key] = file_obj
                paths[month_key] = path

            open_files[month_key].write(format_message(item))
            counts[month_key] += 1
            total += 1

            if total % 5000 == 0:
                await bot_message.answer(f"Обработано сообщений: {total}")

    except FloodWaitError as exc:
        await bot_message.answer(
            f"Telegram временно ограничил запросы. Нужно повторить позже. Ожидание: {exc.seconds} сек."
        )
        return
    finally:
        for file_obj in open_files.values():
            file_obj.close()

    if not paths:
        await bot_message.answer("В этом чате не найдено сообщений.")
        return

    sent_files = 0
    try:
        for month_key in sorted(paths):
            parts = split_large_file(paths[month_key])
            for part in parts:
                await bot_message.answer_document(
                    FSInputFile(part),
                    caption=f"📅 {month_key} — {counts[month_key]} сообщений",
                )
                sent_files += 1
                await asyncio.sleep(0.4)
    finally:
        for path in job_dir.glob("*.txt"):
            path.unlink(missing_ok=True)
        try:
            job_dir.rmdir()
        except OSError:
            pass

    await bot_message.answer(
        f"✅ Экспорт завершён. Сообщений: {total}. Отправлено TXT-файлов: {sent_files}.",
        reply_markup=main_menu(),
    )


def dialogs_keyboard(dialogs: list[Dialog], page: int) -> InlineKeyboardMarkup:
    start = page * DIALOGS_PER_PAGE
    end = start + DIALOGS_PER_PAGE
    buttons: list[list[InlineKeyboardButton]] = []

    for index, dialog in enumerate(dialogs[start:end], start=start):
        icon = "📢" if dialog.is_channel else "👥" if dialog.is_group else "👤"
        name = dialog.name or str(dialog.id)
        buttons.append(
            [InlineKeyboardButton(text=f"{icon} {name[:45]}", callback_data=f"dialog:{index}")]
        )

    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️", callback_data=f"page:{page-1}"))
    if end < len(dialogs):
        navigation.append(InlineKeyboardButton(text="➡️", callback_data=f"page:{page+1}"))
    if navigation:
        buttons.append(navigation)

    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="close")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Доступ к боту закрыт.")
        return
    await message.answer(
        "Бот экспортирует доступные вашему аккаунту сообщения и отправляет отдельный TXT-файл за каждый месяц.",
        reply_markup=main_menu(),
    )


@router.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu())


@router.message(F.text == "🔐 Войти в аккаунт")
async def login_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    client = await authorized_client(message.from_user.id)
    if client:
        me = await client.get_me()
        await message.answer(
            f"Аккаунт уже подключён: {getattr(me, 'first_name', '')} (ID {me.id})."
        )
        return
    await state.set_state(LoginState.phone)
    await message.answer("Отправьте номер телефона в международном формате, например: +79991234567")


@router.message(LoginState.phone)
async def phone_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    phone = (message.text or "").replace(" ", "")
    if not re.fullmatch(r"\+\d{7,15}", phone):
        await message.answer("Неверный формат. Отправьте номер, начиная с + и кода страны.")
        return

    client = get_client(message.from_user.id)
    await ensure_connected(client)
    try:
        sent = await client.send_code_request(phone)
    except PhoneNumberInvalidError:
        await message.answer("Telegram не принял этот номер. Проверьте его и отправьте заново.")
        return

    await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
    await state.set_state(LoginState.code)
    await message.answer(
        "Код входа отправлен Telegram. Пришлите его сюда. Можно писать цифры через пробел, например: 1 2 3 4 5."
    )


@router.message(LoginState.code)
async def code_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    code = re.sub(r"\D", "", message.text or "")
    data = await state.get_data()
    client = get_client(message.from_user.id)

    try:
        await client.sign_in(
            phone=data["phone"],
            code=code,
            phone_code_hash=data["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        await state.set_state(LoginState.password)
        await message.answer("На аккаунте включена двухэтапная аутентификация. Отправьте облачный пароль.")
        return
    except PhoneCodeInvalidError:
        await message.answer("Неверный код. Отправьте код ещё раз.")
        return
    except PhoneCodeExpiredError:
        await state.set_state(LoginState.phone)
        await message.answer("Срок действия кода истёк. Снова отправьте номер телефона.")
        return

    await state.clear()
    me = await client.get_me()
    await message.answer(
        f"✅ Аккаунт подключён: {getattr(me, 'first_name', '')} (ID {me.id}).",
        reply_markup=main_menu(),
    )


@router.message(LoginState.password)
async def password_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    client = get_client(message.from_user.id)
    try:
        await client.sign_in(password=message.text or "")
    except PasswordHashInvalidError:
        await message.answer("Неверный облачный пароль. Попробуйте ещё раз.")
        return

    await state.clear()
    me = await client.get_me()
    await message.answer(
        f"✅ Аккаунт подключён: {getattr(me, 'first_name', '')} (ID {me.id}).",
        reply_markup=main_menu(),
    )


@router.message(F.text == "👤 Статус")
async def status_handler(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    client = await authorized_client(message.from_user.id)
    if not client:
        await message.answer("Аккаунт не подключён.")
        return
    me = await client.get_me()
    username = f"@{me.username}" if getattr(me, "username", None) else "без username"
    await message.answer(
        f"✅ Подключён аккаунт: {getattr(me, 'first_name', '')}\n"
        f"Username: {username}\nID: {me.id}"
    )


@router.message(F.text == "🚪 Выйти из аккаунта")
async def logout_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await state.clear()
    client = get_client(message.from_user.id)
    await ensure_connected(client)
    if await client.is_user_authorized():
        await client.log_out()
    await client.disconnect()
    clients.pop(message.from_user.id, None)
    dialog_pages.pop(message.from_user.id, None)
    dialog_cache.pop(message.from_user.id, None)

    for suffix in (".session", ".session-journal"):
        Path(session_path(message.from_user.id) + suffix).unlink(missing_ok=True)

    await message.answer("Аккаунт отключён, локальная сессия удалена.", reply_markup=main_menu())


@router.message(F.text == "📚 Выбрать чат")
async def choose_chat_handler(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    client = await authorized_client(message.from_user.id)
    if not client:
        await message.answer("Сначала подключите аккаунт.")
        return

    loading = await message.answer("Загружаю список диалогов…")
    dialogs = await client.get_dialogs()
    dialog_pages[message.from_user.id] = list(dialogs)
    dialog_cache[message.from_user.id] = {
        index: dialog.entity for index, dialog in enumerate(dialogs)
    }
    await loading.edit_text(
        f"Выберите чат. Всего найдено: {len(dialogs)}",
        reply_markup=dialogs_keyboard(list(dialogs), 0),
    )


@router.callback_query(F.data.startswith("page:"))
async def page_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    page = int(callback.data.split(":", 1)[1])
    dialogs = dialog_pages.get(callback.from_user.id, [])
    if not dialogs:
        await callback.answer("Список устарел. Откройте его заново.", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=dialogs_keyboard(dialogs, page))
    await callback.answer()


@router.callback_query(F.data.startswith("dialog:"))
async def dialog_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    index = int(callback.data.split(":", 1)[1])
    entity = dialog_cache.get(callback.from_user.id, {}).get(index)
    if entity is None:
        await callback.answer("Список устарел. Откройте его заново.", show_alert=True)
        return
    await callback.answer("Экспорт начат")
    await callback.message.edit_reply_markup(reply_markup=None)
    client = await authorized_client(callback.from_user.id)
    if client:
        await export_chat_by_month(callback.message, client, entity)


@router.callback_query(F.data == "close")
async def close_callback(callback: CallbackQuery) -> None:
    if is_admin(callback.from_user.id):
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer()


@router.message(F.text == "🔎 Чат по ссылке/ID")
async def target_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    client = await authorized_client(message.from_user.id)
    if not client:
        await message.answer("Сначала подключите аккаунт.")
        return
    await state.set_state(ExportState.target)
    await message.answer(
        "Отправьте @username, публичную ссылку t.me/..., ссылку-приглашение или числовой ID чата.\n"
        "Аккаунт должен иметь доступ к этому чату."
    )


@router.message(ExportState.target)
async def export_target_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    target = (message.text or "").strip()
    if re.fullmatch(r"-?\d+", target):
        target_value: Any = int(target)
    else:
        target_value = target

    client = await authorized_client(message.from_user.id)
    if not client:
        await state.clear()
        await message.answer("Сессия не авторизована. Войдите заново.")
        return

    try:
        entity = await client.get_entity(target_value)
    except Exception as exc:
        logger.exception("Unable to resolve target")
        await message.answer(
            f"Не удалось открыть чат: {type(exc).__name__}. Проверьте ссылку/ID и доступ аккаунта."
        )
        return

    await state.clear()
    await export_chat_by_month(message, client, entity)


async def shutdown() -> None:
    for client in list(clients.values()):
        if client.is_connected():
            await client.disconnect()


async def main() -> None:
    if not BOT_TOKEN or not API_ID or not API_HASH:
        raise RuntimeError("Заполните BOT_TOKEN, API_ID и API_HASH в начале main.py")

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())