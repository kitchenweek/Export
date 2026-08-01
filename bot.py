from telethon import TelegramClient, events
import asyncio
import random
import string
import time
from datetime import datetime
import logging

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

# ===== ОПТИМАЛЬНЫЕ НАСТРОЙКИ (БЕЗ FLOOD) =====
MAX_CONCURRENT_CHECKS = 1  # ТОЛЬКО 1 поток!
BATCH_SIZE = 2  # Всего 2 ссылки за раз
CHECK_DELAY = 5.0  # Задержка 5 секунд
ACTIVATION_DELAY = 4.0  # Задержка 4 секунды
BETWEEN_BATCHES = 3.0  # Пауза 3 секунды
MAX_RETRIES = 3  # Повторов при ошибке

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
processed_links = set()
flood_wait_active = False

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

def extract_start_param(link):
    """Извлекает параметр start из ссылки"""
    if 'start=' in link:
        return link.split('start=')[1].strip()
    return None

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
            flood_sleep_threshold=120  # Увеличен порог
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

# ===== ОБРАБОТЧИК FLOOD WAIT =====
async def safe_send_message(client, entity, message):
    """Безопасная отправка сообщения с обработкой flood wait"""
    global flood_wait_active
    
    for attempt in range(MAX_RETRIES):
        try:
            await client.send_message(entity, message)
            return True, None
        except Exception as e:
            error = str(e)
            if 'flood' in error.lower() or 'wait' in error.lower():
                # Извлекаем время ожидания
                import re
                wait_time = 60  # По умолчанию 60 секунд
                match = re.search(r'wait for (\d+)', error)
                if match:
                    wait_time = int(match.group(1)) + 5
                elif re.search(r'(\d+) seconds', error):
                    wait_time = int(re.search(r'(\d+) seconds', error).group(1)) + 5
                
                flood_wait_active = True
                logger.warning(f"⏳ Flood wait {wait_time} секунд...")
                
                # Показываем прогресс
                for i in range(int(wait_time), 0, -5):
                    logger.info(f"⏳ Ожидание {i} сек...")
                    await asyncio.sleep(5)
                
                flood_wait_active = False
                continue
            else:
                return False, str(e)
    
    return False, "Превышено количество попыток"

# ===== ФУНКЦИЯ АКТИВАЦИИ ССЫЛКИ =====
async def activate_link(link):
    """Активирует ссылку - отправляет /start с параметром в CryptoBot"""
    try:
        start_param = extract_start_param(link)
        if not start_param:
            return False, "❌ Нет параметра start"
        
        logger.info(f"🔗 Активирую: {link}")
        logger.info(f"📝 Параметр: {start_param}")
        
        command = f"/start {start_param}"
        logger.info(f"📤 Отправляю в {CRYPTOBOT_USERNAME}: {command}")
        
        # Безопасная отправка
        success, error = await safe_send_message(user_client, CRYPTOBOT_USERNAME, command)
        
        if not success:
            return False, f"❌ Ошибка отправки: {error}"
        
        logger.info(f"✅ Отправлено: {command}")
        
        # Ждем ответ
        await asyncio.sleep(ACTIVATION_DELAY)
        
        # Получаем ответ
        responses = []
        async for msg in user_client.iter_messages(CRYPTOBOT_USERNAME, limit=5):
            if msg.text and len(msg.text) > 3:
                responses.append(msg.text)
        
        if not responses:
            return False, "❌ Нет ответа от CryptoBot"
        
        # Проверяем ответы
        for response in responses:
            text_lower = response.lower()
            
            success_keywords = ['привет', 'добро пожаловать', 'успешно', 'активирован', 
                              'готов', 'выберите', 'меню', 'баланс', 'кошелек']
            error_keywords = ['ошибка', 'не найден', 'не существует', 'недействительный', 
                            'invalid', 'error', 'неверный', 'истек']
            
            if any(keyword in text_lower for keyword in error_keywords):
                return False, f"❌ Ошибка: {response[:100]}..."
            elif any(keyword in text_lower for keyword in success_keywords):
                return True, f"✅ Успешно! {response[:100]}..."
        
        return True, f"✅ Активирована (ответ): {responses[0][:100]}..."
        
    except Exception as e:
        logger.error(f"Ошибка активации: {e}")
        return False, f"❌ Ошибка: {str(e)}"

