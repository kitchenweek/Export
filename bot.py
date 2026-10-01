import asyncio
import hashlib
import os
import sqlite3
from pathlib import Path

from telethon import TelegramClient, events, Button
from telethon.tl.types import User

API_ID = int(os.environ['TG_API_ID'])
API_HASH = os.environ['TG_API_HASH']
BOT_TOKEN = os.environ['BOT_TOKEN']
PHONE = os.environ.get('TG_PHONE', '')
TARGET_GROUP = 'ЧЕКИ МАКСИМ'
TRIGGER = 'добрый день, ваш заказ прибыл к нам на склад в мск'
DB = Path('pdf_bot.db')

user_client = TelegramClient('user_session', API_ID, API_HASH)
bot = TelegramClient('bot_session', API_ID, API_HASH)


def db_init():
    with sqlite3.connect(DB) as con:
        con.execute('''CREATE TABLE IF NOT EXISTS sent_pdf (
            sha256 TEXT PRIMARY KEY,
            sender_id INTEGER,
            source_message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        con.commit()


def already_sent(digest: str) -> bool:
    with sqlite3.connect(DB) as con:
        return con.execute('SELECT 1 FROM sent_pdf WHERE sha256=?', (digest,)).fetchone() is not None


def remember(digest: str, sender_id: int, msg_id: int):
    with sqlite3.connect(DB) as con:
        con.execute('INSERT OR IGNORE INTO sent_pdf(sha256,sender_id,source_message_id) VALUES(?,?,?)',
                    (digest, sender_id, msg_id))
        con.commit()


async def target_group():
    async for dialog in user_client.iter_dialogs():
        if dialog.name == TARGET_GROUP and (dialog.is_group or dialog.is_channel):
            return dialog.entity
    return None


async def trigger_seen_before(chat_id: int, current_message_id: int) -> bool:
    # Проверяем сообщения ДО текущего PDF. Регистр и лишние пробелы игнорируются.
    async for m in user_client.iter_messages(chat_id, offset_id=current_message_id, limit=None):
        text = ' '.join((m.raw_text or '').lower().split())
        if TRIGGER in text:
            return True
    return False


def mention_for(sender) -> str:
    if getattr(sender, 'username', None):
        return '@' + sender.username
    name = ' '.join(x for x in [getattr(sender, 'first_name', None), getattr(sender, 'last_name', None)] if x)
    name = name or str(sender.id)
    return f'[{name}](tg://user?id={sender.id})'


@user_client.on(events.NewMessage(incoming=True))
async def incoming(event):
    if not event.is_private or not event.file:
        return

    mime = (event.file.mime_type or '').lower()
    name = (event.file.name or '').lower()
    if mime != 'application/pdf' and not name.endswith('.pdf'):
        return

    sender = await event.get_sender()
    if not isinstance(sender, User) or sender.bot:
        return

    data = await event.download_media(bytes)
    if not data:
        return
    digest = hashlib.sha256(data).hexdigest()
    if already_sent(digest):
        return

    group = await target_group()
    if group is None:
        await bot.send_message('me', f'Не найдена группа «{TARGET_GROUP}».')
        return

    first_dep = not await trigger_seen_before(event.chat_id, event.id)
    tag = mention_for(sender)
    caption = f'PDF от {tag}'
    if first_dep:
        caption += f'\nПервый деп — {tag}'

    # Загружаем оригинальный PDF в целевую группу. Это надежнее, чем forward,
    # если нужна собственная подпись с тегом.
    await user_client.send_file(group, data, caption=caption, parse_mode='md',
                                attributes=event.message.document.attributes if event.message.document else None)
    remember(digest, sender.id, event.id)


@bot.on(events.NewMessage(pattern='/start'))
async def start_panel(event):
    await event.respond(
        'Панель PDF-монитора',
        buttons=[
            [Button.inline('Статус', b'status')],
            [Button.inline('Проверить группу', b'group')],
            [Button.inline('Статистика', b'stats')],
        ]
    )


@bot.on(events.CallbackQuery)
async def callbacks(event):
    if event.data == b'status':
        await event.answer('Монитор запущен', alert=True)
    elif event.data == b'group':
        g = await target_group()
        await event.answer('Группа найдена' if g else f'Группа «{TARGET_GROUP}» не найдена', alert=True)
    elif event.data == b'stats':
        with sqlite3.connect(DB) as con:
            count = con.execute('SELECT COUNT(*) FROM sent_pdf').fetchone()[0]
        await event.answer(f'Уникальных PDF отправлено: {count}', alert=True)


async def main():
    db_init()
    if PHONE:
        await user_client.start(phone=PHONE)
    else:
        await user_client.start()
    await bot.start(bot_token=BOT_TOKEN)
    print('PDF monitor started')
    await asyncio.gather(user_client.run_until_disconnected(), bot.run_until_disconnected())


if __name__ == '__main__':
    asyncio.run(main())