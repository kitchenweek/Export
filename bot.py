from telethon import TelegramClient, events
import asyncio
import random
import string
import time
from datetime import datetime
import logging
import re

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

# Настройки скорости (увеличенные задержки)
MAX_CONCURRENT_CHECKS = 2  # Меньше параллельных проверок
BATCH_SIZE = 5  # Меньше ссылок в пакете
CHECK_DELAY = 2.0  # Задержка между проверками
ACTIVATION_DELAY = 3.0  # Задержка после активации
BETWEEN_BATCHES = 1.0  # Задержка между пакетами

# ===== СОЗДАЕМ КЛИЕНТА БОТА =====
bot_client = TelegramClient(
    'bot_session',
    API_ID,
    API_HASH
)

# ===== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =====
SEND_BOT_USERNAME = '@send'
CRYPTOBOT_USERNAME = 'CryptoBot'
is_searching = False
search_task = None
activated_links = []
checked_count = 0
start_time = None
total_activated = 0
error_count = 0
processed_links = set()  # Для избежания дубликатов

# Данные пользователя
user_phone = None
is_authorized = False
user_client = None
rate_limiter = None

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

# ===== ФУНКЦИИ АВТОРИЗАЦИИ =====
async def start_auth(phone):
    """Начинает процесс авторизации"""
    global user_client, is_authorized, user_phone, rate_limiter
    
    try:
        user_phone = phone
        session_name = f"user_{phone.replace('+', '')}"
        
        user_client = TelegramClient(
            session_name,
            API_ID,
            API_HASH,
            connection_retries=5,
            retry_delay=2,
            auto_reconnect=True,
            flood_sleep_threshold=60
        )
        
        await user_client.connect()
        
        if await user_client.is_user_authorized():
            is_authorized = True
            me = await user_client.get_me()
            rate_limiter = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
            return True, f"✅ Уже авторизован как {me.first_name}"
        
        await user_client.send_code_request(phone)
        return False, "📱 Код подтверждения отправлен в Telegram"
        
    except Exception as e:
        logger.error(f"Ошибка авторизации: {e}")
        return False, f"❌ Ошибка: {str(e)}"

async def complete_auth(code):
    """Завершает авторизацию с кодом"""
    global is_authorized, rate_limiter
    
    try:
        await user_client.sign_in(user_phone, code)
        is_authorized = True
        me = await user_client.get_me()
        rate_limiter = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
        return True, f"✅ Авторизация успешна! {me.first_name}"
    except Exception as e:
        error = str(e)
        if 'password' in error.lower():
            return False, "🔑 Требуется пароль 2FA. Используйте /setpassword <пароль>"
        return False, f"❌ Ошибка: {error}"

async def complete_auth_with_password(password):
    """Завершает авторизацию с паролем 2FA"""
    global is_authorized, rate_limiter
    
    try:
        await user_client.sign_in(password=password)
        is_authorized = True
        me = await user_client.get_me()
        rate_limiter = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
        return True, f"✅ Авторизация успешна! {me.first_name}"
    except Exception as e:
        return False, f"❌ Ошибка: {str(e)}"

async def logout_user():
    """Выход из аккаунта"""
    global user_client, is_authorized
    
    if user_client:
        try:
            await user_client.disconnect()
        except:
            pass
        user_client = None
        is_authorized = False
        return True, "✅ Выход выполнен"
    return False, "❌ Не авторизован"