# ===== ПРОВЕРКА ЧЕРЕЗ @send =====
async def check_with_send(link):
    """Проверяет ссылку через @send"""
    try:
        logger.info(f"🔍 Проверяю через @send: {link}")
        
        # Безопасная отправка
        success, error = await safe_send_message(user_client, SEND_BOT_USERNAME, link)
        
        if not success:
            return False, f"❌ Ошибка отправки: {error}"
        
        await asyncio.sleep(CHECK_DELAY)
        
        async for msg in user_client.iter_messages(SEND_BOT_USERNAME, limit=3):
            if msg.text and msg.text != link and len(msg.text) > 5:
                text_lower = msg.text.lower()
                
                error_keywords = ['error', 'invalid', 'не найден', 'не существует', 'ошибка']
                if any(keyword in text_lower for keyword in error_keywords):
                    return False, f"@send: {msg.text[:100]}..."
                else:
                    return True, f"@send: {msg.text[:100]}..."
        
        return False, "❌ Нет ответа от @send"
        
    except Exception as e:
        logger.error(f"Ошибка проверки: {e}")
        return False, f"❌ Ошибка: {str(e)}"

# ===== ПОЛНЫЙ ЦИКЛ =====
async def check_and_activate_link(link):
    """Полный цикл: проверка через @send + активация"""
    if not is_authorized or not user_client:
        return False, "❌ Не авторизован"
    
    if link in processed_links:
        return False, "⚠️ Уже обработана"
    
    processed_links.add(link)
    
    try:
        # ШАГ 1: Проверяем через @send
        send_ok, send_msg = await check_with_send(link)
        
        if not send_ok:
            logger.info(f"❌ @send отклонил: {link}")
            return False, send_msg
        
        logger.info(f"✅ @send подтвердил: {link}")
        
        # ШАГ 2: Активируем
        logger.info(f"🎯 Активирую: {link}")
        activate_ok, activate_msg = await activate_link(link)
        
        if activate_ok:
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

# ===== ПОИСКОВЫЙ РАБОЧИЙ ПРОЦЕСС =====
async def search_worker():
    """Основной рабочий процесс поиска и активации"""
    global is_searching, checked_count, start_time, total_activated, error_count, flood_wait_active
    
    if not is_authorized:
        logger.error("❌ Не авторизован для поиска")
        return
    
    checked_count = 0
    start_time = time.time()
    
    logger.info(f"🚀 Поиск и активация запущены!")
    logger.info(f"⚡ Потоков: {MAX_CONCURRENT_CHECKS}")
    logger.info(f"📦 Пакет: {BATCH_SIZE} ссылок")
    logger.info(f"⏱ Задержка: {CHECK_DELAY} сек")
    logger.info(f"🛡️ Flood защита включена")
    
    while is_searching:
        try:
            # Если flood wait активен - ждем
            if flood_wait_active:
                logger.info("⏳ Ожидание окончания flood wait...")
                await asyncio.sleep(10)
                continue
            
            # Генерируем ссылки
            batch = []
            for _ in range(BATCH_SIZE):
                link = generate_cryptobot_link()
                while link in processed_links:
                    link = generate_cryptobot_link()
                batch.append(link)
            
            logger.info(f"📦 Обрабатываю {len(batch)} ссылок...")
            
            # Обрабатываем по одной (медленно, но без flood)
            for link in batch:
                if not is_searching:
                    break
                
                checked_count += 1
                result = await check_and_activate_link(link)
                
                if isinstance(result, tuple) and result[0]:
                    is_valid, msg = result
                    if is_valid:
                        total_activated += 1
                        logger.info(f"🎯 АКТИВИРОВАНА #{total_activated}!")
                        logger.info(f"🔗 {link}")
                        
                        try:
                            start_param = extract_start_param(link)
                            await user_client.send_message(
                                'me',
                                f"🎯 **АКТИВИРОВАНА ССЫЛКА #{total_activated}!**\n\n"
                                f"🔗 `{link}`\n"
                                f"📝 `/start {start_param}`\n\n"
                                f"📊 {msg}\n"
                                f"🔢 Попыток: {checked_count}"
                            )
                        except Exception as e:
                            logger.error(f"Ошибка уведомления: {e}")
                
                # Ждем перед следующей ссылкой (важно!)
                await asyncio.sleep(CHECK_DELAY)
            
            # Статистика
            speed = get_speed()
            logger.info(
                f"📊 Статус: {checked_count} проверок | "
                f"✅ Активировано: {total_activated} | "
                f"{speed:.2f} ссылок/сек"
            )
            
            # Пауза между пакетами
            logger.info(f"⏳ Пауза {BETWEEN_BATCHES} сек...")
            await asyncio.sleep(BETWEEN_BATCHES)
            
        except Exception as e:
            error_count += 1
            logger.error(f"❌ Ошибка: {e}")
            await asyncio.sleep(5)

