import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from telethon import TelegramClient, errors, types
from telethon.sessions import StringSession


# ============================================================
# НАСТРОЙКИ
# ============================================================

API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"
BOT_TOKEN = "8961878352:AAGcRX9m6VHWTjdzf9R0NZmfi5f8uCIMVGQ"

# Укажите свой Telegram ID
ADMIN_ID = 8489562559

BASE_DIR = Path(__file__).resolve().parent
SESSION_FILE = BASE_DIR / "telethon_session.txt"

MAX_POSTS = 100
CHANNELS_PER_PAGE = 8
CHANNEL_TIMEZONE = ZoneInfo("Europe/Moscow")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)

router = Router()
storage = MemoryStorage()

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)

dp = Dispatcher(storage=storage)
dp.include_router(router)

telethon_client: TelegramClient | None = None
telethon_lock = asyncio.Lock()


# ============================================================
# СОСТОЯНИЯ
# ============================================================

class AuthorizationStates(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class ReplacementStates(StatesGroup):
    choosing_channel = State()
    waiting_dates = State()
    collecting_posts = State()
    confirmation = State()
    processing = State()


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔐 Авторизация")],
            [KeyboardButton(text="📝 Заменить посты")],
            [KeyboardButton(text="👤 Статус аккаунта")],
            [KeyboardButton(text="🚪 Выйти из аккаунта")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие",
    )


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить номер", request_contact=True)],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def collection_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Готово")],
            [KeyboardButton(text="🗑 Очистить посты")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True,
    )


def confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Начать замену",
                    callback_data="replace:confirm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="replace:cancel",
                )
            ],
        ]
    )


# ============================================================
# ПРОВЕРКА ДОСТУПА
# ============================================================

def is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_ID


async def reject_non_admin_message(message: Message) -> bool:
    if is_admin(message.from_user.id if message.from_user else None):
        return False

    await message.answer("⛔ У вас нет доступа к этому боту.")
    return True


async def reject_non_admin_callback(callback: CallbackQuery) -> bool:
    if is_admin(callback.from_user.id):
        return False

    await callback.answer("У вас нет доступа.", show_alert=True)
    return True


# ============================================================
# TELETHON
# ============================================================

def load_string_session() -> str:
    if not SESSION_FILE.exists():
        return ""

    try:
        return SESSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        logger.exception("Не удалось прочитать файл сессии")
        return ""


def save_string_session(session_string: str) -> None:
    SESSION_FILE.write_text(session_string, encoding="utf-8")


def delete_session_file() -> None:
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        logger.exception("Не удалось удалить файл сессии")


async def create_telethon_client() -> TelegramClient:
    session_string = load_string_session()

    client = TelegramClient(
        StringSession(session_string),
        API_ID,
        API_HASH,
        device_model="Channel Post Editor",
        system_version="Python",
        app_version="1.0",
        lang_code="ru",
        system_lang_code="ru-RU",
    )

    await client.connect()
    return client


async def get_telethon_client() -> TelegramClient:
    global telethon_client

    async with telethon_lock:
        if telethon_client is None:
            telethon_client = await create_telethon_client()
        elif not telethon_client.is_connected():
            await telethon_client.connect()

        return telethon_client


async def recreate_telethon_client() -> TelegramClient:
    global telethon_client

    async with telethon_lock:
        if telethon_client is not None:
            try:
                await telethon_client.disconnect()
            except Exception:
                logger.exception("Ошибка отключения Telethon")

        telethon_client = await create_telethon_client()
        return telethon_client


async def save_current_telethon_session(client: TelegramClient) -> None:
    session_string = client.session.save()

    if not isinstance(session_string, str) or not session_string:
        raise RuntimeError("Не удалось получить строку сессии Telethon")

    save_string_session(session_string)


# ============================================================
# ДАТЫ
# ============================================================

DATE_PATTERN = re.compile(r"(?<!\d)(\d{2}\.\d{2}\.\d{4})(?!\d)")