# ===== ФУНКЦИЯ АКТИВАЦИИ ССЫЛКИ (УЛУЧШЕННАЯ) =====
async def activate_link(link):
    """Активирует ссылку - открывает и проверяет результат"""
    try:
        # Извлекаем start параметр
        if 'start=' not in link:
            return False, "❌ Нет параметра start"
        
        start_param = link.split('start=')[1]
        
        # Убираем возможные пробелы и спецсимволы
        start_param = start_param.strip()
        
        logger.info(f"🔗 Активирую ссылку: {link}")
        logger.info(f"📝 Параметр start: {start_param}")
        
        # Отправляем команду /start с параметром в CryptoBot
        await user_client.send_message(CRYPTOBOT_USERNAME, f"/start {start_param}")
        logger.info(f"📤 Отправлено: /start {start_param}")
        
        # Ждем ответ от бота (увеличенная задержка)
        await asyncio.sleep(ACTIVATION_DELAY)
        
        # Получаем последние сообщения от CryptoBot
        responses = []
        async for msg in user_client.iter_messages(CRYPTOBOT_USERNAME, limit=5):
            if msg.text and len(msg.text) > 3:
                responses.append(msg.text)
                logger.info(f"📩 Ответ от CryptoBot: {msg.text[:100]}...")
        
        if not responses:
            return False, "❌ Нет ответа от CryptoBot"
        
        # Проверяем ответы на успешность
        for response in responses:
            text_lower = response.lower()
            
            # Ключевые слова успеха
            success_keywords = [
                'привет', 'добро пожаловать', 'успешно', 
                'активирован', 'готов', 'выберите', 'меню',
                'баланс', 'кошелек', 'открыт', 'доступен',
                'создан', 'запущен', 'работает'
            ]
            
            # Ключевые слова ошибки
            error_keywords = [
                'ошибка', 'не найден', 'не существует', 
                'недействительный', 'invalid', 'error',
                'неверный', 'истек', 'закончился'
            ]
            
            if any(keyword in text_lower for keyword in error_keywords):
                return False, f"❌ Ошибка: {response[:100]}..."
            elif any(keyword in text_lower for keyword in success_keywords):
                return True, f"✅ Успешно! {response[:100]}..."
        
        # Если не нашли ни успех, ни ошибку - считаем успехом
        return True, f"✅ Активирована (ответ): {responses[0][:100]}..."
        
    except Exception as e:
        logger.error(f"Ошибка активации {link}: {e}")
        return False, f"❌ Ошибка: {str(e)}"

# ===== ПРОВЕРКА ЧЕРЕЗ @send =====
async def check_with_send(link):
    """Проверяет ссылку через @send"""
    try:
        await user_client.send_message(SEND_BOT_USERNAME, link)
        logger.info(f"📤 Отправлено в @send: {link}")
        
        # Увеличенная задержка для получения ответа
        await asyncio.sleep(CHECK_DELAY)
        
        # Получаем ответ от @send
        async for msg in user_client.iter_messages(SEND_BOT_USERNAME, limit=3):
            if msg.text and msg.text != link and len(msg.text) > 5:
                text_lower = msg.text.lower()
                logger.info(f"📩 Ответ от @send: {msg.text[:100]}...")
                
                # Проверяем на ошибки
                error_keywords = ['error', 'invalid', 'не найден', 'не существует', 'ошибка']
                if any(keyword in text_lower for keyword in error_keywords):
                    return False, f"@send: {msg.text[:100]}..."
                else:
                    return True, f"@send: {msg.text[:100]}..."
        
        return False, "❌ Нет ответа от @send"
        
    except Exception as e:
        logger.error(f"Ошибка проверки @send: {e}")
        return False, f"❌ Ошибка: {str(e)}"

# ===== ПОЛНЫЙ ЦИКЛ ПРОВЕРКИ И АКТИВАЦИИ =====
async def check_and_activate_link(link):
    """Полный цикл: проверка через @send + активация"""
    if not is_authorized or not user_client:
        return False, "❌ Не авторизован"
    
    # Проверяем дубликаты
    if link in processed_links:
        return False, "⚠️ Уже обработана"
    
    processed_links.add(link)
    
    try:
        # ШАГ 1: Проверяем через @send
        logger.info(f"🔍 Проверяю через @send: {link}")
        send_ok, send_msg = await check_with_send(link)
        
        if not send_ok:
            logger.info(f"❌ @send отклонил: {link}")
            return False, send_msg
        
        logger.info(f"✅ @send подтвердил: {link}")
        
        # ШАГ 2: Активируем ссылку
        logger.info(f"🎯 Активирую: {link}")
        activate_ok, activate_msg = await activate_link(link)
        
        if activate_ok:
            # Сохраняем активированную ссылку
            activated_links.append({
                'link': link,
                'result': activate_msg,
                'time': datetime.now().strftime('%H:%M:%S'),
                'attempt': checked_count
            })
            return True, activate_msg
        else:
            return False, f"❌ @send OK, но активация не удалась: {activate_msg}"
            
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        return False, f"❌ Ошибка: {str(e)}"

# ===== ПАКЕТНАЯ ОБРАБОТКА =====
async def batch_check_and_activate(links):
    """Параллельная проверка и активация пакета ссылок"""
    tasks = []
    for link in links:
        tasks.append(check_and_activate_link(link))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results

