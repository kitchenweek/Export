from telethon import TelegramClient, events
import asyncio
import random
import string
import time
from datetime import datetime
import logging
import os
import json

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# ===== КОНФИГУРАЦИЯ =====
API_ID = 36658004
API_HASH = '99c5c1f4bad289e77d4e9e6149d634bc'
BOT_TOKEN = '8900018990:AAFhiQmako8YNwmKKiibkiXtOna2c-GlZig'

# Настройки скорости
MAX_CONCURRENT_CHECKS = 5
BATCH_SIZE = 20
MIN_DELAY = 0.2

# ===== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =====
SEND_BOT_USERNAME = '@send'
is_searching = False
search_task = None
found_links = []
checked_count = 0
start_time = None
total_found = 0
error_count = 0

# Данные пользователя
user_phone = None
user_password = None
is_authorized = False
auth_code = None
auth_step = 'idle'  # idle, waiting_phone, waiting_code, waiting_password

# Словарь для хранения сессий пользователей
user_sessions = {}

# Семафор для контроля параллельных запросов
rate_limiter = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

# ===== ФУНКЦИЯ ГЕНЕРАЦИИ ССЫЛОК =====
CHARS = string.ascii_letters + string.digits

def generate_cryptobot_link():
    """Генерирует ссылку с префиксом CQ и 10 случайными символами"""
    prefix = "CQ"
    random_part = ''.join(random.choices(CHARS, k=10))
    return f"http://t.me/CryptoBot?start={prefix}{random_part}"

def get_speed():
    """Вычисляет скорость проверки"""
    if start_time and checked_count > 0:
        elapsed = time.time() - start_time
        return checked_count / elapsed if elapsed > 0 else 0
    return 0

def get_elapsed():
    """Возвращает прошедшее время"""
    if start_time:
        return time.time() - start_time
    return 0

# ===== КЛАСС ДЛЯ УПРАВЛЕНИЯ АВТОРИЗАЦИЕЙ =====
class AuthManager:
    def __init__(self):
        self.phone = None
        self.password = None
        self.client = None
        self.is_authenticated = False
        
    async def start_auth(self, phone, password=None):
        """Начинает процесс авторизации"""
        self.phone = phone
        self.password = password
        
        # Создаем клиент для этого пользователя
        session_name = f"user_{phone.replace('+', '')}"
        self.client = TelegramClient(
            session_name,
            API_ID,
            API_HASH,
            connection_retries=3,
            retry_delay=1,
            auto_reconnect=True
        )
        
        try:
            await self.client.connect()
            
            # Проверяем, есть ли сохраненная сессия
            if await self.client.is_user_authorized():
                self.is_authenticated = True
                me = await self.client.get_me()
                return True, f"✅ Уже авторизован как {me.first_name}"
            
            # Отправляем код
            await self.client.send_code_request(phone)
            return False, "📱 Код подтверждения отправлен в Telegram"
            
        except Exception as e:
            return False, f"❌ Ошибка: {str(e)}"
    
    async def complete_auth(self, code):
        """Завершает авторизацию с кодом"""
        try:
            await self.client.sign_in(self.phone, code)
            self.is_authenticated = True
            me = await self.client.get_me()
            return True, f"✅ Авторизация успешна! {me.first_name}"
        except Exception as e:
            error = str(e)
            if 'password' in error.lower():
                return False, "🔑 Требуется пароль 2FA. Используйте /setpassword <пароль>"
            return False, f"❌ Ошибка: {error}"
    
    async def complete_auth_with_password(self, password):
        """Завершает авторизацию с паролем 2FA"""
        try:
            await self.client.sign_in(password=password)
            self.is_authenticated = True
            me = await self.client.get_me()
            return True, f"✅ Авторизация успешна! {me.first_name}"
        except Exception as e:
            return False, f"❌ Ошибка: {str(e)}"
    
    async def logout(self):
        """Выход из аккаунта"""
        if self.client:
            await self.client.disconnect()
            self.is_authenticated = False
            self.client = None
            return True, "✅ Выход выполнен"
        return False, "❌ Не авторизован"

# Глобальный менеджер авторизации
auth_manager = AuthManager()

