# -*- coding: utf-8 -*-
"""
Telegram Trigger Scanner
========================
Aiogram + Telethon.

Что делает:
- Telegram-бот запускается через aiogram.
- Через Telethon подключает пользовательский Telegram-аккаунт.
- Показывает список диалогов.
- Полностью сканирует выбранный чат.
- Ищет встроенные триггеры.
- Для каждого совпадения сохраняет 20 сообщений выше и 20 ниже.
- Пересекающиеся фрагменты объединяются.
- Возвращает итоговый TXT-файл в Telegram.

ВАЖНО:
Совпадение с триггером не означает нарушение закона или виновность.
Используйте только для переписок, к которым у вас есть законный доступ.
"""

import asyncio
import html
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

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
    Message,
)
from telethon import TelegramClient, utils
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

# Замени на свои значения.
BOT_TOKEN = "8547309036:AAHlLo7U0GU-SyesPBXS2PAWGD1Gw6bGYXg"
API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"

# Твой числовой Telegram ID.
# Если оставить 0, бот при первом /start в рамках текущего запуска
# запомнит первого пользователя как владельца.
ADMIN_ID = 0

CONTEXT_MESSAGES = 20
DIALOGS_PER_PAGE = 12

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(exist_ok=True)

SESSION_PATH = BASE_DIR / "telethon_scanner_session"

router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH)
scan_lock = asyncio.Lock()

runtime_admin_id = ADMIN_ID
dialog_cache: Dict[int, object] = {}


# ============================================================
# УРОВНИ РИСКА
# ============================================================

RISK_ORDER = {
    "КОНТЕКСТНЫЙ": 1,
    "СРЕДНИЙ": 2,
    "ВЫСОКИЙ": 3,
    "КРИТИЧЕСКИЙ": 4,
}

CATEGORY_RISK = {
    "Терроризм и экстремистское насилие": "КРИТИЧЕСКИЙ",
    "Взрывы и поджоги": "КРИТИЧЕСКИЙ",
    "Убийство и тяжёлое насилие": "КРИТИЧЕСКИЙ",
    "Оружие": "ВЫСОКИЙ",
    "Вооружённый конфликт": "КОНТЕКСТНЫЙ",
    "Шпионаж / государственная безопасность": "ВЫСОКИЙ",
    "Мошенничество": "СРЕДНИЙ",
    "Банковское мошенничество": "ВЫСОКИЙ",
    "Отмывание денег": "ВЫСОКИЙ",
    "Криптовалюта в риск-контексте": "КОНТЕКСТНЫЙ",
    "Фишинг": "ВЫСОКИЙ",
    "Кража аккаунтов": "ВЫСОКИЙ",
    "Вредоносное ПО и киберпреступления": "ВЫСОКИЙ",
    "Персональные данные / незаконный доступ": "СРЕДНИЙ",
    "Поддельные документы": "ВЫСОКИЙ",
    "Взятки и коррупция": "ВЫСОКИЙ",
    "Наркотики": "ВЫСОКИЙ",
    "Незаконный оборот лекарств": "ВЫСОКИЙ",
    "Похищение и вымогательство": "КРИТИЧЕСКИЙ",
    "Кража / грабёж / разбой": "ВЫСОКИЙ",
    "Незаконное проникновение": "ВЫСОКИЙ",
    "Подделка денег": "ВЫСОКИЙ",
    "Налоговые преступления": "СРЕДНИЙ",
    "Контрабанда": "СРЕДНИЙ",
    "Незаконная миграция": "СРЕДНИЙ",
    "Незаконные азартные игры": "СРЕДНИЙ",
    "Договорные спортивные события": "СРЕДНИЙ",
    "Подкуп свидетелей / вмешательство в правосудие": "ВЫСОКИЙ",
    "Уничтожение и сокрытие доказательств": "ВЫСОКИЙ",
    "Незаконная слежка": "ВЫСОКИЙ",
    "Подделки товаров": "СРЕДНИЙ",
    "Организованная преступность": "ВЫСОКИЙ",
    "Призывы к насилию": "КРИТИЧЕСКИЙ",
    "Ненависть и преследование групп": "ВЫСОКИЙ",
    "Незаконный оборот специальных технических средств": "ВЫСОКИЙ",
    "Незаконные действия с автомобилями": "СРЕДНИЙ",
    "Страховое мошенничество": "СРЕДНИЙ",
    "Финансовые пирамиды": "СРЕДНИЙ",
    "Незаконный оборот драгоценных материалов": "СРЕДНИЙ",
    "Браконьерство": "СРЕДНИЙ",
    "Вандализм и повреждение имущества": "СРЕДНИЙ",
    "Железная дорога / транспорт / инфраструктура": "КРИТИЧЕСКИЙ",
    "Государственные и силовые структуры — контекстные триггеры": "КОНТЕКСТНЫЙ",
    "Следствие / уголовный процесс": "КОНТЕКСТНЫЙ",
    "Побег / уклонение от задержания": "ВЫСОКИЙ",
    "Детская сексуальная эксплуатация": "КРИТИЧЕСКИЙ",
    "Торговля людьми / эксплуатация": "КРИТИЧЕСКИЙ",
    "Компромат / интимные материалы": "ВЫСОКИЙ",
    "Общие высокорисковые сочетания": "СРЕДНИЙ",
    "Англоязычные маркеры": "СРЕДНИЙ",
}


# ============================================================
# ПОЛНЫЙ ВСТРОЕННЫЙ СЛОВАРЬ ТРИГГЕРОВ
# ============================================================