# ===== ПОИСКОВЫЙ РАБОЧИЙ ПРОЦЕСС =====
async def search_worker():
    """Основной рабочий процесс поиска и активации"""
    global is_searching, checked_count, start_time, total_activated, error_count
    
    if not is_authorized:
        logger.error("❌ Не авторизован для поиска")
        return
    
    checked_count = 0
    start_time = time.time()
    batch = []
    
    logger.info(f"🚀 Поиск и активация запущены!")
    logger.info(f"⚡ Параллельных проверок: {MAX_CONCURRENT_CHECKS}")
    logger.info(f"📦 Размер пакета: {BATCH_SIZE}")
    logger.info(f"⏱ Задержка между проверками: {CHECK_DELAY} сек")
    
    while is_searching:
        try:
            # Генерируем пакет ссылок
            batch = []
            for _ in range(BATCH_SIZE):
                link = generate_cryptobot_link()
                # Проверяем на дубликаты
                while link in processed_links:
                    link = generate_cryptobot_link()
                batch.append(link)
            
            logger.info(f"📦 Сгенерировано {len(batch)} ссылок")
            
            # Проверяем и активируем
            results = await batch_check_and_activate(batch)
            
            # Обновляем счетчики
            for link, result in zip(batch, results):
                checked_count += 1
                
                if isinstance(result, tuple) and result[0]:
                    is_valid, msg = result
                    if is_valid:
                        total_activated += 1
                        logger.info(f"🎯 АКТИВИРОВАНА #{total_activated}!")
                        logger.info(f"🔗 {link}")
                        
                        # Отправляем уведомление в сохраненные
                        try:
                            await user_client.send_message(
                                'me',
                                f"🎯 **АКТИВИРОВАНА ССЫЛКА #{total_activated}!**\n\n"
                                f"🔗 `{link}`\n\n"
                                f"📊 Результат: {msg}\n"
                                f"🔢 Попыток: {checked_count}\n"
                                f"⚡ Скорость: {get_speed():.1f} ссылок/сек"
                            )
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления: {e}")
            
            # Статистика каждые 10 проверок
            if checked_count % 10 == 0:
                speed = get_speed()
                logger.info(
                    f"📊 Статус: {checked_count} проверок | "
                    f"✅ Активировано: {total_activated} | "
                    f"{speed:.2f} ссылок/сек"
                )
            
            # Задержка между пакетами
            logger.info(f"⏳ Пауза {BETWEEN_BATCHES} сек между пакетами...")
            await asyncio.sleep(BETWEEN_BATCHES)
            
        except Exception as e:
            error_count += 1
            logger.error(f"❌ Ошибка в поиске: {e}")
            await asyncio.sleep(2)

# ===== ОБРАБОТЧИКИ КОМАНД =====

@bot_client.on(events.NewMessage(pattern='/setphone'))
async def set_phone(event):
    parts = event.message.text.split()
    if len(parts) < 2:
        await event.reply(
            "📱 **Введите номер телефона:**\n"
            "`/setphone +71234567890`\n\n"
            "📌 Формат: +7XXXXXXXXXX"
        )
        return
    
    phone = parts[1].strip()
    
    if not phone.startswith('+') or not phone[1:].isdigit():
        await event.reply("❌ Неверный формат. Используйте: `/setphone +71234567890`")
        return
    
    status_msg = await event.reply(f"📱 Подключаюсь к номеру {phone}...")
    
    result, message = await start_auth(phone)
    
    if result:
        await status_msg.edit(f"✅ {message}")
        return
    
    await status_msg.edit(
        f"📱 {message}\n\n"
        f"💡 Введите код из Telegram:\n"
        f"`/setcode 12345`"
    )

