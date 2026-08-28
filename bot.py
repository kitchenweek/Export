"""Telegram-архив удалённых сообщений.

API_ID, API_HASH и BOT_TOKEN уже указаны в коде.
Перед запуском задайте DATABASE_URL и SESSION_SECRET.
Дополнительно: MEDIA_DIR, MAX_MEDIA_MB и LOG_LEVEL.

Установка: pip install -r requirements.txt
Запуск: python bot.py
"""

import asyncio
import base64
import hashlib
import html
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from telethon import Button, TelegramClient, events
from telethon.errors import FloodWaitError, PhoneCodeExpiredError, PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.sessions import StringSession


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("deleted-archive")

load_dotenv()


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"
BOT_TOKEN = "8605386447:AAHnZAM-HfL0o7g-dzj9SaayWhpQKMp-xLs"
DATABASE_URL = required("DATABASE_URL").replace("postgres://", "postgresql+asyncpg://", 1).replace(
    "postgresql://", "postgresql+asyncpg://", 1
)
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./data/media")).resolve()
MAX_MEDIA_MB = max(0, int(os.getenv("MAX_MEDIA_MB", "20")))
PAGE_SIZE = 5

secret = required("SESSION_SECRET").encode()
FERNET = Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    owner_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    session_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notifications: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class SavedMessage(Base):
    __tablename__ = "saved_messages"
    __table_args__ = (UniqueConstraint("owner_id", "chat_id", "message_id", name="uq_saved_message"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(Integer, index=True)
    chat_title: Mapped[str] = mapped_column(String(255), default="Без названия")
    sender_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sender_name: Mapped[str] = mapped_column(String(255), default="Неизвестно")
    text: Mapped[str] = mapped_column(Text, default="")
    media_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
bot = TelegramClient(StringSession(), API_ID, API_HASH)
user_clients: dict[int, TelegramClient] = {}
login_states: dict[int, dict] = {}


def enc(value: str) -> str:
    return FERNET.encrypt(value.encode()).decode()


def dec(value: str) -> str:
    return FERNET.decrypt(value.encode()).decode()


def display_name(entity) -> str:
    if entity is None:
        return "Неизвестно"
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    name = " ".join(filter(None, [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]))
    return name or getattr(entity, "username", None) or str(getattr(entity, "id", "Неизвестно"))


def main_buttons(connected: bool, notifications: bool = True):
    rows = []
    if connected:
        rows.extend([
            [Button.inline("🗑 Удалённые чаты", b"chats:0"), Button.inline("✉️ Сообщения Telegram", b"service:0")],
            [Button.inline("🔔 Уведомления: " + ("ВКЛ" if notifications else "ВЫКЛ"), b"toggle")],
            [Button.inline("👤 Аккаунт", b"account"), Button.inline("⚙️ Помощь", b"help")],
        ])
    else:
        rows.extend([[Button.inline("➕ Подключить аккаунт", b"connect")], [Button.inline("⚙️ Помощь", b"help")]])
    return rows


async def account_for(owner_id: int) -> Optional[Account]:
    async with Session() as db:
        return await db.get(Account, owner_id)


async def show_main(event, notice: str = ""):
    account = await account_for(event.sender_id)
    connected = bool(account and account.session_enc and event.sender_id in user_clients)
    text = "<b>Архив удалённых сообщений</b>\n\n"
    if notice:
        text += html.escape(notice) + "\n\n"
    text += "Статус: " + ("🟢 аккаунт подключён" if connected else "⚪️ аккаунт не подключён")
    buttons = main_buttons(connected, account.notifications if account else True)
    if isinstance(event, events.CallbackQuery.Event):
        await event.edit(text, buttons=buttons, parse_mode="html")
    else:
        await event.respond(text, buttons=buttons, parse_mode="html")


async def safe_delete_message(event):
    try:
        await event.delete()
    except Exception:
        pass


async def save_message(owner_id: int, event):
    message = event.message
    chat_id = event.chat_id
    if chat_id is None:
        return
    try:
        chat, sender = await asyncio.gather(event.get_chat(), event.get_sender())
    except Exception:
        chat, sender = None, None

    media_path = None
    size = getattr(getattr(message, "file", None), "size", None) or 0
    if message.media and MAX_MEDIA_MB and (not size or size <= MAX_MEDIA_MB * 1024 * 1024):
        folder = MEDIA_DIR / str(owner_id) / str(chat_id)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            downloaded = await message.download_media(file=str(folder / str(message.id)))
            media_path = str(Path(downloaded).resolve()) if downloaded else None
        except Exception as exc:
            log.warning("Не удалось сохранить медиа owner=%s chat=%s msg=%s: %s", owner_id, chat_id, message.id, exc)

    values = dict(
        owner_id=owner_id,
        chat_id=chat_id,
        message_id=message.id,
        chat_title=display_name(chat)[:255],
        sender_id=getattr(sender, "id", None),
        sender_name=display_name(sender)[:255],
        text=message.raw_text or "",
        media_path=media_path,
        sent_at=message.date,
    )
    async with Session() as db:
        existing = await db.scalar(select(SavedMessage).where(
            SavedMessage.owner_id == owner_id,
            SavedMessage.chat_id == chat_id,
            SavedMessage.message_id == message.id,
        ))
        if existing:
            for key, value in values.items():
                if key not in {"owner_id", "chat_id", "message_id"}:
                    setattr(existing, key, value)
        else:
            db.add(SavedMessage(**values))
        await db.commit()

    if chat_id == 777000:
        account = await account_for(owner_id)
        if account and account.notifications:
            body = html.escape((message.raw_text or "[медиа]")[:3500])
            await bot.send_message(owner_id, f"✉️ <b>Новое сообщение от Telegram</b>\n\n{body}", parse_mode="html")


def deletion_text(item: SavedMessage) -> str:
    stamp = item.sent_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC") if item.sent_at else "время неизвестно"
    body = html.escape(item.text or "[сообщение без текста]")
    return (
        f"🗑 <b>Удалено сообщение</b>\n"
        f"Чат: <b>{html.escape(item.chat_title)}</b>\n"
        f"Автор: {html.escape(item.sender_name)}\n"
        f"Отправлено: {stamp}\n\n{body[:3000]}"
    )


async def mark_deleted(owner_id: int, event):
    ids = list(event.deleted_ids)
    if not ids:
        return
    conditions = [SavedMessage.owner_id == owner_id, SavedMessage.message_id.in_(ids), SavedMessage.deleted_at.is_(None)]
    if event.chat_id is not None:
        conditions.append(SavedMessage.chat_id == event.chat_id)
    async with Session() as db:
        items = list((await db.scalars(select(SavedMessage).where(*conditions))).all())
        now = datetime.now(timezone.utc)
        for item in items:
            item.deleted_at = now
        await db.commit()
    account = await account_for(owner_id)
    if not account or not account.notifications:
        return
    for item in items:
        try:
            if item.media_path and Path(item.media_path).is_file():
                await bot.send_file(owner_id, item.media_path, caption=deletion_text(item), parse_mode="html")
            else:
                await bot.send_message(owner_id, deletion_text(item), parse_mode="html")
        except Exception as exc:
            log.warning("Не удалось отправить уведомление owner=%s: %s", owner_id, exc)


async def start_user_client(owner_id: int, session_string: str) -> TelegramClient:
    old = user_clients.pop(owner_id, None)
    if old:
        await old.disconnect()
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

    @client.on(events.NewMessage())
    async def on_new(event):
        await save_message(owner_id, event)

    @client.on(events.MessageEdited())
    async def on_edit(event):
        await save_message(owner_id, event)

    @client.on(events.MessageDeleted())
    async def on_delete(event):
        await mark_deleted(owner_id, event)

    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Сессия аккаунта недействительна")
    user_clients[owner_id] = client
    me = await client.get_me()
    log.info("Аккаунт %s запущен для owner=%s", getattr(me, "id", "?"), owner_id)
    return client


async def finish_login(owner_id: int, client: TelegramClient, phone: str):
    session_string = client.session.save()
    async with Session() as db:
        account = await db.get(Account, owner_id)
        if account is None:
            account = Account(owner_id=owner_id)
            db.add(account)
        account.phone = phone
        account.session_enc = enc(session_string)
        await db.commit()
    login_states.pop(owner_id, None)
    await start_user_client(owner_id, session_string)


@bot.on(events.NewMessage(pattern=r"^/(start|menu)$"))
async def start_handler(event):
    if not event.is_private:
        return
    login_states.pop(event.sender_id, None)
    await show_main(event)


@bot.on(events.NewMessage(incoming=True))
async def input_handler(event):
    if not event.is_private or (event.raw_text or "").startswith("/"):
        return
    owner_id = event.sender_id
    state = login_states.get(owner_id)
    if not state:
        return
    value = (event.raw_text or "").strip()
    step = state["step"]
    if step == "phone":
        phone = re.sub(r"[\s()\-]", "", value)
        if not re.fullmatch(r"\+\d{7,15}", phone):
            await event.respond("Введите номер в международном формате, например: <code>+491234567890</code>", parse_mode="html")
            return
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except FloodWaitError as exc:
            await client.disconnect()
            await event.respond(f"Telegram просит подождать {exc.seconds} сек. Попробуйте позднее.")
            login_states.pop(owner_id, None)
            return
        except Exception as exc:
            await client.disconnect()
            await event.respond(f"Не удалось отправить код: {html.escape(str(exc))}", parse_mode="html")
            login_states.pop(owner_id, None)
            return
        state.update(step="code", phone=phone, client=client, phone_code_hash=sent.phone_code_hash)
        await safe_delete_message(event)
        await bot.send_message(owner_id, "Код отправлен Telegram. Введите его цифрами. Сообщение с кодом будет удалено из этого чата после обработки.", buttons=[[Button.inline("Отмена", b"cancel_login")]])
    elif step == "code":
        code = re.sub(r"\D", "", value)
        await safe_delete_message(event)
        try:
            await state["client"].sign_in(state["phone"], code, phone_code_hash=state["phone_code_hash"])
            await finish_login(owner_id, state["client"], state["phone"])
            await bot.send_message(owner_id, "✅ Аккаунт подключён. С этого момента новые сообщения сохраняются.", buttons=main_buttons(True))
        except SessionPasswordNeededError:
            state["step"] = "password"
            await bot.send_message(owner_id, "На аккаунте включён облачный пароль. Введите пароль 2FA — сообщение будет удалено после обработки.", buttons=[[Button.inline("Отмена", b"cancel_login")]])
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as exc:
            await event.respond("Код неверный или истёк. Нажмите «Отмена» и начните подключение заново.", buttons=[[Button.inline("Отмена", b"cancel_login")]])
        except Exception as exc:
            await event.respond(f"Ошибка входа: {html.escape(str(exc))}", parse_mode="html")
    elif step == "password":
        await safe_delete_message(event)
        try:
            await state["client"].sign_in(password=value)
            await finish_login(owner_id, state["client"], state["phone"])
            await bot.send_message(owner_id, "✅ Аккаунт подключён. С этого момента новые сообщения сохраняются.", buttons=main_buttons(True))
        except Exception as exc:
            await event.respond(f"Пароль не принят: {html.escape(str(exc))}", parse_mode="html")


@bot.on(events.CallbackQuery())
async def callback_handler(event):
    owner_id = event.sender_id
    data = event.data.decode()
    await event.answer()
    if data == "menu":
        await show_main(event)
    elif data == "connect":
        state = login_states.pop(owner_id, None)
        if state and state.get("client"):
            await state["client"].disconnect()
        login_states[owner_id] = {"step": "phone"}
        await event.edit(
            "<b>Подключение аккаунта</b>\n\nОтправьте номер телефона с кодом страны, например <code>+491234567890</code>. Подключайте только свой аккаунт.",
            buttons=[[Button.inline("Отмена", b"cancel_login")]], parse_mode="html"
        )
    elif data == "cancel_login":
        state = login_states.pop(owner_id, None)
        if state and state.get("client"):
            await state["client"].disconnect()
        await show_main(event, "Подключение отменено.")
    elif data == "toggle":
        async with Session() as db:
            account = await db.get(Account, owner_id)
            if account:
                account.notifications = not account.notifications
                await db.commit()
        await show_main(event)
    elif data == "account":
        account = await account_for(owner_id)
        if not account or not account.session_enc:
            await show_main(event, "Аккаунт не подключён.")
            return
        phone = html.escape(account.phone or "не указан")
        await event.edit(f"<b>Подключённый аккаунт</b>\n\nТелефон: <code>{phone}</code>", buttons=[
            [Button.inline("Отключить аккаунт", b"disconnect_confirm")],
            [Button.inline("Очистить архив", b"clear_confirm")],
            [Button.inline("◀️ Назад", b"menu")],
        ], parse_mode="html")
    elif data == "disconnect_confirm":
        await event.edit("Отключить аккаунт? Сохранённый архив удалённых сообщений останется.", buttons=[
            [Button.inline("Да, отключить", b"disconnect"), Button.inline("Отмена", b"account")]
        ])
    elif data == "disconnect":
        client = user_clients.pop(owner_id, None)
        if client:
            await client.log_out()
        async with Session() as db:
            account = await db.get(Account, owner_id)
            if account:
                account.session_enc = None
                account.phone = None
                await db.commit()
        await show_main(event, "Аккаунт отключён. Архив сохранён.")
    elif data == "clear_confirm":
        await event.edit("Удалить весь сохранённый архив без возможности восстановления?", buttons=[
            [Button.inline("Да, удалить", b"clear"), Button.inline("Отмена", b"account")]
        ])
    elif data == "clear":
        async with Session() as db:
            paths = list((await db.scalars(select(SavedMessage.media_path).where(
                SavedMessage.owner_id == owner_id, SavedMessage.media_path.is_not(None)
            ))).all())
            await db.execute(delete(SavedMessage).where(SavedMessage.owner_id == owner_id))
            await db.commit()
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
        await show_main(event, "Архив очищен.")
    elif data.startswith("chats:"):
        page = max(0, int(data.split(":")[1]))
        async with Session() as db:
            query = (
                select(SavedMessage.chat_id, func.max(SavedMessage.chat_title), func.count(SavedMessage.id))
                .where(SavedMessage.owner_id == owner_id, SavedMessage.deleted_at.is_not(None))
                .group_by(SavedMessage.chat_id).order_by(func.max(SavedMessage.deleted_at).desc())
                .offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1)
            )
            rows = (await db.execute(query)).all()
        has_next = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        buttons = [[Button.inline(f"{title[:32]} · {count}", f"history:{chat_id}:0".encode())] for chat_id, title, count in rows]
        nav = []
        if page:
            nav.append(Button.inline("⬅️", f"chats:{page-1}".encode()))
        if has_next:
            nav.append(Button.inline("➡️", f"chats:{page+1}".encode()))
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("◀️ Меню", b"menu")])
        await event.edit("<b>Удалённые чаты</b>\n\n" + ("Выберите чат:" if rows else "Удалённых сообщений пока нет."), buttons=buttons, parse_mode="html")
    elif data.startswith("history:"):
        _, chat_raw, page_raw = data.split(":")
        chat_id, page = int(chat_raw), max(0, int(page_raw))
        async with Session() as db:
            q = (select(SavedMessage).where(
                SavedMessage.owner_id == owner_id, SavedMessage.chat_id == chat_id, SavedMessage.deleted_at.is_not(None)
            ).order_by(SavedMessage.deleted_at.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1))
            items = list((await db.scalars(q)).all())
        has_next = len(items) > PAGE_SIZE
        items = items[:PAGE_SIZE]
        title = items[0].chat_title if items else "Чат"
        blocks = []
        for item in items:
            when = item.sent_at.strftime("%d.%m %H:%M") if item.sent_at else "—"
            body = html.escape(item.text or "[медиа/нет текста]")
            blocks.append(f"<b>{html.escape(item.sender_name)}</b> · {when}\n{body[:500]}")
        nav = []
        if page:
            nav.append(Button.inline("⬅️", f"history:{chat_id}:{page-1}".encode()))
        if has_next:
            nav.append(Button.inline("➡️", f"history:{chat_id}:{page+1}".encode()))
        buttons = ([nav] if nav else []) + [[Button.inline("◀️ К чатам", b"chats:0")]]
        await event.edit(f"<b>{html.escape(title)}</b>\n\n" + ("\n\n———\n\n".join(blocks) if blocks else "История пуста."), buttons=buttons, parse_mode="html")
    elif data.startswith("service:"):
        page = max(0, int(data.split(":")[1]))
        async with Session() as db:
            q = (select(SavedMessage).where(
                SavedMessage.owner_id == owner_id, SavedMessage.chat_id == 777000
            ).order_by(SavedMessage.sent_at.desc()).offset(page * PAGE_SIZE).limit(PAGE_SIZE + 1))
            items = list((await db.scalars(q)).all())
        has_next = len(items) > PAGE_SIZE
        items = items[:PAGE_SIZE]
        blocks = []
        for item in items:
            when = item.sent_at.strftime("%d.%m.%Y %H:%M") if item.sent_at else "—"
            blocks.append(f"<b>{when}</b>\n{html.escape(item.text or '[медиа]')[:650]}")
        nav = []
        if page:
            nav.append(Button.inline("⬅️", f"service:{page-1}".encode()))
        if has_next:
            nav.append(Button.inline("➡️", f"service:{page+1}".encode()))
        buttons = ([nav] if nav else []) + [[Button.inline("◀️ Меню", b"menu")]]
        await event.edit("<b>Сообщения от Telegram</b>\n\n" + ("\n\n———\n\n".join(blocks) if blocks else "Сообщений пока нет."), buttons=buttons, parse_mode="html")
    elif data == "help":
        await event.edit(
            "<b>Как это работает</b>\n\n"
            "После подключения аккаунта сервис сохраняет новые сообщения и отмечает их при событии удаления. "
            "Сообщения, удалённые до подключения или пока сервер был выключен, восстановить невозможно. "
            "Секретные чаты Telegram API недоступны. Медиа сохраняются только в пределах установленного лимита.\n\n"
            "Подключайте только собственный аккаунт. Не передавайте доступ к управляющему боту другим людям.",
            buttons=[[Button.inline("◀️ Назад", b"menu")]], parse_mode="html"
        )


async def restore_clients():
    async with Session() as db:
        accounts = list((await db.scalars(select(Account).where(Account.session_enc.is_not(None)))).all())
    for account in accounts:
        try:
            await start_user_client(account.owner_id, dec(account.session_enc))
        except Exception as exc:
            log.exception("Не удалось восстановить аккаунт owner=%s: %s", account.owner_id, exc)


async def main():
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await bot.start(bot_token=BOT_TOKEN)
    await restore_clients()
    log.info("Бот запущен")
    try:
        await bot.run_until_disconnected()
    finally:
        for client in list(user_clients.values()):
            await client.disconnect()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())