def parse_dates(text: str) -> list[datetime]:
    found_dates: set[datetime] = set()
    invalid_dates: list[str] = []

    for value in DATE_PATTERN.findall(text):
        try:
            parsed = datetime.strptime(value, "%d.%m.%Y")
            found_dates.add(parsed)
        except ValueError:
            invalid_dates.append(value)

    if invalid_dates:
        raise ValueError(
            "Некорректные даты: " + ", ".join(sorted(set(invalid_dates)))
        )

    if not found_dates:
        raise ValueError(
            "Даты не найдены. Используйте формат ДД.ММ.ГГГГ."
        )

    return sorted(found_dates)


def format_dates(dates: list[str] | list[datetime]) -> str:
    result: list[str] = []

    for item in dates:
        if isinstance(item, datetime):
            result.append(item.strftime("%d.%m.%Y"))
        else:
            result.append(item)

    return "\n".join(result)


# ============================================================
# РАЗДЕЛЕНИЕ ПОСТОВ
# ============================================================

POST_SEPARATOR_PATTERN = re.compile(
    r'^[ \t]*-/[ \t]*\(?[ \t]*[«»"\'“”‘’]*[ \t]*\d+'
    r'[ \t]*[«»"\'“”‘’]*[ \t]*\)?[ \t]*(?:\r?\n|$)',
    re.MULTILINE,
)


def split_posts(text: str) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not normalized:
        return []

    matches = list(POST_SEPARATOR_PATTERN.finditer(normalized))

    if not matches:
        return [normalized]

    posts: list[str] = []

    prefix = normalized[:matches[0].start()].strip()
    if prefix:
        posts.append(prefix)

    for index, match in enumerate(matches):
        start = match.end()
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(normalized)
        )

        post = normalized[start:end].strip()

        if post:
            posts.append(post)

    return posts


# ============================================================
# КАНАЛЫ
# ============================================================

async def can_edit_channel(
    client: TelegramClient,
    entity: types.Channel,
) -> bool:
    if getattr(entity, "creator", False):
        return True

    admin_rights = getattr(entity, "admin_rights", None)

    if admin_rights is None:
        return False

    return bool(
        getattr(admin_rights, "edit_messages", False)
        or getattr(admin_rights, "post_messages", False)
    )


async def get_editable_channels() -> list[dict[str, Any]]:
    client = await get_telethon_client()

    if not await client.is_user_authorized():
        raise RuntimeError("Аккаунт Telethon не авторизован")

    channels: list[dict[str, Any]] = []

    async for dialog in client.iter_dialogs():
        entity = dialog.entity

        if not isinstance(entity, types.Channel):
            continue

        if not getattr(entity, "broadcast", False):
            continue

        if not await can_edit_channel(client, entity):
            continue

        channels.append(
            {
                "id": entity.id,
                "title": dialog.name or "Без названия",
                "username": getattr(entity, "username", None),
            }
        )

    channels.sort(key=lambda item: item["title"].casefold())
    return channels


def channels_keyboard(
    channels: list[dict[str, Any]],
    page: int,
) -> InlineKeyboardMarkup:
    total_pages = max(
        1,
        (len(channels) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE,
    )

    page = max(0, min(page, total_pages - 1))

    start = page * CHANNELS_PER_PAGE
    end = start + CHANNELS_PER_PAGE
    page_channels = channels[start:end]

    builder = InlineKeyboardBuilder()

    for channel in page_channels:
        title = channel["title"]
        if len(title) > 40:
            title = title[:37] + "..."

        builder.button(
            text=f"📢 {title}",
            callback_data=f"channel:{channel['id']}",
        )

    builder.adjust(1)

    navigation: list[InlineKeyboardButton] = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"channels_page:{page - 1}",
            )
        )

    navigation.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{total_pages}",
            callback_data="channels_page:noop",
        )
    )

    if page < total_pages - 1:
        navigation.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"channels_page:{page + 1}",
            )
        )

    builder.row(*navigation)

    builder.row(
        InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="replace:cancel",
        )
    )

    return builder.as_markup()


# ============================================================
# ПОЛУЧЕНИЕ СООБЩЕНИЙ
# ============================================================

async def resolve_channel(channel_id: int) -> types.Channel:
    client = await get_telethon_client()

    entity = await client.get_entity(
        types.PeerChannel(channel_id)
    )

    if not isinstance(entity, types.Channel):
        raise RuntimeError("Выбранный объект не является каналом")

    return entity