# ===== КОМАНДЫ =====
@bot_client.on(events.NewMessage(pattern='/setphone'))
async def set_phone(event):
    parts = event.message.text.split()
    if len(parts) < 2:
        await event.reply("📱 Введите номер: `/setphone +71234567890`")
        return
    
    phone = parts[1].strip()
    
    if not phone.startswith('+') or not phone[1:].isdigit():
        await event.reply("❌ Неверный формат")
        return
    
    status_msg = await event.reply(f"📱 Подключаюсь к {phone}...")
    result, message = await start_auth(phone)
    
    if result:
        await status_msg.edit(f"✅ {message}")
    else:
        await status_msg.edit(f"📱 {message}\n\n💡 Введите код: `/setcode 12345`")

@bot_client.on(events.NewMessage(pattern='/setcode'))
async def set_code(event):
    if not user_phone:
        await event.reply("❌ Сначала введите номер")
        return
    
    parts = event.message.text.split()
    if len(parts) < 2:
        await event.reply("📱 Введите код: `/setcode 12345`")
        return
    
    code = parts[1].strip()
    
    if not code.isdigit():
        await event.reply("❌ Только цифры")
        return
    
    status_msg = await event.reply("🔐 Проверяю код...")
    result, message = await complete_auth(code)
    
    if result:
        await status_msg.edit(f"✅ {message}")
        await status_msg.reply(
            "🚀 **Авторизован!**\n\n"
            "📌 /search - Запустить поиск\n"
            "📌 /status - Статистика\n"
            "📌 /found - Активированные ссылки"
        )
    else:
        if "пароль" in message.lower():
            await status_msg.edit(f"🔑 {message}\n\n💡 `/setpassword ваш_пароль`")
        else:
            await status_msg.edit(f"❌ {message}")

@bot_client.on(events.NewMessage(pattern='/setpassword'))
async def set_password(event):
    parts = event.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("🔑 Введите пароль: `/setpassword ваш_пароль`")
        return
    
    password = parts[1].strip()
    status_msg = await event.reply("🔐 Проверяю пароль...")
    result, message = await complete_auth_with_password(password)
    
    if result:
        await status_msg.edit(f"✅ {message}")
        await status_msg.reply("🚀 Авторизован! Используйте /search")
    else:
        await status_msg.edit(f"❌ {message}")

@bot_client.on(events.NewMessage(pattern='/search'))
async def start_search(event):
    global is_searching, search_task
    
    if not is_authorized:
        await event.reply("❌ Сначала авторизуйтесь")
        return
    
    if is_searching:
        await event.reply("⚠️ Поиск уже запущен")
        return
    
    is_searching = True
    search_task = asyncio.create_task(search_worker())
    
    await event.reply(
        f"🚀 **Поиск запущен!**\n\n"
        f"⚡ Медленный режим (без flood)\n"
        f"⏱ Задержка: {CHECK_DELAY} сек\n"
        f"📦 Пакет: {BATCH_SIZE} ссылок\n"
        f"🛡️ Flood защита активна\n\n"
        f"📌 /stop - Остановить"
    )

