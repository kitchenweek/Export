from telethon import TelegramClient, events
import asyncio
import random
import string
import time
from datetime import datetime
import logging
from colorama import init, Fore, Style

# Инициализация colorama
init(autoreset=True)

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
MAX_CONCURRENT_CHECKS = 15  # Параллельных проверок
BATCH_SIZE = 50  # Размер пакета
CHECK_TIMEOUT = 1.5  # Таймаут проверки
MIN_DELAY = 0.05  # Минимальная задержка между пакетами

# ===== ИНИЦИАЛИЗАЦИЯ КЛИЕНТА =====
client = TelegramClient(
    'ultra_bot_session',
    API_ID,
    API_HASH,
    connection_retries=5,
    retry_delay=1,
    auto_reconnect=True,
    flood_sleep_threshold=60
).start(bot_token=BOT_TOKEN)

# ===== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =====
SEND_BOT_USERNAME = '@send'
is_searching = False
search_task = None
found_links = []
checked_count = 0
start_time = None
total_found = 0
error_count = 0

# Семафор для контроля параллельных запросов
rate_limiter = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

# Предварительная генерация символов для скорости
CHARS = string.ascii_letters + string.digits
LINK_TEMPLATE = "http://t.me/CryptoBot?start={}"

# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====
def generate_cryptobot_link():
    """Максимально быстрая генерация ссылки"""
    return LINK_TEMPLATE.format(''.join(random.choices(CHARS, k=14)))

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
    global is_searching, checked_count, found_links, start_time, total_found, error_count
    
    checked_count = 0
    found_links = []
    start_time = time.time()
    batch = []
    last_found_time = time.time()
    found_in_batch = []
    
    logger.info(f"{Fore.GREEN}🚀 Поиск запущен! Скорость: МАКСИМАЛЬНАЯ")
    logger.info(f"{Fore.CYAN}⚡ Параллельных проверок: {MAX_CONCURRENT_CHECKS}")
    logger.info(f"{Fore.CYAN}📦 Размер пакета: {BATCH_SIZE}")
    
    while is_searching:
        try:
            # Генерируем пакет ссылок
            batch = [generate_cryptobot_link() for _ in range(BATCH_SIZE)]
            
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
                    notification = (
                        f"{Fore.GREEN}🎯 НАЙДЕНА РАБОЧАЯ ССЫЛКА #{total_found}!\n"
                        f"{Fore.CYAN}🔗 {link}\n"
                        f"{Fore.YELLOW}📊 Проверено: {checked_count} | Найдено: {total_found}\n"
                        f"{Fore.BLUE}⚡ Скорость: {get_speed():.1f} ссылок/сек"
                    )
                    logger.info(notification)
                    
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
                logger.info(f"{Fore.GREEN}✅ Найдено {len(found_in_batch)} ссылок в пакете!")
                found_in_batch = []
            
            # Обновляем статус каждые 100 проверок
            if checked_count % 100 == 0:
                speed = get_speed()
                logger.info(
                    f"{Fore.CYAN}📊 Статус: {checked_count} проверок | "
                    f"{Fore.GREEN}{total_found} найдено | "
                    f"{Fore.YELLOW}{speed:.1f} ссылок/сек"
                )
            
            # Маленькая задержка между пакетами
            await asyncio.sleep(MIN_DELAY)
            
        except Exception as e:
            error_count += 1
            logger.error(f"{Fore.RED}❌ Ошибка в поиске: {e}")
            await asyncio.sleep(0.5)

# ===== ОБРАБОТЧИКИ КОМАНД =====
@client.on(events.NewMessage(pattern='/start'))
async def start_command(event):
    await event.reply(
        f"{Fore.CYAN}🚀 **Ultra Speed Bot - CryptoBot Checker**\n\n"
        f"{Fore.GREEN}📌 **Команды:**\n"
        f"/start - Показать это сообщение\n"
        f"/search - Запустить поиск (МАКСИМАЛЬНАЯ СКОРОСТЬ)\n"
        f"/stop - Остановить поиск\n"
        f"/status - Статистика\n"
        f"/found - Показать найденные ссылки\n"
        f"/clear - Очистить найденные ссылки\n"
        f"/restart - Перезапустить бота\n\n"
        f"{Fore.YELLOW}⚡ Параллельных проверок: {MAX_CONCURRENT_CHECKS}\n"
        f"📦 Размер пакета: {BATCH_SIZE}"
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
        f"⚡ Скорость: МАКСИМАЛЬНАЯ\n"
        f"🔄 Параллельных потоков: {MAX_CONCURRENT_CHECKS}\n"
        f"📦 Размер пакета: {BATCH_SIZE}\n\n"
        f"📊 Для просмотра статистики используйте /status\n"
        f"📌 Найденные ссылки будут отправлены в 'Сохраненные сообщения'"
    )
    
    # Запускаем поиск в фоне
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
    
    # Показываем последние 5 ссылок
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

@client.on(events.NewMessage(pattern='/restart'))
async def restart_bot(event):
    await event.reply("🔄 Перезапуск бота...")
    logger.info("Перезапуск бота...")
    await client.disconnect()
    await asyncio.sleep(1)
    await client.start()
    logger.info("✅ Бот перезапущен!")

# ===== ЗАПУСК =====
async def main():
    try:
        print(f"{Fore.GREEN}🚀 ULTRA SPEED BOT STARTED!")
        print(f"{Fore.CYAN}⚡ Concurrent checks: {MAX_CONCURRENT_CHECKS}")
        print(f"{Fore.CYAN}📦 Batch size: {BATCH_SIZE}")
        print(f"{Fore.YELLOW}💡 Используйте /search для запуска поиска")
        print(f"{Fore.MAGENTA}📌 Бот @{BOT_TOKEN.split(':')[0]} запущен!")
        
        await client.start()
        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"{Fore.RED}❌ Критическая ошибка: {e}")
    finally:
        await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}👋 Бот остановлен пользователем")