async def get_messages_for_dates(
    channel_id: int,
    dates: list[str],
) -> list[types.Message]:
    """
    Возвращает существующие сообщения канала за выбранные даты.
    Даты сравниваются в часовом поясе Europe/Moscow.
    Новые сообщения эта функция не создаёт.
    """
    client = await get_telethon_client()
    channel = await resolve_channel(channel_id)

    target_dates = {
        datetime.strptime(value, "%d.%m.%Y").date()
        for value in dates
    }

    first_date = min(target_dates)
    last_date = max(target_dates)

    # Верхняя граница поиска: начало следующего дня по Москве,
    # преобразованное в UTC для Telethon.
    local_end = datetime.combine(
        last_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=CHANNEL_TIMEZONE,
    )
    offset_date = local_end.astimezone(timezone.utc)

    found: list[types.Message] = []

    async for message in client.iter_messages(
        channel,
        offset_date=offset_date,
    ):
        if message.date is None:
            continue

        local_message_date = message.date.astimezone(
            CHANNEL_TIMEZONE
        ).date()

        if local_message_date < first_date:
            break

        if local_message_date not in target_dates:
            continue

        if getattr(message, "action", None) is not None:
            continue

        # Берём только реальные публикации. У медиапоста будет
        # заменена подпись, само медиа останется прежним.
        if message.message is None and message.media is None:
            continue

        found.append(message)

    found.sort(
        key=lambda item: (
            item.date,
            item.id,
        )
    )

    return found


# ============================================================
# РЕДАКТИРОВАНИЕ
# ============================================================

async def edit_channel_message(
    channel: types.Channel,
    old_message: types.Message,
    new_text: str,
) -> None:
    client = await get_telethon_client()

    await client.edit_message(
        entity=channel,
        message=old_message.id,
        text=new_text,
        parse_mode=None,
        link_preview=False,
    )


async def perform_replacement(
    channel_id: int,
    dates: list[str],
    new_posts: list[str],
) -> tuple[int, list[str]]:
    """
    Только редактирует уже существующие публикации канала.
    Отправка новых сообщений в канал отсутствует.
    """
    channel = await resolve_channel(channel_id)
    old_messages = await get_messages_for_dates(channel_id, dates)

    if len(old_messages) < len(new_posts):
        raise RuntimeError(
            f"Найдено только {len(old_messages)} сообщений, "
            f"а новых текстов передано {len(new_posts)}."
        )

    selected_messages = old_messages[:len(new_posts)]
    edited_count = 0
    errors_list: list[str] = []

    for index, (old_message, new_text) in enumerate(
        zip(selected_messages, new_posts),
        start=1,
    ):
        try:
            await edit_channel_message(
                channel=channel,
                old_message=old_message,
                new_text=new_text,
            )
            edited_count += 1

        except errors.FloodWaitError as error:
            await asyncio.sleep(int(error.seconds) + 1)

            try:
                await edit_channel_message(
                    channel=channel,
                    old_message=old_message,
                    new_text=new_text,
                )
                edited_count += 1
            except Exception as retry_error:
                logger.exception(
                    "Ошибка повторного редактирования сообщения %s",
                    old_message.id,
                )
                errors_list.append(
                    f"Пост {index}, ID {old_message.id}: "
                    f"{type(retry_error).__name__}: {retry_error}"
                )

        except errors.MessageNotModifiedError:
            # Текст уже совпадает с новым.
            edited_count += 1

        except errors.MessageEditTimeExpiredError:
            errors_list.append(
                f"Пост {index}, ID {old_message.id}: "
                "истёк допустимый срок редактирования"
            )

        except errors.ChatAdminRequiredError:
            errors_list.append(
                f"Пост {index}, ID {old_message.id}: "
                "недостаточно прав администратора"
            )

        except errors.MessageAuthorRequiredError:
            errors_list.append(
                f"Пост {index}, ID {old_message.id}: "
                "аккаунт не может редактировать этот пост"
            )

        except Exception as error:
            logger.exception(
                "Ошибка редактирования сообщения %s",
                old_message.id,
            )
            errors_list.append(
                f"Пост {index}, ID {old_message.id}: "
                f"{type(error).__name__}: {error}"
            )

        # Небольшая пауза снижает вероятность FloodWait.
        await asyncio.sleep(0.7)

    return edited_count, errors_list


