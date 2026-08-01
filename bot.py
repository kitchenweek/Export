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

# Настройки скорости
MAX_CONCURRENT_CHECKS = 5
BATCH_SIZE = 20
MIN_DELAY = 0.2

# ===== ИНИЦИАЛИЗАЦИЯ КЛИЕНТА =====
client = TelegramClient(
    'user_session',
    API_ID,
    API_HASH,
    connection_retries=3,
    retry_delay=1,
    auto_reconnect=True,
    flood_sleep_threshold=30
)

# ===== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =====
SEND_BOT_USERNAME = '@send'
is_searching = False
search_task = None
found_links = []
checked_count = 0
start_time = None
total_found = 0
error_count = 0
link_history = []

# Семафор для контроля параллельных запросов
rate_limiter = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

# ===== ФУНКЦИЯ ГЕНЕРАЦИИ ССЫЛОК С ПРЕФИКСОМ CQ + 10 СИМВОЛОВ =====
CHARS = string.ascii_letters + string.digits

def generate_cryptobot_link():
    """Генерирует ссылку с префиксом CQ и 10 случайными символами"""
    prefix = "CQ"
    random_part = ''.join(random.choices(CHARS, k=10))  # 10 символов
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

# ===== ОСНОВНЫЕ ФУНКЦИИ =====
async def check_link_fast(link):
    """Быстрая проверка ссылки через @send"""
    try:
        async with rate_limiter:
            # Отправляем ссылку
            await client.send_message(SEND_BOT_USERNAME, link)
            
            # Минимальная задержка для получения ответа
            await asyncio.sleep(0.3)
            
            # Получаем последний ответ
            async for msg in client.iter_messages(SEND_BOT_USERNAME, limit=1):
                if msg.text and msg.text != link and len(msg.text) > 5:
                    text_lower = msg.text.lower()
                    # Проверка на ошибки
                    error_keywords = ['error', 'invalid', 'не найден', 'не существует', 'ошибка']
                    if any(keyword in text_lower for keyword in error_keywords):
                        return False, msg.text
                    else:
                        return True, msg.text
                        
    except Exception as e:
        logger.error(f"Ошибка проверки {link}: {e}")
        return False, None
    
    return False, None

async def batch_check_links(links):
    """Параллельная проверка пакета ссылок"""
    tasks = [check_link_fast(link) for link in links]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results

async def search_worker():
    """Основной рабочий процесс поиска"""
    global is_searching, checked_count, found_links, start_time, total_found, error_count, link_history
    
    checked_count = 0
    found_links = []
    link_history = []
    start_time = time.time()
    batch = []
    found_in_batch = []
    
    logger.info(f"🚀 Поиск запущен! Генерация ссылок с префиксом CQ + 10 символов")
    logger.info(f"⚡ Параллельных проверок: {MAX_CONCURRENT_CHECKS}")
    logger.info(f"📦 Размер пакета: {BATCH_SIZE}")
    
    while is_searching:
        try:
            # Генерируем пакет ссылок с префиксом CQ + 10 символов
            batch = [generate_cryptobot_link() for _ in range(BATCH_SIZE)]
            link_history.extend(batch)
            
            # Проверяем пакет
            results = await batch_check_links(batch)
            
            # Обрабатываем результаты
            for link, result in zip(batch, results):
                checked_count += 1
                
                if isinstance(result, tuple) and result[0]:
                    # Найдена рабочая ссылка!
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
                    
                    # Уведомление о находке
                    logger.info(f"🎯 НАЙДЕНА РАБОЧАЯ ССЫЛКА #{total_found}!")
                    logger.info(f"🔗 {link}")
                    logger.info(f"📊 Проверено: {checked_count} | Найдено: {total_found}")
                    
                    # Отправляем в Telegram
                    try:
                        await client.send_message(
                            'me',
                            f"🎯 **РАБОЧАЯ ССЫЛКА #{total_found}!**\n\n"
                            f"🔗 `{link}`\n\n"
                            f"📊 Проверено: {checked_count}\n"
                            f"✅ Найдено: {total_found}\n"
                            f"⚡ Скорость: {get_speed():.1f} ссылок/сек"
                        )
                    except Exception as e:
                        logger.error(f"Ошибка отправки уведомления: {e}")
            
            # Если найдены ссылки в этом пакете, показываем статистику
            if found_in_batch:
                logger.info(f"✅ Найдено {len(found_in_batch)} ссылок в пакете!")
                found_in_batch = []
            
            # Обновляем статус каждые 100 проверок
            if checked_count % 100 == 0:
                speed = get_speed()
                logger.info(
                    f"📊 Статус: {checked_count} проверок | "
                    f"{total_found} найдено | "
                    f"{speed:.1f} ссылок/сек"
                )
            
            # Маленькая задержка между пакетами
            await asyncio.sleep(MIN_DELAY)
            
        except Exception as e:
            error_count += 1
            logger.error(f"❌ Ошибка в поиске: {e}")
            await asyncio.sleep(0.5)

