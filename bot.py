import asyncio
import hashlib
import io
import sqlite3

import qrcode
from pathlib import Path

from telethon import TelegramClient, events, Button
from telethon.errors import (
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
    PasswordHashInvalidError,
    PhoneNumberInvalidError,
)
from telethon.sessions import StringSession
from telethon.tl.types import User

# ============================================================
# НАСТРОЙКИ
# ============================================================

API_ID = 414121
API_HASH = "db09ccfc2a65e1b14a937be15bdb5d4b"
BOT_TOKEN = "8587715287:AAFfFjBjQXxesOTuPiolYu01ZF4KWwWblaY"

ADMIN_ID = 6500453044  # Telegram ID администратора. Измени здесь.

TARGET_GROUP = "ЧЕКИ МАКСИМ"
TRIGGER = "добрый день, ваш заказ прибыл к нам на склад в мск"

DB = Path("pdf_bot.db")

# ============================================================
# БАЗА
# ============================================================

def db_init():
    with sqlite3.connect(DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS sent_pdf (
                sha256 TEXT PRIMARY KEY,
                sender_id INTEGER,
                source_message_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        con.commit()


def get_setting(key, default=None):
    with sqlite3.connect(DB) as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with sqlite3.connect(DB) as con:
        con.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            (key, str(value))
        )
        con.commit()


def del_setting(key):
    with sqlite3.connect(DB) as con:
        con.execute("DELETE FROM settings WHERE key=?", (key,))
        con.commit()


def already_sent(digest):
    with sqlite3.connect(DB) as con:
        return con.execute(
            "SELECT 1 FROM sent_pdf WHERE sha256=?",
            (digest,)
        ).fetchone() is not None


def remember(digest, sender_id, msg_id):
    with sqlite3.connect(DB) as con:
        con.execute(
            """
            INSERT OR IGNORE INTO sent_pdf
            (sha256, sender_id, source_message_id)
            VALUES (?, ?, ?)
            """,
            (digest, sender_id, msg_id)
        )
        con.commit()


def sent_count():
    with sqlite3.connect(DB) as con:
        return con.execute("SELECT COUNT(*) FROM sent_pdf").fetchone()[0]


# ============================================================
# TELEGRAM
# ============================================================

bot = TelegramClient("bot_session", API_ID, API_HASH)

user_client = None
monitor_handler_installed = False
login_states = {}

OWNER_ID = ADMIN_ID


def normalize(text):
    return " ".join((text or "").lower().replace("ё", "е").split())


NORMALIZED_TRIGGER = normalize(TRIGGER)


def main_keyboard():
    connected = user_client is not None and user_client.is_connected()
    return [
        [Button.inline(
            "🟢 Аккаунт подключен" if connected else "🔐 Подключить аккаунт",
            b"status" if connected else b"connect"
        )],
        [
            Button.inline("📊 Статус", b"status"),
            Button.inline("👥 Проверить группу", b"group")
        ],
        [
            Button.inline("📄 Статистика PDF", b"stats"),
            Button.inline("🔄 Перезапустить монитор", b"restart_monitor")
        ],
        [Button.inline("🚪 Отключить аккаунт", b"disconnect")]
    ]


async def panel_text():
    connected = False
    account_text = "не подключен"

    if user_client:
        try:
            if user_client.is_connected() and await user_client.is_user_authorized():
                me = await user_client.get_me()
                connected = True
                username = f"@{me.username}" if me.username else ""
                account_text = f"{me.first_name or ''} {username}".strip()
        except Exception:
            pass

    return (
        "⚙️ Панель PDF-монитора\n\n"
        f"Аккаунт: {'🟢' if connected else '🔴'} {account_text}\n"
        f"Группа: {TARGET_GROUP}\n"
        f"Отправлено уникальных PDF: {sent_count()}\n\n"
        "PDF отслеживаются только во входящих личных сообщениях."
    )


async def show_panel(event):
    text = await panel_text()
    try:
        await event.edit(text, buttons=main_keyboard())
    except Exception:
        await event.respond(text, buttons=main_keyboard())


async def target_group():
    if not user_client:
        return None

    try:
        async for dialog in user_client.iter_dialogs():
            if dialog.name == TARGET_GROUP and (dialog.is_group or dialog.is_channel):
                return dialog.entity
    except Exception:
        return None

    return None


async def trigger_seen_before(chat_id, current_message_id):
    if not user_client:
        return False

    async for message in user_client.iter_messages(
        chat_id,
        offset_id=current_message_id,
        limit=None
    ):
        if NORMALIZED_TRIGGER in normalize(message.raw_text):
            return True

    return False


def mention_for(sender):
    if getattr(sender, "username", None):
        return "@" + sender.username

    name = " ".join(
        x for x in [
            getattr(sender, "first_name", None),
            getattr(sender, "last_name", None)
        ] if x
    )

    name = name or str(sender.id)
    name = name.replace("[", "").replace("]", "")
    return f"[{name}](tg://user?id={sender.id})"


async def incoming_pdf(event):
    if not event.is_private or not event.file:
        return

    mime = (event.file.mime_type or "").lower()
    name = (event.file.name or "").lower()

    if mime != "application/pdf" and not name.endswith(".pdf"):
        return

    sender = await event.get_sender()

    if not isinstance(sender, User) or sender.bot:
        return

    try:
        data = await event.download_media(bytes)
        if not data:
            return

        digest = hashlib.sha256(data).hexdigest()

        if already_sent(digest):
            print(f"Дубликат PDF пропущен: {digest}")
            return

        group = await target_group()

        if group is None:
            if OWNER_ID:
                await bot.send_message(
                    OWNER_ID,
                    f"⚠️ Получен PDF, но группа «{TARGET_GROUP}» не найдена."
                )
            return

        first_dep = not await trigger_seen_before(event.chat_id, event.id)

        tag = mention_for(sender)

        caption = f"📄 PDF от {tag}"

        if first_dep:
            caption += f"\n\n⚠️ Первый деп — {tag}"

        kwargs = {
            "caption": caption,
            "parse_mode": "md"
        }

        if event.message.document:
            kwargs["attributes"] = event.message.document.attributes

        await user_client.send_file(
            group,
            data,
            **kwargs
        )

        remember(digest, sender.id, event.id)

        print(
            f"PDF отправлен. sender={sender.id}, "
            f"first_dep={first_dep}, hash={digest}"
        )

    except Exception as e:
        print(f"Ошибка обработки PDF: {e}")

        if OWNER_ID:
            try:
                await bot.send_message(
                    OWNER_ID,
                    f"❌ Ошибка обработки PDF:\n{type(e).__name__}: {e}"
                )
            except Exception:
                pass


async def install_monitor():
    global monitor_handler_installed

    if not user_client or monitor_handler_installed:
        return

    user_client.add_event_handler(
        incoming_pdf,
        events.NewMessage(incoming=True)
    )

    monitor_handler_installed = True
    print("PDF monitor installed")


async def restore_user_session():
    global user_client

    session_string = get_setting("user_session")

    if not session_string:
        return False

    client = TelegramClient(
        StringSession(session_string),
        API_ID,
        API_HASH
    )

    try:
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            del_setting("user_session")
            return False

        user_client = client
        await install_monitor()

        me = await user_client.get_me()
        print(f"Аккаунт восстановлен: {me.id}")

        return True

    except Exception as e:
        print(f"Не удалось восстановить аккаунт: {e}")

        try:
            await client.disconnect()
        except Exception:
            pass

        return False


async def save_current_session():
    if user_client:
        set_setting("user_session", user_client.session.save())


async def disconnect_user(delete_session=True):
    global user_client, monitor_handler_installed

    if user_client:
        try:
            await user_client.disconnect()
        except Exception:
            pass

    user_client = None
    monitor_handler_installed = False

    if delete_session:
        del_setting("user_session")


# ============================================================
# QR-АВТОРИЗАЦИЯ И ОБЩЕЕ ЗАВЕРШЕНИЕ ВХОДА
# ============================================================

QR_MAX_AUTO_RENEWALS = 4


def qr_keyboard():
    return [[
        Button.inline("🔄 Новый QR", b"qr_refresh"),
        Button.inline("❌ Отмена", b"qr_cancel")
    ]]


async def cancel_login(owner_id):
    """Закрывает незавершенную авторизацию и ее временное соединение."""
    state = login_states.pop(owner_id, None)
    if not state:
        return

    task = state.get("task")
    if task and task is not asyncio.current_task() and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    client = state.get("client")
    if client and client is not user_client:
        try:
            await client.disconnect()
        except Exception:
            pass


async def finish_login(owner_id, state):
    """Проверяет сессию, сохраняет ее и запускает PDF-монитор."""
    global user_client

    if login_states.get(owner_id) is not state:
        return

    client = state["client"]
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не подтвердил авторизацию")

    me = await client.get_me()
    user_client = client
    await save_current_session()
    await install_monitor()
    login_states.pop(owner_id, None)

    await bot.send_message(
        owner_id,
        f"✅ Аккаунт подключен.\n\n"
        f"Имя: {me.first_name or '-'}\n"
        f"ID: {me.id}\n\n"
        "Монитор PDF запущен. После перезапуска сессия восстановится автоматически.",
        buttons=main_keyboard()
    )


async def send_qr_image(owner_id, state, refreshed=False):
    """Присылает настоящий QR с текущей tg://login-ссылкой."""
    if login_states.get(owner_id) is not state or state.get("step") != "qr":
        return

    image = qrcode.make(state["qr"].url, box_size=10, border=4)
    data = io.BytesIO()
    image.save(data, format="PNG")
    data.seek(0)
    data.name = "telegram_login_qr.png"

    caption = (
        ("🔄 Новый QR для входа в Telegram.\n\n" if refreshed else
         "📷 Вход в аккаунт по QR-коду.\n\n")
        + "1. Открой Telegram на ДРУГОМ устройстве, где уже выполнен вход.\n"
        + "2. Настройки → Устройства → Подключить устройство.\n"
        + "3. Отсканируй этот QR-код и подтверди вход.\n\n"
        + "Бот ожидает подтверждения автоматически. QR действует недолго. "
          "Если не успел, нажми «Новый QR».\n\n"
        + "⚠️ Никому не пересылай QR-код: он даёт доступ к аккаунту."
    )
    await bot.send_file(owner_id, data, caption=caption, buttons=qr_keyboard())


async def wait_qr_login(owner_id, state):
    """Ожидает сканирования; по истечении срока переиздает QR."""
    try:
        while login_states.get(owner_id) is state and state.get("step") == "qr":
            try:
                # Telethon QRLogin.wait() ожидает до истечения срока самого токена.
                await state["qr"].wait()
            except asyncio.TimeoutError:
                if login_states.get(owner_id) is not state:
                    return
                if state["renewals"] >= QR_MAX_AUTO_RENEWALS:
                    await bot.send_message(
                        owner_id,
                        "⌛ Время ожидания QR истекло. Нажми «Новый QR» "
                        "для продолжения или «Отмена».",
                        buttons=qr_keyboard()
                    )
                    return
                state["renewals"] += 1
                await state["qr"].recreate()
                await send_qr_image(owner_id, state, refreshed=True)
                continue
            except SessionPasswordNeededError:
                if login_states.get(owner_id) is not state:
                    return
                state["step"] = "qr_password"
                await bot.send_message(
                    owner_id,
                    "✅ QR-код подтверждён.\n\n"
                    "🔐 Включена двухэтапная аутентификация. "
                    "Отправь пароль 2FA в этот личный чат с ботом. "
                    "Сообщение с паролем будет удалено после получения.\n\n"
                    "Для отмены: /cancel"
                )
                return

            # QR принят и аккаунт уже авторизован.
            await finish_login(owner_id, state)
            return

    except asyncio.CancelledError:
        raise
    except Exception as e:
        if login_states.get(owner_id) is state:
            try:
                await bot.send_message(
                    owner_id,
                    f"❌ Ошибка QR-входа: {type(e).__name__}: {e}\n"
                    "Попробуй подключить аккаунт заново.",
                    buttons=main_keyboard()
                )
            finally:
                # Не ждём самих себя при отмене ожидающей задачи.
                await cancel_login(owner_id)


async def begin_qr_login(owner_id):
    """Создает новую временную сессию и запускает прослушивание QR."""
    await cancel_login(owner_id)
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        qr = await client.qr_login()
        state = {
            "step": "qr", "client": client, "qr": qr,
            "renewals": 0, "task": None
        }
        login_states[owner_id] = state
        # Задача ожидания запускается сразу, иначе сканирование может не
        # завершить вход (QRLogin.wait должен уже слушать UpdateLoginToken).
        state["task"] = asyncio.create_task(wait_qr_login(owner_id, state))
        await send_qr_image(owner_id, state)
    except Exception:
        if login_states.get(owner_id, {}).get("client") is client:
            await cancel_login(owner_id)
        else:
            await client.disconnect()
        raise


async def refresh_qr_login(owner_id):
    state = login_states.get(owner_id)
    if not state or state.get("step") != "qr":
        return False

    task = state.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await state["qr"].recreate()
    state["renewals"] = 0
    state["task"] = asyncio.create_task(wait_qr_login(owner_id, state))
    await send_qr_image(owner_id, state, refreshed=True)
    return True


# ============================================================
# ПАНЕЛЬ
# ============================================================

@bot.on(events.NewMessage(pattern=r"^/start$"))
async def start_handler(event):
    if not event.is_private or event.sender_id != ADMIN_ID:
        return

    await event.respond(
        await panel_text(),
        buttons=main_keyboard()
    )


@bot.on(events.CallbackQuery)
async def callback_handler(event):
    if not event.is_private or event.sender_id != ADMIN_ID:
        await event.answer("Нет доступа", alert=True)
        return

    data = event.data

    if data == b"connect":
        if user_client and user_client.is_connected() and await user_client.is_user_authorized():
            await event.answer("Аккаунт уже подключен.", alert=True)
            return

        await event.respond(
            "🔐 Выбери способ подключения Telegram-аккаунта:",
            buttons=[
                [Button.inline("📷 Войти по QR-коду", b"connect_qr")],
                [Button.inline("📱 Войти по номеру", b"connect_phone")],
                [Button.inline("❌ Отмена", b"qr_cancel")]
            ]
        )
        await event.answer()
        return

    if data == b"connect_phone":
        if user_client and user_client.is_connected() and await user_client.is_user_authorized():
            await event.answer("Аккаунт уже подключен.", alert=True)
            return
        await cancel_login(event.sender_id)
        login_states[event.sender_id] = {"step": "phone"}
        await event.respond(
            "📱 Отправь номер телефона аккаунта Telegram.\n\n"
            "Пример: +79991234567\n\n"
            "Для отмены: /cancel"
        )
        await event.answer()
        return

    if data == b"connect_qr":
        if user_client and user_client.is_connected() and await user_client.is_user_authorized():
            await event.answer("Аккаунт уже подключен.", alert=True)
            return
        await event.answer("Создаю QR-код…")
        try:
            await begin_qr_login(event.sender_id)
        except Exception as e:
            await event.respond(
                f"❌ Не удалось создать QR: {type(e).__name__}: {e}",
                buttons=main_keyboard()
            )
        return

    if data == b"qr_refresh":
        await event.answer("Обновляю QR-код…")
        try:
            if not await refresh_qr_login(event.sender_id):
                await event.respond(
                    "Нет активного QR-входа. Нажми «Подключить аккаунт».",
                    buttons=main_keyboard()
                )
        except Exception as e:
            await event.respond(
                f"❌ Ошибка обновления QR: {type(e).__name__}: {e}"
            )
        return

    if data == b"qr_cancel":
        await cancel_login(event.sender_id)
        await event.answer("Авторизация отменена.")
        await event.respond("Авторизация отменена.", buttons=main_keyboard())
        return

    if data == b"status":
        await show_panel(event)
        await event.answer()
        return

    if data == b"group":
        if not user_client:
            await event.answer(
                "Сначала подключи пользовательский аккаунт.",
                alert=True
            )
            return

        group = await target_group()

        if group:
            await event.answer(
                f"Группа «{TARGET_GROUP}» найдена.",
                alert=True
            )
        else:
            await event.answer(
                f"Группа «{TARGET_GROUP}» не найдена.",
                alert=True
            )
        return

    if data == b"stats":
        await event.answer(
            f"Уникальных PDF отправлено: {sent_count()}",
            alert=True
        )
        return

    if data == b"restart_monitor":
        if not user_client:
            await event.answer(
                "Аккаунт не подключен.",
                alert=True
            )
            return

        try:
            if not user_client.is_connected():
                await user_client.connect()

            await install_monitor()

            await event.answer(
                "Монитор PDF работает.",
                alert=True
            )

        except Exception as e:
            await event.answer(
                f"Ошибка: {e}",
                alert=True
            )
        return

    if data == b"disconnect":
        if not user_client:
            await event.answer(
                "Аккаунт уже отключен.",
                alert=True
            )
            return

        await event.respond(
            "Отключить пользовательский аккаунт?\n"
            "Сохраненная сессия будет удалена.",
            buttons=[
                [
                    Button.inline("Да, отключить", b"disconnect_yes"),
                    Button.inline("Отмена", b"status")
                ]
            ]
        )
        await event.answer()
        return

    if data == b"disconnect_yes":
        await disconnect_user(delete_session=True)

        await event.answer(
            "Аккаунт отключен.",
            alert=True
        )

        await show_panel(event)
        return


# ============================================================
# АВТОРИЗАЦИЯ ЧЕРЕЗ БОТА
# ============================================================

@bot.on(events.NewMessage)
async def login_messages(event):
    global user_client

    if not event.is_private:
        return

    if event.sender_id != ADMIN_ID:
        return

    state = login_states.get(event.sender_id)

    if not state:
        return

    text = (event.raw_text or "").strip()

    if text == "/cancel":
        await cancel_login(event.sender_id)
        await event.respond("Авторизация отменена.", buttons=main_keyboard())
        return

    if state["step"] == "qr":
        await event.respond(
            "📷 Сейчас ожидается сканирование QR-кода. "
            "Отсканируй его в Telegram на другом устройстве или нажми «Новый QR».\n"
            "Для отмены: /cancel",
            buttons=qr_keyboard()
        )
        return

    # ШАГ 1 — НОМЕР
    if state["step"] == "phone":
        phone = text.replace(" ", "")

        if not phone.startswith("+"):
            await event.respond(
                "Номер должен начинаться с +.\n"
                "Например: +79991234567"
            )
            return

        client = TelegramClient(
            StringSession(),
            API_ID,
            API_HASH
        )

        try:
            await client.connect()

            sent = await client.send_code_request(phone)

            state.update({
                "step": "code",
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
                "client": client
            })

            await event.respond(
                "✉️ Код отправлен Telegram.\n\n"
                "Отправь код сюда.\n"
                "Можно отправить цифры подряд, например: 12345\n\n"
                "Для отмены: /cancel"
            )

        except PhoneNumberInvalidError:
            await client.disconnect()
            await event.respond(
                "❌ Telegram считает этот номер некорректным. "
                "Проверь номер и отправь его снова."
            )

        except Exception as e:
            await client.disconnect()
            await event.respond(
                f"❌ Не удалось отправить код:\n"
                f"{type(e).__name__}: {e}"
            )

        return

    # ШАГ 2 — КОД
    if state["step"] == "code":
        code = "".join(ch for ch in text if ch.isdigit())

        if not code:
            await event.respond("Отправь код цифрами.")
            return

        client = state["client"]

        try:
            await client.sign_in(
                phone=state["phone"],
                code=code,
                phone_code_hash=state["phone_code_hash"]
            )

            await finish_login(event.sender_id, state)

        except SessionPasswordNeededError:
            state["step"] = "password"

            await event.respond(
                "🔐 На аккаунте включена двухэтапная аутентификация.\n\n"
                "Отправь пароль 2FA.\n\n"
                "Для отмены: /cancel"
            )

        except PhoneCodeInvalidError:
            await event.respond(
                "❌ Неверный код. Попробуй ещё раз."
            )

        except PhoneCodeExpiredError:
            try:
                await client.disconnect()
            except Exception:
                pass

            login_states.pop(event.sender_id, None)

            await event.respond(
                "❌ Код истёк.\n"
                "Нажми «Подключить аккаунт» и запроси новый."
            )

        except Exception as e:
            await event.respond(
                f"❌ Ошибка входа:\n"
                f"{type(e).__name__}: {e}"
            )

        return

    # ШАГ 3 — 2FA
    if state["step"] in ("password", "qr_password"):
        client = state["client"]

        # По возможности не оставляем пароль 2FA в истории личного чата.
        try:
            await event.delete()
        except Exception:
            pass

        try:
            await client.sign_in(password=text)

            await finish_login(event.sender_id, state)

        except PasswordHashInvalidError:
            await event.respond(
                "❌ Неверный пароль 2FA. Попробуй ещё раз."
            )

        except Exception as e:
            await event.respond(
                f"❌ Ошибка 2FA:\n"
                f"{type(e).__name__}: {e}"
            )

        return


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    db_init()

    # Бот запускается сразу и НЕ требует input() в консоли.
    await bot.start(bot_token=BOT_TOKEN)

    # Если пользователь уже подключался через панель,
    # автоматически восстанавливаем его StringSession.
    restored = await restore_user_session()

    print("Telegram bot started")
    print("User session restored:", restored)

    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())