def post_preview(text: str, max_length: int = 170) -> str:
    one_line = " ".join(text.split())

    if len(one_line) > max_length:
        one_line = one_line[:max_length - 3] + "..."

    return html.escape(one_line)


# ============================================================
# КОМАНДЫ
# ============================================================

@router.message(CommandStart())
async def command_start(message: Message, state: FSMContext) -> None:
    if await reject_non_admin_message(message):
        return

    await state.clear()

    await message.answer(
        "👋 <b>Редактор постов канала</b>\n\n"
        "Авторизуйте аккаунт через Telethon, выберите канал, "
        "укажите даты и отправьте новые посты.",
        reply_markup=main_keyboard(),
    )


@router.message(Command("cancel"))
@router.message(F.text == "❌ Отмена")
async def cancel_handler(message: Message, state: FSMContext) -> None:
    if await reject_non_admin_message(message):
        return

    await state.clear()

    await message.answer(
        "❌ Действие отменено.",
        reply_markup=main_keyboard(),
    )


@router.message(F.text == "👤 Статус аккаунта")
async def account_status(message: Message) -> None:
    if await reject_non_admin_message(message):
        return

    try:
        client = await get_telethon_client()

        if not await client.is_user_authorized():
            await message.answer(
                "❌ Пользовательский аккаунт не авторизован.",
                reply_markup=main_keyboard(),
            )
            return

        me = await client.get_me()

        username = f"@{me.username}" if me.username else "не установлен"
        phone = f"+{me.phone}" if me.phone else "скрыт"

        await message.answer(
            "✅ <b>Аккаунт авторизован</b>\n\n"
            f"Имя: <b>{html.escape(me.first_name or '')}</b>\n"
            f"Username: <b>{html.escape(username)}</b>\n"
            f"Телефон: <code>{html.escape(phone)}</code>\n"
            f"ID: <code>{me.id}</code>",
            reply_markup=main_keyboard(),
        )

    except Exception as error:
        logger.exception("Ошибка получения статуса аккаунта")
        await message.answer(
            f"❌ Ошибка проверки аккаунта:\n"
            f"<code>{html.escape(str(error))}</code>"
        )


# ============================================================
# АВТОРИЗАЦИЯ
# ============================================================

@router.message(F.text == "🔐 Авторизация")
async def authorization_start(
    message: Message,
    state: FSMContext,
) -> None:
    if await reject_non_admin_message(message):
        return

    client = await get_telethon_client()

    if await client.is_user_authorized():
        me = await client.get_me()

        await message.answer(
            "✅ Аккаунт уже авторизован.\n\n"
            f"ID: <code>{me.id}</code>",
            reply_markup=main_keyboard(),
        )
        return

    await state.clear()
    await state.set_state(AuthorizationStates.waiting_phone)

    await message.answer(
        "📱 Отправьте номер телефона аккаунта.\n\n"
        "Формат: <code>+79991234567</code>",
        reply_markup=phone_keyboard(),
    )