# ===== КОМАНДА ДЛЯ ГЕНЕРАЦИИ ССЫЛОК =====
@client.on(events.NewMessage(pattern='/generate'))
async def generate_links(event):
    """Генерирует и показывает ссылки с префиксом CQ + 10 символов"""
    parts = event.message.text.split()
    count = 10
    if len(parts) > 1:
        try:
            count = min(int(parts[1]), 50)
        except:
            count = 10
    
    # Генерируем ссылки
    links = []
    for _ in range(count):
        links.append(generate_cryptobot_link())
    
    # Формируем ответ
    response = f"🔗 **Сгенерировано {count} ссылок (CQ + 10 символов):**\n\n"
    for i, link in enumerate(links, 1):
        # Показываем длину после CQ
        after_cq = link.split('start=CQ')[1] if 'start=CQ' in link else ''
        response += f"{i}. `{link}`\n"
        response += f"   📝 После CQ: `{after_cq}` (10 символов)\n\n"
    
    response += f"💡 Используйте /search для начала проверки"
    
    await event.reply(response)

# ===== КОМАНДА ДЛЯ ПРОВЕРКИ КОНКРЕТНОЙ ССЫЛКИ =====
@client.on(events.NewMessage(pattern='/check'))
async def check_specific_link(event):
    """Проверяет конкретную ссылку"""
    parts = event.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await event.reply("❌ Укажите ссылку: /check http://t.me/CryptoBot?start=CQ...")
        return
    
    link = parts[1].strip()
    
    # Проверяем формат
    if not link.startswith('http://t.me/CryptoBot?start=CQ'):
        await event.reply("❌ Ссылка должна начинаться с: http://t.me/CryptoBot?start=CQ")
        return
    
    # Проверяем длину после CQ
    after_cq = link.split('start=CQ')[1] if 'start=CQ' in link else ''
    if len(after_cq) != 10:
        await event.reply(f"❌ После CQ должно быть 10 символов, а у вас {len(after_cq)}")
        return
    
    status_msg = await event.reply(f"🔍 Проверяю ссылку: `{link}`")
    
    result = await check_link_fast(link)
    
    if result[0]:
        await status_msg.edit(
            f"✅ **Ссылка рабочая!**\n\n"
            f"🔗 `{link}`\n\n"
            f"📊 Результат: {result[1]}\n"
            f"📝 После CQ: `{after_cq}` (10 символов)"
        )
    else:
        await status_msg.edit(
            f"❌ **Ссылка не работает**\n\n"
            f"🔗 `{link}`\n\n"
            f"📊 Результат: {result[1]}\n"
            f"📝 После CQ: `{after_cq}` (10 символов)"
        )

# ===== ОСТАЛЬНЫЕ КОМАНДЫ =====
@client.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    await event.reply(
        f"🚀 **Ultra Speed Bot - CryptoBot Checker**\n\n"
        f"📌 **Команды:**\n"
        f"/start - Показать это сообщение\n"
        f"/generate [количество] - Сгенерировать CQ + 10 символов ссылки\n"
        f"/check <ссылка> - Проверить конкретную ссылку\n"
        f"/search - Запустить поиск (МАКСИМАЛЬНАЯ СКОРОСТЬ)\n"
        f"/stop - Остановить поиск\n"
        f"/status - Статистика\n"
        f"/found - Показать найденные ссылки\n"
        f"/clear - Очистить найденные ссылки\n\n"
        f"⚡ Параллельных проверок: {MAX_CONCURRENT_CHECKS}\n"
        f"📦 Размер пакета: {BATCH_SIZE}\n"
        f"🔗 Формат: CQ + 10 символов\n"
        f"📝 Пример: `http://t.me/CryptoBot?start=CQxK7mP9rT2`"
    )

@client.on(events.NewMessage(pattern='/search'))
async def start_search(event):
    global is_searching, search_task, start_time
    
    if is_searching:
        await event.reply("⚠️ Поиск уже запущен! Используйте /stop для остановки.")
        return
    
    is_searching = True
    start_time = time.time()
    
    await event.reply(
        f"🚀 **Поиск запущен!**\n\n"
        f"🔗 Генерация ссылок: CQ + 10 символов\n"
        f"⚡ Скорость: МАКСИМАЛЬНАЯ\n"
        f"🔄 Параллельных потоков: {MAX_CONCURRENT_CHECKS}\n"
        f"📦 Размер пакета: {BATCH_SIZE}\n\n"
        f"📊 Для просмотра статистики используйте /status\n"
        f"📌 Найденные ссылки будут отправлены в 'Сохраненные сообщения'"
    )
    
    search_task = asyncio.create_task(search_worker())

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
        f"🚀 Скорость: {speed:.1f} ссылок/сек\n"
        f"❌ Ошибок: {error_count}"
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
        f"❌ Ошибок: {error_count}\n"
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
        f"📌 Последние 5:\n{links_text}\n\n"
        f"💾 Все ссылки сохранены в памяти бота"
    )

@client.on(events.NewMessage(pattern='/clear'))
async def clear_found(event):
    global found_links, total_found
    count = len(found_links)
    found_links = []
    total_found = 0
    await event.reply(f"🧹 Очищено {count} найденных ссылок.")

# ===== ЗАПУСК =====
async def main():
    try:
        print("🚀 ULTRA SPEED BOT WITH CQ + 10 CHARACTERS!")
        print("🔗 Генерация ссылок: CQ + 10 случайных символов")
        print("📌 Команды доступны в боте")
        print("💡 Используйте /generate для просмотра ссылок")
        print("💡 Используйте /search для запуска поиска")
        
        await client.start(bot_token=BOT_TOKEN)
        print("✅ Бот успешно запущен!")
        
        await client.run_until_disconnected()
        
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем")
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