# ===== ОСНОВНЫЕ ФУНКЦИИ =====
async def check_link_fast(link):
    """Быстрая проверка ссылки через @send"""
    if not auth_manager.is_authenticated or not auth_manager.client:
        return False, "❌ Не авторизован"
    
    try:
        async with rate_limiter:
            # Отправляем ссылку
            await auth_manager.client.send_message(SEND_BOT_USERNAME, link)
            
            # Минимальная задержка для получения ответа
            await asyncio.sleep(0.3)
            
            # Получаем последний ответ
            async for msg in auth_manager.client.iter_messages(SEND_BOT_USERNAME, limit=1):
                if msg.text and msg.text != link and len(msg.text) > 5:
                    text_lower = msg.text.lower()
                    error_keywords = ['error', 'invalid', 'не найден', 'не существует', 'ошибка']
                    if any(keyword in text_lower for keyword in error_keywords):
                        return False, msg.text
                    else:
                        return True, msg.text
                        
    except Exception as e:
        logger.error(f"Ошибка проверки {link}: {e}")
        return False, f"Ошибка: {str(e)}"
    
    return False, "Нет ответа от бота"

async def batch_check_links(links):
    """Параллельная проверка пакета ссылок"""
    tasks = [check_link_fast(link) for link in links]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results