@router.message(AuthorizationStates.waiting_phone)
async def authorization_phone(
    message: Message,
    state: FSMContext,
) -> None:
    if await reject_non_admin_message(message):
        return

    phone: str | None = None

    if message.contact:
        if message.contact.user_id != message.from_user.id:
            await message.answer("❌ Отправьте собственный контакт.")
            return

        phone = message.contact.phone_number
    elif message.text:
        phone = message.text.strip()

    if not phone:
        await message.answer("❌ Не удалось получить номер телефона.")
        return

    phone = re.sub(r"[^\d+]", "", phone)

    if not phone.startswith("+"):
        phone = "+" + phone

    if not re.fullmatch(r"\+\d{7,15}", phone):
        await message.answer(
            "❌ Неверный формат.\n"
            "Пример: <code>+79991234567</code>"
        )
        return

    try:
        client = await recreate_telethon_client()
        sent_code = await client.send_code_request(phone)

        await state.update_data(
            phone=phone,
            phone_code_hash=sent_code.phone_code_hash,
        )
        await state.set_state(AuthorizationStates.waiting_code)

        await message.answer(
            "✉️ Telegram отправил код входа.\n\n"
            "Отправьте код цифрами.",
            reply_markup=ReplyKeyboardRemove(),
        )

    except errors.PhoneNumberInvalidError:
        await message.answer("❌ Telegram считает номер некорректным.")

    except errors.PhoneNumberBannedError:
        await message.answer("❌ Этот номер заблокирован Telegram.")

    except errors.FloodWaitError as error:
        await message.answer(
            f"⏳ Слишком много попыток. Повторите через "
            f"<b>{error.seconds} сек.</b>"
        )

    except Exception as error:
        logger.exception("Ошибка отправки кода")
        await message.answer(
            "❌ Не удалось отправить код:\n"
            f"<code>{html.escape(str(error))}</code>"
        )


@router.message(AuthorizationStates.waiting_code)
async def authorization_code(
    message: Message,
    state: FSMContext,
) -> None:
    if await reject_non_admin_message(message):
        return

    if not message.text:
        await message.answer("❌ Отправьте код текстом.")
        return

    code = re.sub(r"\D", "", message.text)

    if not code:
        await message.answer("❌ Код должен содержать цифры.")
        return

    data = await state.get_data()
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")

    if not phone or not phone_code_hash:
        await state.clear()
        await message.answer(
            "❌ Данные авторизации потеряны. Начните заново.",
            reply_markup=main_keyboard(),
        )
        return

    try:
        client = await get_telethon_client()

        await client.sign_in(
            phone=phone,
            code=code,
            phone_code_hash=phone_code_hash,
        )

        await save_current_telethon_session(client)
        await state.clear()

        me = await client.get_me()

        await message.answer(
            "✅ <b>Авторизация выполнена</b>\n\n"
            f"Аккаунт: <b>{html.escape(me.first_name or '')}</b>\n"
            f"ID: <code>{me.id}</code>",
            reply_markup=main_keyboard(),
        )

    except errors.SessionPasswordNeededError:
        await state.set_state(AuthorizationStates.waiting_password)

        await message.answer(
            "🔐 На аккаунте включён облачный пароль.\n\n"
            "Отправьте пароль двухэтапной аутентификации."
        )

    except errors.PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуйте ещё раз.")

    except errors.PhoneCodeExpiredError:
        await state.clear()
        await message.answer(
            "❌ Код истёк. Начните авторизацию заново.",
            reply_markup=main_keyboard(),
        )

    except Exception as error:
        logger.exception("Ошибка входа по коду")
        await message.answer(
            "❌ Ошибка авторизации:\n"
            f"<code>{html.escape(str(error))}</code>"
        )


@router.message(AuthorizationStates.waiting_password)
async def authorization_password(
    message: Message,
    state: FSMContext,
) -> None:
    if await reject_non_admin_message(message):
        return

    if not message.text:
        await message.answer("❌ Отправьте пароль текстом.")
        return

    try:
        client = await get_telethon_client()
        await client.sign_in(password=message.text)

        await save_current_telethon_session(client)
        await state.clear()

        me = await client.get_me()

        try:
            await message.delete()
        except TelegramBadRequest:
            pass

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "✅ <b>Авторизация выполнена</b>\n\n"
                f"Аккаунт: <b>{html.escape(me.first_name or '')}</b>\n"
                f"ID: <code>{me.id}</code>"
            ),
            reply_markup=main_keyboard(),
        )

    except errors.PasswordHashInvalidError:
        await message.answer("❌ Неверный облачный пароль.")

    except Exception as error:
        logger.exception("Ошибка входа по паролю")
        await message.answer(
            "❌ Ошибка авторизации:\n"
            f"<code>{html.escape(str(error))}</code>"
        )