TRIGGER_CATEGORIES = {
    "Терроризм и экстремистское насилие": [
        "теракт", "терроризм", "террорист", "террористический", "экстремизм", "экстремист",
        "радикализация", "боевик", "ячейка", "подполье", "вербовка", "завербовать", "смертник",
        "диверсия", "диверсант", "диверсионная группа", "захват заложников", "заложник",
        "угон самолёта", "нападение", "вооружённое нападение", "массовое нападение",
        "подготовка нападения", "план нападения", "объект атаки", "цель атаки", "организовать атаку",
        "финансирование терроризма", "финансирование экстремизма", "пособничество", "вербовщик",
        "пропаганда терроризма", "оправдание терроризма", "призыв к терроризму",
    ],
    "Взрывы и поджоги": [
        "взрыв", "взорвать", "взрывчатка", "взрывное устройство", "СВУ", "бомба", "бомбить",
        "заминировать", "минирование", "мина", "детонатор", "детонация", "взрыватель", "подрыв",
        "подорвать", "подрыв здания", "подрыв машины", "поджог", "поджечь", "коктейль Молотова",
        "горючая смесь", "объект для поджога", "устроить пожар",
    ],
    "Убийство и тяжёлое насилие": [
        "убить", "убийство", "киллер", "заказное убийство", "заказать человека", "устранить человека",
        "ликвидировать человека", "расправа", "зарезать", "задушить", "застрелить", "расстрелять",
        "избить", "пытать", "пытка", "похитить человека", "похищение", "удерживать силой",
        "заложник", "вымогательство с угрозами", "угроза убийством", "угрожать расправой", "нападение",
    ],
    "Оружие": [
        "оружие", "огнестрельное оружие", "ствол", "огнестрел", "пистолет", "револьвер", "автомат",
        "автомат Калашникова", "АК", "АКМ", "АК-74", "винтовка", "карабин", "ружьё", "обрез",
        "пулемёт", "патрон", "патроны", "боеприпасы", "магазин", "глушитель", "граната",
        "холодное оружие", "кастет", "кинжал", "боевой нож", "нелегальное оружие", "купить оружие",
        "продать оружие", "достать оружие", "переделать оружие",
    ],
    "Вооружённый конфликт": [
        "СВО", "война", "военные действия", "боевые действия", "фронт", "линия фронта", "Украина",
        "ВСУ", "ВС РФ", "российская армия", "украинская армия", "НАТО", "мобилизация",
        "мобилизованный", "мобик", "повестка", "военкомат", "контрактник", "доброволец", "наёмник",
        "пленный", "военнопленный", "плен", "окоп", "позиция", "наступление", "контрнаступление",
        "отступление", "штурм", "штурмовик", "обстрел", "артобстрел", "бомбардировка", "ракета",
        "беспилотник", "БПЛА", "FPV", "военный объект", "военная часть", "координаты позиции",
        "расположение войск", "передвижение войск",
    ],
    "Шпионаж / государственная безопасность": [
        "шпионаж", "шпион", "разведка", "разведчик", "контрразведка", "агент", "иностранный агент",
        "спецслужба", "секретные сведения", "государственная тайна", "гостайна", "секретный документ",
        "секретные материалы", "засекречено", "совершенно секретно", "передать сведения",
        "передать документы", "военная тайна", "секретная информация", "координаты объекта",
        "сведения о военных", "закрытый объект", "режимный объект",
    ],
    "Мошенничество": [
        "скам", "scam", "скамер", "скамить", "заскамить", "мошенник", "мошенничество", "обман",
        "развести", "развод", "разводить клиента", "мамонт", "мамонты", "мамонтёнок", "воркер",
        "ворк", "профит", "профитнуть", "схема", "схемка", "темка", "тема", "связка", "отработка",
        "обработка", "прогрев", "лид", "лиды", "трафик", "залив", "заливщик", "вбив", "вбивер",
        "вбить данные", "мануал", "расходник", "расходники", "гарант", "рефанд", "возврат через обман",
        "фейковая оплата", "поддельный чек", "фейковый чек",
    ],
    "Банковское мошенничество": [
        "чужая карта", "карта дропа", "дроп-карта", "дроп", "дроппер", "дроповод", "банковский дроп",
        "оформить карту на человека", "принять деньги на карту", "транзит денег", "прогнать деньги",
        "обнал", "обналичка", "обналить", "вывести деньги", "чужой банковский счёт", "чужие реквизиты",
        "банковские реквизиты", "CVV", "CVC", "PIN", "пин-код", "код банка", "SMS-код",
        "код подтверждения", "код операции", "доступ к банку", "интернет-банк", "личный кабинет банка",
        "безопасный счёт", "сотрудник банка", "служба безопасности банка",
    ],
    "Отмывание денег": [
        "отмывание", "отмыв", "отмыть деньги", "легализация денег", "грязные деньги", "грязь",
        "чистые деньги", "очистить деньги", "обналичить", "обнал", "наличка", "транзит",
        "прокрутить деньги", "прогнать сумму", "разбить сумму", "дробление платежей", "подставной счёт",
        "номинал", "номинальный владелец", "фирма-прокладка", "фирма-однодневка", "фиктивная сделка",
        "фиктивный договор", "фиктивный платёж",
    ],
    "Криптовалюта в риск-контексте": [
        "крипта", "криптовалюта", "USDT", "BTC", "Bitcoin", "Ethereum", "ETH", "TRX", "Monero", "XMR",
        "криптокошелёк", "холодный кошелёк", "обменник", "криптообменник", "P2P", "Binance", "Bybit",
        "перевод в крипте", "получить USDT", "вывести USDT", "обменять наличку на USDT",
        "анонимный обмен", "крипта без KYC",
    ],
    "Фишинг": [
        "фишинг", "phishing", "фиш", "фишинговый сайт", "фейковый сайт", "поддельный сайт",
        "клон сайта", "копия сайта", "поддельная форма", "форма авторизации", "форма оплаты",
        "страница оплаты", "перехват логина", "перехват пароля", "перехват кода",
        "получить код подтверждения", "выманить код", "ссылка для оплаты", "поддельная ссылка",
        "фейковая ссылка", "редирект", "домен под банк",
    ],
    "Кража аккаунтов": [
        "угнать аккаунт", "угон аккаунта", "украсть аккаунт", "доступ к аккаунту", "чужой аккаунт",
        "логин", "пароль", "логин-пароль", "данные авторизации", "код авторизации", "код подтверждения",
        "SMS-код", "резервный код", "recovery code", "session", "session file", "сессия",
        "Telegram session", "tdata", "токен", "auth token", "cookie", "cookies", "украсть сессию",
        "перехватить сессию",
    ],
    "Вредоносное ПО и киберпреступления": [
        "вирус", "malware", "троян", "trojan", "стилер", "stealer", "инфостилер", "ransomware",
        "шифровальщик", "ботнет", "RAT", "удалённый доступ", "вредонос", "вредоносное ПО",
        "эксплойт", "exploit", "уязвимость", "взлом", "хакнуть", "хакер", "брут", "bruteforce",
        "brute force", "DDoS", "заражённый компьютер", "украсть пароли", "украсть cookies",
        "украсть данные",
    ],
    "Персональные данные / незаконный доступ": [
        "персональные данные", "личные данные", "паспортные данные", "паспорт", "скан паспорта",
        "фото паспорта", "селфи с паспортом", "СНИЛС", "ИНН", "номер карты", "номер счёта",
        "база клиентов", "база пользователей", "база номеров", "слив базы", "утечка базы",
        "купить базу", "продать базу", "пробив", "пробить человека", "пробив по номеру",
        "пробив по ФИО", "адрес человека", "найти владельца номера",
    ],
    "Поддельные документы": [
        "поддельный паспорт", "фальшивый паспорт", "купить паспорт", "поддельные права",
        "водительские права", "поддельное удостоверение", "поддельная справка", "поддельный диплом",
        "купить диплом", "поддельный сертификат", "поддельная печать", "поддельная подпись",
        "нарисовать документ", "сделать документы", "липовая справка", "липовый договор",
        "фиктивный документ", "фальсификация документов",
    ],
    "Взятки и коррупция": [
        "взятка", "дать взятку", "получить взятку", "занести деньги", "откат", "процент чиновнику",
        "решить вопрос за деньги", "договориться с инспектором", "договориться с полицейским",
        "купить решение", "купить должностное лицо", "коррупция", "подкуп", "коммерческий подкуп",
        "вознаграждение чиновнику",
    ],
    "Наркотики": [
        "наркотик", "наркотики", "наркота", "наркотическое средство", "психотроп",
        "психотропное вещество", "меф", "мефедрон", "амфетамин", "метамфетамин", "кокаин",
        "героин", "марихуана", "каннабис", "гашиш", "соли", "синтетика", "экстази", "MDMA",
        "ЛСД", "LSD", "закладка", "клад", "кладмен", "закладчик", "барыга", "дилер",
        "наркодилер", "фасовка", "партия наркотиков", "сбыт", "распространение наркотиков",
    ],
    "Незаконный оборот лекарств": [
        "рецепт", "поддельный рецепт", "рецептурный препарат", "сильнодействующее вещество",
        "сильнодействующий препарат", "психотропный препарат", "таблетки без рецепта",
        "продать таблетки", "купить таблетки без рецепта", "стероиды", "анаболики",
    ],
    "Похищение и вымогательство": [
        "похитить", "похищение человека", "украсть человека", "удерживать человека", "заложник",
        "заложники", "выкуп", "потребовать выкуп", "вымогательство", "вымогать деньги", "шантаж",
        "шантажировать", "компромат", "заплати или", "угрожать семье", "угрожать человеку",
    ],
    "Кража / грабёж / разбой": [
        "украсть", "кража", "воровать", "вор", "грабёж", "ограбить", "разбой", "вынести товар",
        "вскрыть магазин", "вскрыть квартиру", "украсть машину", "угнать машину", "угон",
        "краденое", "краденый товар", "скупка краденого", "продать краденое",
    ],
    "Незаконное проникновение": [
        "вскрыть дверь", "вскрыть замок", "отмычка", "проникнуть в квартиру", "проникнуть на объект",
        "обойти охрану", "отключить сигнализацию", "отключить камеру", "слепая зона камеры",
        "обойти пропуск", "поддельный пропуск",
    ],
    "Подделка денег": [
        "фальшивые деньги", "фальшивка", "поддельные деньги", "поддельная купюра",
        "фальшивая купюра", "фальшивые рубли", "поддельные доллары", "печатать деньги",
        "изготовление банкнот", "сбыт фальшивок",
    ],
    "Налоговые преступления": [
        "уклонение от налогов", "не платить налоги", "скрыть доход", "скрыть выручку",
        "фиктивные расходы", "фиктивный НДС", "бумажный НДС", "обнал через ИП", "номинальный ИП",
        "номинальный директор", "фирма-однодневка", "фиктивная компания", "дробление бизнеса",
        "скрытая выручка",
    ],
    "Контрабанда": [
        "контрабанда", "контрабас", "провезти через границу", "спрятать от таможни", "тайник",
        "незаконный ввоз", "незаконный вывоз", "запрещённый груз", "нелегальный груз",
        "провести через границу", "таможня", "декларация", "не декларировать",
    ],
    "Незаконная миграция": [
        "фиктивная регистрация", "купить регистрацию", "прописка за деньги", "фиктивная прописка",
        "нелегальный мигрант", "незаконное пересечение границы", "поддельная виза", "купить визу",
        "фиктивное приглашение", "фиктивный трудовой договор",
    ],
    "Незаконные азартные игры": [
        "подпольное казино", "нелегальное казино", "игровой автомат", "незаконные ставки",
        "букмекер без лицензии", "договорной матч", "договорняк", "подставная ставка",
        "подкрутить результат",
    ],
    "Договорные спортивные события": [
        "договорной матч", "договорняк", "купить матч", "продать матч", "слить матч",
        "фиксированный результат", "подкупить игрока", "подкупить судью", "гарантированный исход",
    ],
    "Подкуп свидетелей / вмешательство в правосудие": [
        "подкупить свидетеля", "заплатить свидетелю", "изменить показания", "забрать заявление",
        "заставить забрать заявление", "ложные показания", "соврать следователю",
        "уничтожить доказательства", "спрятать доказательства", "удалить доказательства",
        "алиби", "сделать алиби",
    ],
    "Уничтожение и сокрытие доказательств": [
        "удалить переписку", "удалить сообщения", "почистить чат", "очистить историю",
        "уничтожить документы", "уничтожить доказательства", "спрятать документы",
        "избавиться от доказательств", "стереть данные", "разбить телефон", "выбросить телефон",
        "удалить аккаунт",
    ],
    "Незаконная слежка": [
        "прослушка", "прослушивать телефон", "жучок", "скрытый микрофон", "скрытая камера",
        "spy camera", "перехват звонков", "перехват сообщений", "следить за человеком",
        "GPS-маячок", "скрытый трекер", "spyware", "шпионское ПО",
    ],
    "Подделки товаров": [
        "контрафакт", "подделка", "паль", "пальё", "реплика", "копия бренда", "фейковый бренд",
        "поддельная маркировка", "поддельный сертификат", "перебить серийник",
        "изменить серийный номер", "поддельный чек",
    ],
    "Организованная преступность": [
        "ОПГ", "организованная группа", "преступная группа", "банда", "бандит", "бригадир",
        "смотрящий", "общак", "криминальный авторитет", "крыша", "криминальная крыша",
        "доля в общак", "преступное сообщество",
    ],
    "Призывы к насилию": [
        "призыв к насилию", "убить всех", "расстрелять", "уничтожить людей", "расправиться",
        "устроить нападение", "устроить стрельбу", "массовая стрельба", "нападение на школу",
        "нападение на людей",
    ],
    "Ненависть и преследование групп": [
        "разжигание ненависти", "разжигание вражды", "ненависть по национальности",
        "ненависть по религии", "этническая ненависть", "расовая ненависть",
        "призыв к расправе", "изгнать народ", "уничтожить народ",
    ],
    "Незаконный оборот специальных технических средств": [
        "скрытая камера", "замаскированная камера", "скрытый микрофон", "устройство прослушки",
        "жучок", "GSM-жучок", "устройство перехвата", "специальное техническое средство",
        "средство негласного получения информации",
    ],
    "Незаконные действия с автомобилями": [
        "угон", "угнать машину", "перебить VIN", "перебитый VIN", "изменить VIN",
        "машина-двойник", "поддельный ПТС", "поддельный СТС", "краденая машина",
        "разобрать угнанную машину",
    ],
    "Страховое мошенничество": [
        "страховой случай", "подставное ДТП", "автоподстава", "инсценировать ДТП",
        "инсценировать аварию", "фиктивная авария", "получить страховку обманом",
        "поддельные повреждения",
    ],
    "Финансовые пирамиды": [
        "финансовая пирамида", "пирамида", "привлечь вкладчиков", "гарантированный доход",
        "гарантированный процент", "деньги новых участников", "реферальные выплаты",
        "инвестиции без риска",
    ],
    "Незаконный оборот драгоценных материалов": [
        "нелегальное золото", "незаконная добыча золота", "скупка золота", "слиток без документов",
        "необработанный алмаз", "нелегальные драгоценные камни",
    ],
    "Браконьерство": [
        "браконьерство", "браконьер", "незаконная охота", "охота без лицензии",
        "незаконный вылов", "краснокнижное животное", "красная книга", "незаконная добыча",
        "сеть для рыбы", "электроудочка",
    ],
    "Вандализм и повреждение имущества": [
        "вандализм", "разгромить", "разбить витрину", "разбить машину", "поджечь машину",
        "испортить имущество", "уничтожить имущество", "повредить имущество",
    ],
    "Железная дорога / транспорт / инфраструктура": [
        "повредить рельсы", "разобрать рельсы", "поджечь релейный шкаф", "релейный шкаф",
        "вывести из строя железную дорогу", "повредить инфраструктуру",
        "нарушить движение поездов", "вывести из строя связь",
        "повредить линию электропередачи",
    ],
    "Государственные и силовые структуры — контекстные триггеры": [
        "ФСБ", "МВД", "полиция", "Росгвардия", "прокуратура", "Следственный комитет", "СК",
        "суд", "следователь", "оперативник", "опер", "спецслужбы", "военкомат", "военная часть",
        "погранслужба", "таможня", "ФСИН", "колония", "СИЗО", "уголовное дело", "уголовка",
        "розыск", "задержание", "обыск", "допрос",
    ],
    "Следствие / уголовный процесс": [
        "уголовное дело", "уголовная статья", "статья УК", "подозреваемый", "обвиняемый",
        "соучастник", "пособник", "организатор", "исполнитель", "следователь", "дознаватель",
        "допрос", "обыск", "задержание", "арест", "СИЗО", "признание", "явка с повинной",
        "доказательства", "вещдок", "уголовный розыск", "федеральный розыск",
    ],
    "Побег / уклонение от задержания": [
        "розыск", "федеральный розыск", "скрываться", "уйти от полиции", "скрыться от следствия",
        "побег", "сбежать из-под стражи", "поддельная личность", "чужие документы",
    ],
    "Детская сексуальная эксплуатация": [
        "детская порнография", "несовершеннолетний интим", "интим с ребёнком",
        "сексуальная эксплуатация ребёнка", "интимные фото несовершеннолетнего",
        "распространение интимных материалов несовершеннолетнего",
    ],
    "Торговля людьми / эксплуатация": [
        "торговля людьми", "продать человека", "купить человека", "рабство",
        "удерживать документы", "отобрать паспорт", "принудительный труд", "заставить работать",
        "сексуальная эксплуатация", "перевозка людей для эксплуатации",
    ],
    "Компромат / интимные материалы": [
        "компромат", "шантаж фотографиями", "интимные фото", "слить интимки",
        "распространить интимные фото", "опубликовать без согласия", "заплати или выложу",
    ],
    "Общие высокорисковые сочетания": [
        "купить нелегально", "продать нелегально", "без документов", "без оформления",
        "без регистрации", "без лицензии", "без разрешения", "никто не узнает", "анонимно",
        "не говори никому", "удали переписку", "после удали", "не пиши здесь",
        "перейдём в другой чат", "секретно", "спрячь", "передай наличными", "через посредника",
        "на чужое имя", "оформить на другого человека", "подставное лицо", "за процент",
        "гарантирует безопасность", "решить вопрос",
    ],
    "Англоязычные маркеры": [
        "scam", "scammer", "fraud", "fraudulent", "phishing", "malware", "stealer",
        "infostealer", "ransomware", "exploit", "hacking", "hack", "hacked account",
        "stolen account", "stolen card", "carding", "carder", "cashout", "money laundering",
        "dirty money", "drop account", "mule account", "fake ID", "fake passport", "counterfeit",
        "weapon", "firearm", "ammunition", "explosive", "bomb", "detonator", "terrorist",
        "terrorism", "extremist", "kidnapping", "ransom", "blackmail", "extortion", "drugs",
        "narcotics", "cocaine", "heroin", "methamphetamine", "cannabis", "darknet", "botnet",
        "DDoS", "spyware", "stolen data", "leaked database", "credentials",
    ],
}