@bot_client.on(events.NewMessage(pattern='/setcode'))
async def set_code(event):
    if not user_phone:
        await event.reply("❌ Сначала введите номер: `/setphone +71234567890`")
        return
    
    parts = event.message.text.split()
    if len(parts) < 2:
        await event.reply("📱 Введите код: `/setcode 12345`")
        return
    
    code = parts[1].strip()
    
    if not code.isdigit():
        await event.reply("❌ Код должен состоять только из цифр")
        return
    
    status_msg = await event.reply("🔐 Проверяю код...")
    
    result, message = await complete_auth(code)
    
    if result:
        await status_msg.edit(f"✅ {message}")
        await status_msg.reply(
            "🚀 **Авторизация успешна!**\n\n"
            "📌 Команды:\n"
            "/search - Поиск и активация\n"
            "/activate <ссылка> - Активировать ссылку\n"
            "/generate - Сгенерировать ссылки\n"
            "/status - Статистика\n"
            "/found - Активированные ссылки"
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

@bot_client.on(events.NewMessage(pattern='/setpassword'))
async def set_password(event):
    parts = event.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("🔑 Введите пароль: `/setpassword ваш_пароль`")
        return
    
    password = parts[1].strip()
    
    if len(password) < 4:
        await event.reply("❌ Пароль слишком короткий")
        return
    
    status_msg = await event.reply("🔐 Проверяю пароль...")
    
    result, message = await complete_auth_with_password(password)
    
    if result:
        await status_msg.edit(f"✅ {message}")
        await status_msg.reply(
            "🚀 **Авторизация успешна!**\n\n"
            "📌 Команды:\n"
            "/search - Поиск и активация\n"
            "/activate <ссылка> - Активировать ссылку"
        )
    else:
        await status_msg.edit(f"❌ {message}")

@bot_client.on(events.NewMessage(pattern='/search'))
async def start_search(event):
    global is_searching, search_task
    
    if not is_authorized:
        await event.reply("❌ Сначала авторизуйтесь: `/setphone +71234567890`")
        return
    
    if is_searching:
        await event.reply("⚠️ Поиск уже запущен! Используйте /stop для остановки.")
        return
    
    is_searching = True
    search_task = asyncio.create_task(search_worker())
    
    await event.reply(
        f"🚀 **Поиск и активация запущены!**\n\n"
        f"🔍 Проверка через @send (задержка {CHECK_DELAY} сек)\n"
        f"✅ Активация в CryptoBot (задержка {ACTIVATION_DELAY} сек)\n"
        f"⚡ Параллельных потоков: {MAX_CONCURRENT_CHECKS}\n"
        f"📦 Размер пакета: {BATCH_SIZE} ссылок\n\n"
        f"📊 Для статистики: /status"
    )

@bot_client.on(events.NewMessage(pattern='/stop'))
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
        f"✅ Активировано: {total_activated}\n"
        f"⏱ Время: {elapsed:.1f} сек\n"
        f"🚀 Скорость: {speed:.2f} ссылок/сек"
    )

@bot_client.on(events.NewMessage(pattern='/status'))
async def show_status(event):
    if not is_searching:
        await event.reply("⚠️ Поиск не запущен. Используйте /search для запуска.")
        return
    
    elapsed = get_elapsed()
    speed = get_speed()
    
    status_text = (
        f"📊 **Статистика:**\n\n"
        f"🔄 Статус: {'🟢 Активен' if is_searching else '🔴 Остановлен'}\n"
        f"🔍 Проверено: {checked_count}\n"
        f"✅ Активировано: {total_activated}\n"
        f"⚡ Скорость: {speed:.2f} ссылок/сек\n"
        f"⏱ Время: {elapsed:.1f} сек\n"
        f"❌ Ошибок: {error_count}\n"
        f"🔄 Потоков: {MAX_CONCURRENT_CHECKS}\n"
        f"⏱ Задержка: {CHECK_DELAY} сек\n\n"
    )
    
    if activated_links:
        last = activated_links[-1]
        status_text += f"📝 Последняя активированная:\n`{last['link']}`\n⏱ {last['time']}"
    else:
        status_text += "📝 Нет активированных ссылок"
    
    await event.reply(status_text)

@bot_client.on(events.NewMessage(pattern='/found'))
async def show_found_links(event):
    if not activated_links:
        await event.reply("❌ Пока не активировано ни одной ссылки.")
        return
    
    last_links = activated_links[-10:]
    links_text = ""
    for i, item in enumerate(last_links, 1):
        links_text += f"#{i} `{item['link']}`\n"
        links_text += f"   ⏱ {item['time']} | {item['result'][:50]}...\n\n"
    
    await event.reply(
        f"✅ **Активировано ссылок: {len(activated_links)}**\n\n"
        f"📌 Последние 10:\n{links_text}"
    )