@router.message(F.text == "🚪 Выйти из аккаунта")
async def logout_handler(
    message: Message,
    state: FSMContext,
) -> None:
    global telethon_client

    if await reject_non_admin_message(message):
        return

    await state.clear()

    try:
        client = await get_telethon_client()

        if await client.is_user_authorized():
            await client.log_out()

        telethon_client = None
        delete_session_file()

        await message.answer(
            "✅ Сессия пользовательского аккаунта удалена.",
            reply_markup=main_keyboard(),
        )

    except Exception as error:
        logger.exception("Ошибка выхода из аккаунта")
        await message.answer(
            "❌ Ошибка выхода:\n"
            f"<code>{html.escape(str(error))}</code>"
        )


# ============================================================
# ВЫБОР КАНАЛА
# ============================================================

@router.message(F.text == "📝 Заменить посты")
async def replacement_start(
    message: Message,
    state: FSMContext,
) -> None:
    if await reject_non_admin_message(message):
        return

    client = await get_telethon_client()

    if not await client.is_user_authorized():
        await message.answer(
            "❌ Сначала авторизуйте пользовательский аккаунт.",
            reply_markup=main_keyboard(),
        )
        return

    loading = await message.answer("🔍 Получаю список каналов...")

    try:
        channels = await get_editable_channels()

        if not channels:
            await loading.edit_text(
                "❌ Не найдено каналов, в которых аккаунт может "
                "редактировать посты."
            )
            return

        await state.clear()
        await state.update_data(channels=channels)
        await state.set_state(ReplacementStates.choosing_channel)

        await loading.edit_text(
            "📢 <b>Выберите канал</b>",
            reply_markup=channels_keyboard(channels, page=0),
        )

    except Exception as error:
        logger.exception("Ошибка получения каналов")

        await loading.edit_text(
            "❌ Не удалось получить каналы:\n"
            f"<code>{html.escape(str(error))}</code>"
        )


@router.callback_query(
    ReplacementStates.choosing_channel,
    F.data.startswith("channels_page:"),
)
async def channels_page_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await reject_non_admin_callback(callback):
        return

    value = callback.data.split(":", maxsplit=1)[1]

    if value == "noop":
        await callback.answer()
        return

    page = int(value)
    data = await state.get_data()
    channels = data.get("channels", [])

    await callback.message.edit_reply_markup(
        reply_markup=channels_keyboard(channels, page)
    )
    await callback.answer()