# ============================================================
# FSM
# ============================================================

class LoginState(StatesGroup):
    phone = State()
    code = State()
    password = State()


# ============================================================
# ДОСТУП
# ============================================================

def is_admin(user_id: int) -> bool:
    global runtime_admin_id

    if runtime_admin_id == 0:
        runtime_admin_id = user_id
        print(f"[OWNER] Первый пользователь этого запуска назначен владельцем: {user_id}")

    return user_id == runtime_admin_id


# ============================================================
# НОРМАЛИЗАЦИЯ И ИНДЕКС ТРИГГЕРОВ
# ============================================================

CYR_LAT_TRANSLATION = str.maketrans({
    "ё": "е",
    "a": "а",
    "c": "с",
    "e": "е",
    "o": "о",
    "p": "р",
    "x": "х",
    "y": "у",
    "k": "к",
    "m": "м",
    "t": "т",
})


def normalize_text(text: str) -> str:
    text = (text or "").casefold().replace("ё", "е")
    # Только простая унификация похожих символов.
    text = text.translate(CYR_LAT_TRANSLATION)
    text = re.sub(r"[^0-9a-zа-я]+", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def compact_text(text: str) -> str:
    return normalize_text(text).replace(" ", "")


TRIGGER_INDEX = []
_seen = set()

for category, words in TRIGGER_CATEGORIES.items():
    for raw in words:
        normalized = normalize_text(raw)
        compacted = compact_text(raw)

        key = (category, normalized)
        if not normalized or key in _seen:
            continue
        _seen.add(key)

        TRIGGER_INDEX.append({
            "raw": raw,
            "normalized": normalized,
            "compact": compacted,
            "category": category,
            "risk": CATEGORY_RISK.get(category, "СРЕДНИЙ"),
        })


def find_trigger_hits(text: str) -> List[dict]:
    if not text:
        return []

    normalized = normalize_text(text)
    compacted = compact_text(text)

    hits = []
    seen_hits = set()

    for trigger in TRIGGER_INDEX:
        needle = trigger["normalized"]
        found = False

        # Для коротких одиночных слов используем границы слова,
        # чтобы "ак" не совпадал внутри длинного слова.
        if " " not in needle and len(needle) <= 4:
            pattern = rf"(?<![0-9a-zа-я]){re.escape(needle)}(?![0-9a-zа-я])"
            if re.search(pattern, normalized):
                found = True
        else:
            if needle in normalized:
                found = True

        # Дополнительно ловим разнесённое пробелами слово:
        # "м а м о н т" -> "мамонт".
        if not found and len(trigger["compact"]) >= 5:
            if trigger["compact"] in compacted:
                found = True

        if found:
            key = (trigger["category"], trigger["raw"].casefold())
            if key not in seen_hits:
                seen_hits.add(key)
                hits.append(trigger)

    hits.sort(
        key=lambda item: (
            -RISK_ORDER.get(item["risk"], 0),
            item["category"].casefold(),
            item["raw"].casefold(),
        )
    )
    return hits


def highest_risk(hits: List[dict]) -> str:
    if not hits:
        return "КОНТЕКСТНЫЙ"
    return max(hits, key=lambda x: RISK_ORDER.get(x["risk"], 0))["risk"]


# ============================================================
# UI
# ============================================================

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Подключить аккаунт", callback_data="connect")],
            [InlineKeyboardButton(text="🔎 Выбрать чат и сканировать", callback_data="dialogs:0")],
            [InlineKeyboardButton(text="👤 Статус аккаунта", callback_data="status")],
            [InlineKeyboardButton(text="📚 Статистика словаря", callback_data="dictionary")],
            [InlineKeyboardButton(text="🚪 Отключить аккаунт", callback_data="logout")],
        ]
    )