@bot_client.on(events.NewMessage(pattern='/clear'))
async def clear_found(event):
    global activated_links, total_activated, processed_links
    count = len(activated_links)
    activated_links = []
    total_activated = 0
    processed_links = set()
    await event.reply(f"🧹 Очищено {count} активированных ссылок.")

@bot_client.on(events.NewMessage(pattern='/generate'))
async def generate_links(event):
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

@bot_client.on(events.NewMessage(pattern='/activate'))
async def activate_specific_link(event):
    """Активирует конкретную ссылку"""
    parts = event.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply(
            "🔗 **Активировать ссылку:**\n"
            "`/activate http://t.me/CryptoBot?start=CQ...`"
        )
        return
    
    link = parts[1].strip()
    
    if not link.startswith('http://t.me/CryptoBot?start='):
        await event.reply("❌ Неверный формат ссылки")
        return
    
    if not is_authorized:
        await event.reply("❌ Сначала авторизуйтесь")
        return
    
    status_msg = await event.reply(f"🔗 Активирую ссылку...\n`{link}`")
    
    result, message = await activate_link(link)
    
    if result:
        await status_msg.edit(f"✅ {message}\n\n🔗 `{link}`")
        # Сохраняем в активированные
        activated_links.append({
            'link': link,
            'result': message,
            'time': datetime.now().strftime('%H:%M:%S'),
            'attempt': checked_count + 1
        })
    else:
        await status_msg.edit(f"❌ {message}\n\n🔗 `{link}`")

@bot_client.on(events.NewMessage(pattern='/logout'))
async def logout(event):
    global is_searching, search_task
    
    if is_searching:
        await event.reply("⏹ Сначала остановите поиск: /stop")
        return
    
    result, message = await logout_user()
    await event.reply(message)

@bot_client.on(events.NewMessage(pattern='/authstatus'))
async def auth_status(event):
    if is_authorized and user_client:
        try:
            me = await user_client.get_me()
            await event.reply(
                f"✅ **Авторизован**\n\n"
                f"👤 {me.first_name}\n"
                f"📱 {me.phone}\n"
                f"🆔 ID: {me.id}"
            )
        except:
            await event.reply("✅ Авторизован")
    else:
        await event.reply(
            "❌ **Не авторизован**\n\n"
            "📌 Введите номер:\n"
            "`/setphone +71234567890`"
        )

@bot_client.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    await event.reply(
        f"🚀 **Ultra Speed Bot - Активатор ссылок**\n\n"
        f"📌 **Авторизация:**\n"
        f"/setphone +71234567890 - Ввести номер\n"
        f"/setcode 12345 - Ввести код\n"
        f"/setpassword пароль - Ввести пароль 2FA\n"
        f"/authstatus - Статус\n"
        f"/logout - Выйти\n\n"
        f"📌 **Команды:**\n"
        f"/generate [count] - Сгенерировать ссылки\n"
        f"/activate <ссылка> - Активировать ссылку\n"
        f"/search - Автоматический поиск и активация\n"
        f"/stop - Остановить\n"
        f"/status - Статистика\n"
        f"/found - Показать активированные\n"
        f"/clear - Очистить список\n\n"
        f"⚙️ **Настройки:**\n"
        f"⏱ Проверка @send: {CHECK_DELAY} сек\n"
        f"⏱ Активация: {ACTIVATION_DELAY} сек\n"
        f"⚡ Потоков: {MAX_CONCURRENT_CHECKS}\n"
        f"📦 Пакет: {BATCH_SIZE} ссылок"
    )

# ===== ЗАПУСК =====
async def main():
    try:
        await bot_client.start(bot_token=BOT_TOKEN)
        
        print("🚀 ULTRA SPEED BOT - АКТИВАТОР ССЫЛОК!")
        print("📌 Бот ищет ссылки через @send и активирует их")
        print("⚙️ Настройки:")
        print(f"   ⏱ Задержка @send: {CHECK_DELAY} сек")
        print(f"   ⏱ Задержка активации: {ACTIVATION_DELAY} сек")
        print(f"   📦 Размер пакета: {BATCH_SIZE}")
        print("💡 /setphone +71234567890 - ввести номер")
        print("💡 /search - запустить поиск и активацию")
        print("✅ Бот запущен!")
        
        await bot_client.run_until_disconnected()
        
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        if bot_client:
            await bot_client.disconnect()
        if user_client:
            await user_client.disconnect()

if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Фатальная ошибка: {e}")
    finally:
        loop.close()