@router.callback_query(
    ReplacementStates.choosing_channel,
    F.data.startswith("channel:"),
)
async def channel_selected(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await reject_non_admin_callback(callback):
        return

    channel_id = int(callback.data.split(":", maxsplit=1)[1])

    data = await state.get_data()
    channels: list[dict[str, Any]] = data.get("channels", [])

    selected = next(
        (
            channel
            for channel in channels
            if channel["id"] == channel_id
        ),
        None,
    )

    if selected is None:
        await callback.answer(
            "Канал не найден. Начните заново.",
            show_alert=True,
        )
        return

    await state.update_data(
        channel_id=channel_id,
        channel_title=selected["title"],
        channels=None,
    )
    await state.set_state(ReplacementStates.waiting_dates)

    await callback.message.edit_text(
        "✅ Выбран канал:\n"
        f"<b>{html.escape(selected['title'])}</b>"
    )

    await callback.message.answer(
        "📅 <b>Отправьте даты сообщений</b>\n\n"
        "<code>03.03.2026\n"
        "04.04.2026\n"
        "15.05.2026</code>"
    )

    await callback.answer()


# ============================================================
# ДАТЫ И ПОСТЫ
# ============================================================

@router.message(ReplacementStates.waiting_dates)
async def dates_received(
    message: Message,
    state: FSMContext,
) -> None:
    if await reject_non_admin_message(message):
        return

    if not message.text:
        await message.answer("❌ Отправьте даты текстовым сообщением.")
        return

    try:
        parsed_dates = parse_dates(message.text)
    except ValueError as error:
        await message.answer(
            f"❌ {html.escape(str(error))}\n\n"
            "Пример:\n"
            "<code>03.03.2026\n04.04.2026</code>"
        )
        return

    dates_strings = [
        date.strftime("%d.%m.%Y")
        for date in parsed_dates
    ]

    await state.update_data(
        dates=dates_strings,
        posts=[],
    )
    await state.set_state(ReplacementStates.collecting_posts)

    await message.answer(
        "✅ <b>Даты сохранены</b>\n\n"
        f"<code>{html.escape(format_dates(dates_strings))}</code>\n\n"
        "Теперь отправляйте новые посты.\n\n"
        "Разделители:\n"
        "<code>-/1\n-/2\n-/3</code>\n\n"
        "или:\n"
        "<code>-/(1)\n-/(2)</code>\n\n"
        "Номера игнорируются, учитывается только порядок.\n"
        f"Максимум: <b>{MAX_POSTS}</b>.",
        reply_markup=collection_keyboard(),
    )


@router.message(
    ReplacementStates.collecting_posts,
    F.text == "🗑 Очистить посты",
)
async def clear_collected_posts(
    message: Message,
    state: FSMContext,
) -> None:
    if await reject_non_admin_message(message):
        return

    await state.update_data(posts=[])

    await message.answer(
        "🗑 Все принятые посты удалены."
    )


@router.message(
    ReplacementStates.collecting_posts,
    F.text == "✅ Готово",
)
async def finish_collecting_posts(
    message: Message,
    state: FSMContext,
) -> None:
    if await reject_non_admin_message(message):
        return

    data = await state.get_data()

    channel_id = data.get("channel_id")
    channel_title = data.get("channel_title")
    dates: list[str] = data.get("dates", [])
    posts: list[str] = data.get("posts", [])

    if not channel_id or not dates:
        await state.clear()
        await message.answer(
            "❌ Данные операции потеряны. Начните заново.",
            reply_markup=main_keyboard(),
        )
        return

    if not posts:
        await message.answer(
            "❌ Вы ещё не отправили ни одного поста."
        )
        return

    try:
        old_messages = await get_messages_for_dates(
            channel_id=channel_id,
            dates=dates,
        )

        if len(old_messages) < len(posts):
            await message.answer(
                "❌ <b>Недостаточно сообщений для замены</b>\n\n"
                f"Новых текстов: <b>{len(posts)}</b>\n"
                f"Найдено существующих постов: "
                f"<b>{len(old_messages)}</b>\n\n"
                "Новые сообщения в канал создаваться не будут.",
                reply_markup=collection_keyboard(),
            )
            return

        preview_lines: list[str] = []

        for index, post in enumerate(posts[:5], start=1):
            preview_lines.append(
                f"<b>{index}.</b> {post_preview(post)}"
            )

        if len(posts) > 5:
            preview_lines.append(
                f"\n…и ещё <b>{len(posts) - 5}</b>"
            )

        await state.set_state(ReplacementStates.confirmation)

        await message.answer(
            "⚠️ <b>Подтвердите замену</b>\n\n"
            f"Канал: <b>{html.escape(channel_title)}</b>\n"
            f"Дат выбрано: <b>{len(dates)}</b>\n"
            f"Найдено существующих постов: "
            f"<b>{len(old_messages)}</b>\n"
            f"Новых текстов: <b>{len(posts)}</b>\n\n"
            + "\n\n".join(preview_lines)
            + "\n\nБот только изменит существующие посты. "
              "Ничего нового в канал отправлено не будет.",
            reply_markup=confirmation_keyboard(),
        )

    except Exception as error:
        logger.exception("Ошибка проверки сообщений")
        await state.clear()

        await message.answer(
            "❌ Ошибка проверки канала:\n"
            f"<code>{html.escape(str(error))}</code>",
            reply_markup=main_keyboard(),
        )


@router.message(ReplacementStates.collecting_posts)
async def collect_posts(
    message: Message,
    state: FSMContext,
) -> None:
    if await reject_non_admin_message(message):
        return

    incoming_text = message.text or message.caption

    if not incoming_text:
        await message.answer(
            "❌ Поддерживаются текстовые сообщения и подписи к медиа."
        )
        return

    new_parts = split_posts(incoming_text)

    if not new_parts:
        await message.answer(
            "❌ В сообщении не найден текст поста."
        )
        return

    data = await state.get_data()
    current_posts: list[str] = data.get("posts", [])

    available = MAX_POSTS - len(current_posts)

    if available <= 0:
        await message.answer(
            f"❌ Уже принято максимальное количество: "
            f"{MAX_POSTS} постов."
        )
        return

    accepted_parts = new_parts[:available]
    rejected_count = len(new_parts) - len(accepted_parts)

    current_posts.extend(accepted_parts)
    await state.update_data(posts=current_posts)

    response = (
        f"✅ Принято из сообщения: <b>{len(accepted_parts)}</b>\n"
        f"Всего: <b>{len(current_posts)}/{MAX_POSTS}</b>"
    )

    if rejected_count:
        response += (
            f"\n⚠️ Не принято из-за лимита: "
            f"<b>{rejected_count}</b>"
        )

    await message.answer(response)


# ============================================================
# ПОДТВЕРЖДЕНИЕ
# ============================================================

@router.callback_query(
    ReplacementStates.confirmation,
    F.data == "replace:cancel",
)
@router.callback_query(
    ReplacementStates.choosing_channel,
    F.data == "replace:cancel",
)
async def replacement_cancel_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await reject_non_admin_callback(callback):
        return

    await state.clear()

    await callback.message.answer(
        "❌ Операция отменена.",
        reply_markup=main_keyboard(),
    )

    await callback.answer()


@router.callback_query(
    ReplacementStates.confirmation,
    F.data == "replace:confirm",
)
async def replacement_confirm_callback(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    if await reject_non_admin_callback(callback):
        return

    data = await state.get_data()

    channel_id = data.get("channel_id")
    channel_title = data.get("channel_title")
    dates: list[str] = data.get("dates", [])
    posts: list[str] = data.get("posts", [])

    if not channel_id or not dates or not posts:
        await state.clear()
        await callback.answer(
            "Данные операции потеряны",
            show_alert=True,
        )
        await callback.message.answer(
            "❌ Начните операцию заново.",
            reply_markup=main_keyboard(),
        )
        return

    await state.set_state(ReplacementStates.processing)
    await callback.answer("Замена началась")

    try:
        edited_count, errors_list = await perform_replacement(
            channel_id=channel_id,
            dates=dates,
            new_posts=posts,
        )

        error_text = ""

        if errors_list:
            shown_errors = errors_list[:10]
            error_text = (
                "\n\n⚠️ <b>Ошибки:</b>\n"
                + "\n".join(
                    f"• {html.escape(value)}"
                    for value in shown_errors
                )
            )

            if len(errors_list) > 10:
                error_text += (
                    f"\n• …и ещё {len(errors_list) - 10}"
                )

        await callback.message.answer(
            "✅ <b>Замена завершена</b>\n\n"
            f"Канал: <b>{html.escape(channel_title)}</b>\n"
            f"Изменено: <b>{edited_count}/{len(posts)}</b>\n"
            f"Ошибок: <b>{len(errors_list)}</b>"
            f"{error_text}\n\n"
            "Новые сообщения в канал не отправлялись.",
            reply_markup=main_keyboard(),
        )

    except Exception as error:
        logger.exception("Критическая ошибка замены")

        await callback.message.answer(
            "❌ <b>Замена остановлена</b>\n\n"
            f"<code>{html.escape(str(error))}</code>\n\n"
            "Новые сообщения в канал не отправлялись.",
            reply_markup=main_keyboard(),
        )

    finally:
        await state.clear()


@router.message(ReplacementStates.processing)
async def processing_message_handler(message: Message) -> None:
    if await reject_non_admin_message(message):
        return

    await message.answer(
        "⏳ Сейчас выполняется замена сообщений."
    )


@router.message(StateFilter(None))
async def unknown_message(message: Message) -> None:
    if await reject_non_admin_message(message):
        return

    await message.answer(
        "Выберите действие в меню.",
        reply_markup=main_keyboard(),
    )


async def on_shutdown() -> None:
    global telethon_client

    if telethon_client is not None:
        try:
            await telethon_client.disconnect()
        except Exception:
            logger.exception("Ошибка отключения Telethon")


async def main() -> None:
    logger.info("Запуск бота")

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await dp.start_polling(bot)
    finally:
        await on_shutdown()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")