async def search_worker():
    """Основной рабочий процесс поиска"""
    global is_searching, checked_count, found_links, start_time, total_found, error_count
    
    if not auth_manager.is_authenticated:
        logger.error("❌ Не авторизован для поиска")
        return
    
    checked_count = 0
    found_links = []
    start_time = time.time()
    batch = []
    found_in_batch = []
    
    logger.info(f"🚀 Поиск запущен!")
    logger.info(f"⚡ Параллельных проверок: {MAX_CONCURRENT_CHECKS}")
    logger.info(f"📦 Размер пакета: {BATCH_SIZE}")
    
    while is_searching:
        try:
            batch = [generate_cryptobot_link() for _ in range(BATCH_SIZE)]
            results = await batch_check_links(batch)
            
            for link, result in zip(batch, results):
                checked_count += 1
                
                if isinstance(result, tuple) and result[0]:
                    is_valid, msg = result
                    total_found += 1
                    
                    link_data = {
                        'link': link,
                        'result': msg or '✅ Валидная',
                        'time': datetime.now().strftime('%H:%M:%S'),
                        'attempt': checked_count
                    }
                    found_links.append(link_data)
                    found_in_batch.append(link_data)
                    
                    logger.info(f"🎯 НАЙДЕНА РАБОЧАЯ ССЫЛКА #{total_found}!")
                    logger.info(f"🔗 {link}")
                    
                    try:
                        await auth_manager.client.send_message(
                            'me',
                            f"🎯 **РАБОЧАЯ ССЫЛКА #{total_found}!**\n\n"
                            f"🔗 `{link}`\n\n"
                            f"📊 Проверено: {checked_count}\n"
                            f"✅ Найдено: {total_found}\n"
                            f"⚡ Скорость: {get_speed():.1f} ссылок/сек"
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки уведомления: {e}")
            
            if found_in_batch:
                logger.info(f"✅ Найдено {len(found_in_batch)} ссылок в пакете!")
                found_in_batch = []
            
            if checked_count % 100 == 0:
                speed = get_speed()
                logger.info(
                    f"📊 Статус: {checked_count} проверок | "
                    f"{total_found} найдено | "
                    f"{speed:.1f} ссылок/сек"
                )
            
            await asyncio.sleep(MIN_DELAY)
            
        except Exception as e:
            error_count += 1
            logger.error(f"❌ Ошибка в поиске: {e}")
            await asyncio.sleep(0.5)

# ===== ОБРАБОТЧИКИ КОМАНД =====

# Команда для ввода номера
@client.on(events.NewMessage(pattern='/setphone'))
async def set_phone(event):
    """Установка номера телефона"""
    global auth_step
    
    parts = event.message.text.split()
    if len(parts) < 2:
        await event.reply(
            "📱 **Введите номер телефона:**\n"
            "`/setphone +71234567890`\n\n"
            "📌 Формат: +7XXXXXXXXXX (международный)"
        )
        return
    
    phone = parts[1].strip()
    
    # Проверяем формат
    if not phone.startswith('+') or not phone[1:].isdigit():
        await event.reply("❌ Неверный формат. Используйте: `/setphone +71234567890`")
        return
    
    if len(phone) < 10:
        await event.reply("❌ Слишком короткий номер. Введите полный номер с кодом страны.")
        return
    
    # Начинаем авторизацию
    status_msg = await event.reply(f"📱 Подключаюсь к номеру {phone}...")
    
    result, message = await auth_manager.start_auth(phone)
    
    if result:
        await status_msg.edit(f"✅ {message}")
        return
    
    await status_msg.edit(
        f"📱 {message}\n\n"
        f"💡 Введите код из Telegram:\n"
        f"`/setcode 12345`"
    )

# Команда для ввода кода
@client.on(events.NewMessage(pattern='/setcode'))
async def set_code(event):
    """Ввод кода подтверждения"""
    if not auth_manager.phone:
        await event.reply("❌ Сначала введите номер: `/setphone +71234567890`")
        return
    
    parts = event.message.text.split()
    if len(parts) < 2:
        await event.reply(
            "📱 **Введите код подтверждения:**\n"
            "`/setcode 12345`\n\n"
            "📌 Код пришел в Telegram"
        )
        return
    
    code = parts[1].strip()
    
    if not code.isdigit():
        await event.reply("❌ Код должен состоять только из цифр")
        return
    
    status_msg = await event.reply("🔐 Проверяю код...")
    
    result, message = await auth_manager.complete_auth(code)
    
    if result:
        await status_msg.edit(f"✅ {message}")
        await status_msg.reply(
            "🚀 **Теперь вы авторизованы!**\n\n"
            "📌 Доступные команды:\n"
            "/search - Запустить поиск\n"
            "/generate - Сгенерировать ссылки\n"
            "/status - Статистика\n"
            "/logout - Выйти из аккаунта"
        )
    else:
        if "пароль" in message.lower():
            await status_msg.edit(
                f"🔑 {message}\n\n"
                f"💡 Введите пароль 2FA:\n"
                f"`/setpassword ваш_пароль`"
            )
        else:
            await status_msg.edit(f"❌ {message}")

# Команда для ввода пароля 2FA
@client.on(events.NewMessage(pattern='/setpassword'))
async def set_password(event):
    """Ввод пароля 2FA"""
    parts = event.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply(
            "🔑 **Введите пароль 2FA:**\n"
            "`/setpassword ваш_пароль`"
        )
        return
    
    password = parts[1].strip()
    
    if len(password) < 4:
        await event.reply("❌ Пароль слишком короткий (минимум 4 символа)")
        return
    
    status_msg = await event.reply("🔐 Проверяю пароль...")
    
    result, message = await auth_manager.complete_auth_with_password(password)
    
    if result:
        await status_msg.edit(f"✅ {message}")
        await status_msg.reply(
            "🚀 **Теперь вы авторизованы!**\n\n"
            "📌 Доступные команды:\n"
            "/search - Запустить поиск\n"
            "/generate - Сгенерировать ссылки\n"
            "/status - Статистика\n"
            "/logout - Выйти из аккаунта"
        )
    else:
        await status_msg.edit(f"❌ {message}")

# Команда для выхода
@client.on(events.NewMessage(pattern='/logout'))
async def logout(event):
    """Выход из аккаунта"""
    global is_searching, search_task
    
    if is_searching:
        await event.reply("⏹ Сначала остановите поиск: /stop")
        return
    
    result, message = await auth_manager.logout()
    await event.reply(message)

# Команда для статуса авторизации
@client.on(events.NewMessage(pattern='/authstatus'))
async def auth_status(event):
    """Проверка статуса авторизации"""
    if auth_manager.is_authenticated:
        try:
            me = await auth_manager.client.get_me()
            await event.reply(
                f"✅ **Авторизован**\n\n"
                f"👤 {me.first_name} {me.last_name or ''}\n"
                f"📱 {me.phone}\n"
                f"🆔 ID: {me.id}\n"
                f"{'@' + me.username if me.username else ''}"
            )
        except:
            await event.reply("✅ Авторизован, но не могу получить данные")
    else:
        await event.reply(
            "❌ **Не авторизован**\n\n"
            "📌 Введите номер:\n"
            "`/setphone +71234567890`"
        )

# Команда для генерации ссылок
@client.on(events.NewMessage(pattern='/generate'))
async def generate_links(event):
    """Генерирует и показывает ссылки"""
    parts = event.message.text.split()
    count = 10
    if len(parts) > 1:
        try:
            count = min(int(parts[1]), 50)
        except:
            count = 10
    
    links = [generate_cryptobot_link() for _ in range(count)]
    
    response = f"🔗 **Сгенерировано {count} ссылок (CQ + 10 символов):**\n\n"
    for i, link in enumerate(links, 1):
        after_cq = link.split('start=CQ')[1] if 'start=CQ' in link else ''
        response += f"{i}. `{link}`\n"
        response += f"   📝 После CQ: `{after_cq}` (10 символов)\n\n"
    
    await event.reply(response)

# Остальные команды (search, stop, status, found, clear)
@client.on(events.NewMessage(pattern='/search'))
async def start_search(event):
    global is_searching, search_task
    
    if not auth_manager.is_authenticated:
        await event.reply("❌ Сначала авторизуйтесь: `/setphone +71234567890`")
        return
    
    if is_searching:
        await event.reply("⚠️ Поиск уже запущен! Используйте /stop для остановки.")
        return
    
    is_searching = True
    search_task = asyncio.create_task(search_worker())
    
    await event.reply(
        f"🚀 **Поиск запущен!**\n\n"
        f"⚡ Скорость: МАКСИМАЛЬНАЯ\n"
        f"🔄 Параллельных потоков: {MAX_CONCURRENT_CHECKS}\n"
        f"📦 Размер пакета: {BATCH_SIZE}\n\n"
        f"📊 Для просмотра статистики используйте /status"
    )

@client.on(events.NewMessage(pattern='/stop'))
async def stop_search(event):
    global is_searching, search_task
    
    if not is_searching:
        await event.reply("⚠️ Поиск не запущен.")
        return
    
    is_searching = False
    
    if search_task:
        search_task.cancel()
        try:
            await search_task
        except asyncio.CancelledError:
            pass
        search_task = None
    
    elapsed = get_elapsed()
    speed = get_speed()
    
    await event.reply(
        f"⏹ **Поиск остановлен!**\n\n"
        f"📊 **Итог:**\n"
        f"🔍 Проверено: {checked_count}\n"
        f"✅ Найдено: {total_found}\n"
        f"⏱ Время: {elapsed:.1f} сек\n"
        f"🚀 Скорость: {speed:.1f} ссылок/сек"
    )

@client.on(events.NewMessage(pattern='/status'))
async def show_status(event):
    if not is_searching:
        await event.reply("⚠️ Поиск не запущен. Используйте /search для запуска.")
        return
    
    elapsed = get_elapsed()
    speed = get_speed()
    
    status_text = (
        f"📊 **Статистика поиска:**\n\n"
        f"🔄 Статус: {'🟢 Активен' if is_searching else '🔴 Остановлен'}\n"
        f"🔍 Проверено: {checked_count}\n"
        f"✅ Найдено: {total_found}\n"
        f"📈 Процент: {(total_found/checked_count*100):.2f}%\n"
        f"⚡ Скорость: {speed:.1f} ссылок/сек\n"
        f"⏱ Время: {elapsed:.1f} сек\n"
        f"🔄 Потоков: {MAX_CONCURRENT_CHECKS}\n\n"
    )
    
    if found_links:
        last = found_links[-1]
        status_text += f"📝 Последняя найденная:\n`{last['link']}`\n⏱ {last['time']}"
    else:
        status_text += "📝 Нет находок"
    
    await event.reply(status_text)

@client.on(events.NewMessage(pattern='/found'))
async def show_found_links(event):
    if not found_links:
        await event.reply("❌ Пока не найдено ни одной рабочей ссылки.")
        return
    
    last_links = found_links[-5:]
    links_text = "\n\n".join([
        f"#{i+1} `{item['link']}`\n   ⏱ {item['time']} | Попытка #{item['attempt']}"
        for i, item in enumerate(last_links)
    ])
    
    await event.reply(
        f"✅ **Найдено ссылок: {len(found_links)}**\n\n"
        f"📌 Последние 5:\n{links_text}"
    )

@client.on(events.NewMessage(pattern='/clear'))
async def clear_found(event):
    global found_links, total_found
    count = len(found_links)
    found_links = []
    total_found = 0
    await event.reply(f"🧹 Очищено {count} найденных ссылок.")

@client.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    await event.reply(
        f"🚀 **Ultra Speed Bot - CryptoBot Checker**\n\n"
        f"📌 **Команды для авторизации:**\n"
        f"/setphone +71234567890 - Ввести номер телефона\n"
        f"/setcode 12345 - Ввести код из Telegram\n"
        f"/setpassword пароль - Ввести пароль 2FA\n"
        f"/authstatus - Проверить статус авторизации\n"
        f"/logout - Выйти из аккаунта\n\n"
        f"📌 **Основные команды:**\n"
        f"/generate [количество] - Сгенерировать ссылки\n"
        f"/search - Запустить поиск\n"
        f"/stop - Остановить поиск\n"
        f"/status - Статистика\n"
        f"/found - Показать найденные ссылки\n"
        f"/clear - Очистить найденные\n\n"
        f"⚡ Параллельных проверок: {MAX_CONCURRENT_CHECKS}\n"
        f"🔗 Формат: CQ + 10 символов"
    )

# ===== ЗАПУСК =====
async def main():
    try:
        print("🚀 ULTRA SPEED BOT WITH PHONE AUTH!")
        print("📌 Команды для авторизации доступны в боте")
        print("💡 /setphone +71234567890 - ввести номер")
        print("💡 /setcode 12345 - ввести код")
        
        await client.start(bot_token=BOT_TOKEN)
        print("✅ Бот запущен!")
        print(f"📱 Бот: @{BOT_TOKEN.split(':')[0]}")
        
        await client.run_until_disconnected()
        
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        await client.disconnect()

if __name__ == '__main__':
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Фатальная ошибка: {e}")
    finally:
        try:
            loop.close()
        except:
            pass