async def ensure_authorized() -> bool:
    if not client.is_connected():
        await client.connect()
    return await client.is_user_authorized()


async def entity_name(entity) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title

    first = getattr(entity, "first_name", "") or ""
    last = getattr(entity, "last_name", "") or ""
    username = getattr(entity, "username", None)

    name = " ".join(x for x in (first, last) if x).strip()
    if username:
        name = f"{name} (@{username})".strip()

    return name or str(getattr(entity, "id", "Unknown"))


async def sender_name(message) -> str:
    try:
        sender = await message.get_sender()
    except Exception:
        sender = None

    if sender is None:
        return "Unknown"

    title = getattr(sender, "title", None)
    if title:
        return title

    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    username = getattr(sender, "username", None)

    name = " ".join(x for x in (first, last) if x).strip()
    if username:
        name = f"{name} (@{username})".strip()

    return name or str(getattr(sender, "id", "Unknown"))


async def build_dialog_keyboard(page: int) -> Tuple[InlineKeyboardMarkup, int]:
    dialog_cache.clear()
    dialogs = []

    async for dialog in client.iter_dialogs():
        dialogs.append(dialog)

    total = len(dialogs)
    pages = max(1, (total + DIALOGS_PER_PAGE - 1) // DIALOGS_PER_PAGE)
    page = max(0, min(page, pages - 1))

    start = page * DIALOGS_PER_PAGE
    chunk = dialogs[start:start + DIALOGS_PER_PAGE]

    rows = []

    for dialog in chunk:
        entity = dialog.entity
        peer_id = utils.get_peer_id(entity)
        dialog_cache[peer_id] = entity

        name = dialog.name or await entity_name(entity)
        if len(name) > 42:
            name = name[:39] + "..."

        rows.append([
            InlineKeyboardButton(
                text=f"💬 {name}",
                callback_data=f"scan:{peer_id}",
            )
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"dialogs:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"dialogs:{page + 1}"))

    rows.append(nav)
    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu")])

    return InlineKeyboardMarkup(inline_keyboard=rows), total


async def resolve_dialog(peer_id: int):
    if peer_id in dialog_cache:
        return dialog_cache[peer_id]

    async for dialog in client.iter_dialogs():
        current_peer_id = utils.get_peer_id(dialog.entity)
        if current_peer_id == peer_id:
            dialog_cache[peer_id] = dialog.entity
            return dialog.entity

    raise RuntimeError("Диалог не найден")


# ============================================================
# СКАНИРОВАНИЕ
# ============================================================

def merge_windows(hit_indexes: List[int], total: int) -> List[Tuple[int, int]]:
    if not hit_indexes:
        return []

    windows: List[List[int]] = []

    for index in hit_indexes:
        start = max(0, index - CONTEXT_MESSAGES)
        end = min(total - 1, index + CONTEXT_MESSAGES)

        if not windows or start > windows[-1][1] + 1:
            windows.append([start, end])
        else:
            windows[-1][1] = max(windows[-1][1], end)

    return [(start, end) for start, end in windows]


async def scan_dialog(entity, status_message: Message):
    title = await entity_name(entity)

    messages = []
    loaded = 0

    async for msg in client.iter_messages(entity, reverse=True):
        messages.append(msg)
        loaded += 1

        if loaded % 5000 == 0:
            try:
                await status_message.edit_text(
                    f"⏳ Загружено сообщений: {loaded:,}\n"
                    f"💬 {html.escape(title)}"
                )
            except Exception:
                pass

    hit_map: Dict[int, List[dict]] = {}
    category_counts = defaultdict(int)
    risk_counts = defaultdict(int)

    for index, msg in enumerate(messages):
        text = getattr(msg, "message", "") or ""
        hits = find_trigger_hits(text)

        if hits:
            hit_map[index] = hits

            categories_in_message = {hit["category"] for hit in hits}
            for category in categories_in_message:
                category_counts[category] += 1

            risk_counts[highest_risk(hits)] += 1

    hit_indexes = sorted(hit_map)
    windows = merge_windows(hit_indexes, len(messages))

    safe_title = re.sub(r"[^0-9A-Za-zА-Яа-я._-]+", "_", title).strip("_")[:60] or "chat"
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    report_path = REPORTS_DIR / f"scan_{safe_title}_{timestamp}.txt"

    with report_path.open("w", encoding="utf-8") as report:
        report.write("TELEGRAM TRIGGER SCAN\n")
        report.write("=" * 100 + "\n")
        report.write(f"Чат: {title}\n")
        report.write(f"Дата отчёта: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n")
        report.write(f"Всего сообщений в истории: {len(messages):,}\n")
        report.write(f"Сообщений с триггерами: {len(hit_indexes):,}\n")
        report.write(f"Объединённых фрагментов: {len(windows):,}\n")
        report.write(f"Контекст: {CONTEXT_MESSAGES} сообщений ДО + совпадение + {CONTEXT_MESSAGES} ПОСЛЕ\n")
        report.write(f"Триггеров в словаре: {len(TRIGGER_INDEX):,}\n")
        report.write("\n")
        report.write("ВАЖНО: совпадение с триггером само по себе не означает нарушение закона.\n")
        report.write("Файл предназначен для ручной проверки контекста.\n")
        report.write("=" * 100 + "\n\n")

        report.write("СТАТИСТИКА ПО УРОВНЯМ РИСКА\n")
        report.write("-" * 100 + "\n")
        for risk in ("КРИТИЧЕСКИЙ", "ВЫСОКИЙ", "СРЕДНИЙ", "КОНТЕКСТНЫЙ"):
            report.write(f"{risk}: {risk_counts.get(risk, 0):,}\n")

        report.write("\nСТАТИСТИКА ПО КАТЕГОРИЯМ\n")
        report.write("-" * 100 + "\n")
        for category, count in sorted(category_counts.items(), key=lambda x: (-x[1], x[0].casefold())):
            report.write(f"{count:>7} | {category}\n")

        report.write("\n" + "=" * 100 + "\n")

        if not hit_indexes:
            report.write("\nСовпадений не найдено.\n")
            return report_path, len(messages), 0

        for fragment_no, (start, end) in enumerate(windows, 1):
            report.write("\n\n")
            report.write("#" * 100 + "\n")
            report.write(f"ФРАГМЕНТ #{fragment_no}\n")
            report.write(f"Сообщения истории: {start + 1} — {end + 1}\n")

            fragment_hit_indexes = [i for i in hit_indexes if start <= i <= end]
            fragment_hits = [hit for i in fragment_hit_indexes for hit in hit_map[i]]
            fragment_risk = highest_risk(fragment_hits)

            categories = sorted({hit["category"] for hit in fragment_hits})
            words = sorted({hit["raw"] for hit in fragment_hits}, key=str.casefold)

            report.write(f"Максимальный уровень: {fragment_risk}\n")
            report.write(f"Категории: {', '.join(categories)}\n")
            report.write(f"Триггеры: {', '.join(words)}\n")
            report.write("#" * 100 + "\n\n")

            for index in range(start, end + 1):
                msg = messages[index]
                text = (getattr(msg, "message", "") or "").replace("\x00", "")
                name = await sender_name(msg)

                date = getattr(msg, "date", None)
                if date:
                    try:
                        date_text = date.astimezone().strftime("%d.%m.%Y %H:%M:%S")
                    except Exception:
                        date_text = date.strftime("%d.%m.%Y %H:%M:%S")
                else:
                    date_text = "?"

                if index in hit_map:
                    msg_hits = hit_map[index]
                    msg_risk = highest_risk(msg_hits)

                    report.write("\n>>> ПОДОЗРИТЕЛЬНОЕ СООБЩЕНИЕ <<<\n")
                    report.write(f"УРОВЕНЬ: {msg_risk}\n")

                    grouped = defaultdict(list)
                    for hit in msg_hits:
                        grouped[hit["category"]].append(hit["raw"])

                    for category, words_in_category in grouped.items():
                        report.write(
                            f"[{category}] "
                            + ", ".join(sorted(set(words_in_category), key=str.casefold))
                            + "\n"
                        )

                report.write(
                    f"[{date_text}] msg_id={getattr(msg, 'id', '?')} | {name}\n"
                )

                if text:
                    report.write(text + "\n")
                elif getattr(msg, "media", None):
                    report.write("[медиа без текста]\n")
                else:
                    report.write("[пустое сообщение]\n")

                report.write("-" * 100 + "\n")

    return report_path, len(messages), len(hit_indexes)


# ============================================================
# HANDLERS
# ============================================================

@router.message(CommandStart())
async def command_start(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return

    await state.clear()

    await message.answer(
        "🔍 <b>Telegram Trigger Scanner</b>\n\n"
        "1. Подключи Telegram-аккаунт через Telethon.\n"
        "2. Выбери чат.\n"
        "3. Бот полностью пройдёт историю.\n"
        f"4. Для каждого совпадения сохранит ±{CONTEXT_MESSAGES} сообщений.\n"
        "5. Вернёт итоговый TXT-файл.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@router.callback_query(F.data == "menu")
async def menu_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return

    await state.clear()
    await call.message.edit_text("Главное меню:", reply_markup=main_keyboard())
    await call.answer()


@router.callback_query(F.data == "noop")
async def noop_handler(call: CallbackQuery):
    if is_admin(call.from_user.id):
        await call.answer()


@router.callback_query(F.data == "dictionary")
async def dictionary_handler(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    category_count = len(TRIGGER_CATEGORIES)
    trigger_count = len(TRIGGER_INDEX)

    counts = defaultdict(int)
    for item in TRIGGER_INDEX:
        counts[item["risk"]] += 1

    await call.answer()
    await call.message.answer(
        "📚 <b>Встроенный словарь</b>\n\n"
        f"Категорий: <b>{category_count}</b>\n"
        f"Триггеров: <b>{trigger_count}</b>\n\n"
        f"🔴 Критический: {counts['КРИТИЧЕСКИЙ']}\n"
        f"🟠 Высокий: {counts['ВЫСОКИЙ']}\n"
        f"🟡 Средний: {counts['СРЕДНИЙ']}\n"
        f"⚪ Контекстный: {counts['КОНТЕКСТНЫЙ']}",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "status")
async def status_handler(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    if not await ensure_authorized():
        await call.answer()
        await call.message.answer("❌ Telethon-аккаунт не подключён.")
        return

    me = await client.get_me()
    name = " ".join(
        x for x in (
            getattr(me, "first_name", "") or "",
            getattr(me, "last_name", "") or "",
        )
        if x
    ).strip()

    username = getattr(me, "username", None)

    await call.answer()
    await call.message.answer(
        "✅ <b>Аккаунт подключён</b>\n\n"
        f"ID: <code>{me.id}</code>\n"
        f"Имя: {html.escape(name or '—')}\n"
        f"Username: @{html.escape(username) if username else '—'}",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "connect")
async def connect_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return

    if await ensure_authorized():
        await call.answer("Аккаунт уже подключён.", show_alert=True)
        return

    await state.set_state(LoginState.phone)
    await call.answer()
    await call.message.answer(
        "📱 Отправь номер телефона аккаунта в международном формате.\n"
        "Например: <code>+79991234567</code>",
        parse_mode="HTML",
    )


@router.message(LoginState.phone)
async def login_phone_handler(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    phone = (message.text or "").strip().replace(" ", "")

    if not re.fullmatch(r"\+\d{7,15}", phone):
        await message.answer("❌ Неверный формат. Пример: +79991234567")
        return

    try:
        if not client.is_connected():
            await client.connect()

        sent = await client.send_code_request(phone)

    except PhoneNumberInvalidError:
        await message.answer("❌ Telegram считает этот номер неверным.")
        return
    except Exception as exc:
        await message.answer(f"❌ Ошибка отправки кода: {type(exc).__name__}: {exc}")
        return

    await state.update_data(
        phone=phone,
        phone_code_hash=sent.phone_code_hash,
    )
    await state.set_state(LoginState.code)

    await message.answer(
        "📨 Код входа отправлен Telegram.\n"
        "Пришли код сюда цифрами."
    )


@router.message(LoginState.code)
async def login_code_handler(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    code = re.sub(r"\D", "", message.text or "")
    data = await state.get_data()

    try:
        await client.sign_in(
            phone=data["phone"],
            code=code,
            phone_code_hash=data["phone_code_hash"],
        )

    except SessionPasswordNeededError:
        await state.set_state(LoginState.password)
        await message.answer("🔐 Включена 2FA. Отправь облачный пароль.")
        return

    except PhoneCodeInvalidError:
        await message.answer("❌ Неверный код. Попробуй ещё раз.")
        return

    except PhoneCodeExpiredError:
        await state.clear()
        await message.answer(
            "❌ Код истёк. Запусти подключение заново.",
            reply_markup=main_keyboard(),
        )
        return

    except Exception as exc:
        await state.clear()
        await message.answer(
            f"❌ Ошибка входа: {type(exc).__name__}: {exc}",
            reply_markup=main_keyboard(),
        )
        return

    await state.clear()
    await message.answer(
        "✅ Аккаунт успешно подключён.",
        reply_markup=main_keyboard(),
    )


@router.message(LoginState.password)
async def login_password_handler(message: Message, state: FSMContext):
    if not message.from_user or not is_admin(message.from_user.id):
        return

    try:
        await client.sign_in(password=message.text or "")
    except Exception as exc:
        await message.answer(f"❌ Ошибка 2FA: {type(exc).__name__}: {exc}")
        return

    await state.clear()
    await message.answer(
        "✅ 2FA пройдена. Аккаунт подключён.",
        reply_markup=main_keyboard(),
    )


@router.callback_query(F.data == "logout")
async def logout_handler(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return

    await call.answer()

    try:
        if not client.is_connected():
            await client.connect()

        if await client.is_user_authorized():
            await client.log_out()

    except Exception as exc:
        await call.message.answer(
            f"⚠️ Не удалось завершить сессию: {type(exc).__name__}: {exc}"
        )
        return

    await state.clear()
    await call.message.answer(
        "🚪 Telethon-аккаунт отключён.",
        reply_markup=main_keyboard(),
    )


@router.callback_query(F.data.startswith("dialogs:"))
async def dialogs_handler(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    if not await ensure_authorized():
        await call.answer("Сначала подключи аккаунт.", show_alert=True)
        return

    try:
        page = int(call.data.split(":", 1)[1])
    except Exception:
        page = 0

    keyboard, total = await build_dialog_keyboard(page)

    await call.answer()
    await call.message.edit_text(
        f"💬 Выбери чат для полного сканирования.\n"
        f"Диалогов: {total}",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("scan:"))
async def scan_handler(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return

    if scan_lock.locked():
        await call.answer("Сканирование уже выполняется.", show_alert=True)
        return

    if not await ensure_authorized():
        await call.answer("Сначала подключи аккаунт.", show_alert=True)
        return

    try:
        peer_id = int(call.data.split(":", 1)[1])
        entity = await resolve_dialog(peer_id)
    except Exception as exc:
        await call.answer("Не удалось открыть этот чат.", show_alert=True)
        return

    title = await entity_name(entity)

    await call.answer()
    status_message = await call.message.answer(
        f"⏳ Начинаю полное сканирование:\n"
        f"<b>{html.escape(title)}</b>\n\n"
        "История читается от самого старого сообщения к самому новому.",
        parse_mode="HTML",
    )

    async with scan_lock:
        try:
            report_path, total_messages, hit_messages = await scan_dialog(
                entity,
                status_message,
            )

        except FloodWaitError as exc:
            await status_message.edit_text(
                f"⚠️ Telegram выдал FloodWait: {exc.seconds} сек.\n"
                "Повтори сканирование после окончания ограничения."
            )
            return

        except Exception as exc:
            await status_message.edit_text(
                f"❌ Ошибка сканирования:\n"
                f"{type(exc).__name__}: {exc}"
            )
            return

    await status_message.edit_text(
        "✅ Сканирование завершено.\n\n"
        f"Проверено сообщений: {total_messages:,}\n"
        f"Сообщений с триггерами: {hit_messages:,}\n"
        f"Контекст: ±{CONTEXT_MESSAGES}"
    )

    try:
        await call.message.answer_document(
            FSInputFile(report_path),
            caption=(
                f"📄 Отчёт: {title}\n"
                f"Проверено: {total_messages:,}\n"
                f"С триггерами: {hit_messages:,}\n"
                f"Контекст: {CONTEXT_MESSAGES} выше / {CONTEXT_MESSAGES} ниже"
            ),
        )
    except Exception as exc:
        await call.message.answer(
            f"⚠️ Отчёт создан, но Telegram не смог отправить файл:\n{exc}\n\n"
            f"Файл находится локально:\n<code>{html.escape(str(report_path))}</code>",
            parse_mode="HTML",
        )

    await call.message.answer(
        "Готово. Можно выбрать другой чат.",
        reply_markup=main_keyboard(),
    )


# ============================================================
# MAIN
# ============================================================

async def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_BOT_TOKEN_HERE":
        raise RuntimeError("Укажи BOT_TOKEN в начале bot.py")

    if not API_ID:
        raise RuntimeError("Укажи API_ID в начале bot.py")

    if not API_HASH or API_HASH == "PASTE_API_HASH_HERE":
        raise RuntimeError("Укажи API_HASH в начале bot.py")

    await client.connect()

    bot = Bot(BOT_TOKEN)

    print("=" * 60)
    print("Telegram Trigger Scanner запущен")
    print(f"Категорий: {len(TRIGGER_CATEGORIES)}")
    print(f"Триггеров: {len(TRIGGER_INDEX)}")
    print("=" * 60)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())