@bot_client.on(events.NewMessage(pattern='/stop'))
async def stop_search(event):
    global is_searching, search_task
    
    if not is_searching:
        await event.reply("⚠️ Поиск не запущен")
        return
    
    is_searching = False
    
    if search_task:
        search_task.cancel()
        try:
            await search_task
        except:
            pass
        search_task = None
    
    elapsed = get_elapsed()
    speed = get_speed()
    
    await event.reply(
        f"⏹ **Поиск остановлен!**\n\n"
        f"🔍 Проверено: {checked_count}\n"
        f"✅ Активировано: {total_activated}\n"
        f"⏱ Время: {elapsed:.1f} сек\n"
        f"🚀 Скорость: {speed:.2f} ссылок/сек"
    )

@bot_client.on(events.NewMessage(pattern='/status'))
async def show_status(event):
    if not is_searching:
        await event.reply("⚠️ Поиск не запущен")
        return
    
    elapsed = get_elapsed()
    speed = get_speed()
    
    await event.reply(
        f"📊 **Статистика:**\n\n"
        f"🔄 Статус: {'🟢 Активен' if is_searching else '🔴 Остановлен'}\n"
        f"🔍 Проверено: {checked_count}\n"
        f"✅ Активировано: {total_activated}\n"
        f"⚡ Скорость: {speed:.2f} ссылок/сек\n"
        f"⏱ Время: {elapsed:.1f} сек\n"
        f"🛡️ Flood защита: {'🟢 OK' if not flood_wait_active else '🔴 Ожидание'}"
    )

@bot_client.on(events.NewMessage(pattern='/found'))
async def show_found_links(event):
    if not activated_links:
        await event.reply("❌ Нет активированных ссылок")
        return
    
    last_links = activated_links[-10:]
    text = f"✅ **Активировано: {len(activated_links)}**\n\n"
    for i, item in enumerate(last_links, 1):
        start_param = extract_start_param(item['link'])
        text += f"#{i} `/start {start_param}`\n"
        text += f"   ⏱ {item['time']}\n\n"
    
    await event.reply(text)

@bot_client.on(events.NewMessage(pattern='/clear'))
async def clear_found(event):
    global activated_links, total_activated, processed_links
    count = len(activated_links)
    activated_links = []
    total_activated = 0
    processed_links = set()
    await event.reply(f"🧹 Очищено {count} ссылок")

@bot_client.on(events.NewMessage(pattern='/generate'))
async def generate_links(event):
    parts = event.message.text.split()
    count = min(int(parts[1]) if len(parts) > 1 else 10, 20)
    
    links = [generate_cryptobot_link() for _ in range(count)]
    text = f"🔗 **{count} ссылок:**\n\n"
    for i, link in enumerate(links, 1):
        start_param = extract_start_param(link)
        text += f"{i}. `/start {start_param}`\n"
    
    await event.reply(text)

@bot_client.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    await event.reply(
        f"🚀 **Бот-активатор ссылок**\n\n"
        f"📌 **Авторизация:**\n"
        f"/setphone +71234567890 - Ввести номер\n"
        f"/setcode 12345 - Ввести код\n"
        f"/setpassword пароль - 2FA\n\n"
        f"📌 **Команды:**\n"
        f"/search - Запустить поиск\n"
        f"/stop - Остановить\n"
        f"/status - Статистика\n"
        f"/found - Активированные\n"
        f"/generate - Сгенерировать ссылки\n"
        f"/clear - Очистить\n\n"
        f"⚙️ **Безопасный режим:**\n"
        f"⏱ Задержка: {CHECK_DELAY} сек\n"
        f"🛡️ Flood защита включена"
    )

# ===== ЗАПУСК =====
async def main():
    try:
        await bot_client.start(bot_token=BOT_TOKEN)
        
        print("🚀 БОТ ЗАПУЩЕН!")
        print("⚙️ БЕЗОПАСНЫЙ РЕЖИМ (без flood)")
        print(f"⏱ Задержка: {CHECK_DELAY} сек")
        print("💡 /search - запустить поиск")
        print("✅ Готов к работе!")
        
        await bot_client.run_until_disconnected()
        
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
    finally:
        loop.close()