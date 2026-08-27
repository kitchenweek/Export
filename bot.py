"""Перенос содержимого постов между двумя Telegram-каналами.

Скрипт входит в обычный Telegram-аккаунт через Telethon, сопоставляет каждый
пост исходного канала с ближайшим по дате ещё не использованным постом целевого
канала и редактирует целевой пост. Список использованных сообщений существует
только до завершения текущего запуска.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from telethon import TelegramClient, errors, types, utils
from telethon.tl.custom import Dialog, Message


SESSION_NAME = "channel_copier"
LOG_FILE = "migration_log.csv"
PREVIEW_LIMIT = 30

# Данные приложения Telegram. Их также можно переопределить переменными
# окружения TG_API_ID и TG_API_HASH.
API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"

# Для входа в обычный аккаунт этот токен не нужен. Он сохранён здесь по просьбе
# пользователя, но намеренно не передаётся в client.start(): бот-аккаунт не
# подходит для переноса всей истории каналов через пользовательскую сессию.
BOT_TOKEN = "8797332751:AAE_WMFhyYtNXrhyIq-xCky50Dzynlz3Xco"


@dataclass(frozen=True)
class Pair:
    source: Message
    target: Message
    difference_seconds: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Перенос постов в ближайшие по дате сообщения другого канала"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="не запрашивать финальное подтверждение",
    )
    parser.add_argument(
        "--session",
        default=SESSION_NAME,
        help=f"имя файла сессии Telethon (по умолчанию: {SESSION_NAME})",
    )
    return parser.parse_args()


def read_credentials() -> tuple[int, str, str | None]:
    """Берёт реквизиты из окружения или спрашивает их в консоли."""
    api_id_text = os.getenv("TG_API_ID") or str(API_ID)
    api_hash = os.getenv("TG_API_HASH") or API_HASH
    phone = os.getenv("TG_PHONE") or input(
        "Телефон аккаунта с + и кодом страны: "
    ).strip()

    try:
        api_id = int(api_id_text)
    except ValueError as exc:
        raise SystemExit("Ошибка: API ID должен быть целым числом.") from exc

    if not api_hash:
        raise SystemExit("Ошибка: API HASH не может быть пустым.")

    return api_id, api_hash, phone or None


async def get_broadcast_channels(client: TelegramClient) -> list[Dialog]:
    channels: list[Dialog] = []
    async for dialog in client.iter_dialogs():
        if dialog.is_channel and getattr(dialog.entity, "broadcast", False):
            channels.append(dialog)
    return sorted(channels, key=lambda item: (item.name or "").casefold())


def channel_label(dialog: Dialog) -> str:
    username = getattr(dialog.entity, "username", None)
    suffix = f" (@{username})" if username else ""
    return f"{dialog.name}{suffix} [id={dialog.entity.id}]"


def choose_channel(channels: Sequence[Dialog], prompt: str) -> Dialog:
    if not channels:
        raise SystemExit("В аккаунте не найдено ни одного канала.")

    print("\nДоступные каналы:")
    for index, dialog in enumerate(channels, start=1):
        print(f"{index:>4}. {channel_label(dialog)}")

    while True:
        value = input(f"\n{prompt} (номер из списка): ").strip()
        try:
            index = int(value) - 1
        except ValueError:
            print("Введите номер канала из списка.")
            continue
        if 0 <= index < len(channels):
            return channels[index]
        print("Такого номера в списке нет.")


def is_unsupported_media(message: Message) -> bool:
    return isinstance(
        message.media,
        (
            types.MessageMediaPoll,
            types.MessageMediaDice,
            types.MessageMediaGame,
            types.MessageMediaInvoice,
            types.MessageMediaUnsupported,
        ),
    )


async def load_posts(
    client: TelegramClient, entity: object, channel_name: str
) -> tuple[list[Message], dict[str, int]]:
    posts: list[Message] = []
    skipped = {"albums": 0, "unsupported": 0, "empty": 0}

    async for message in client.iter_messages(entity, reverse=True):
        if message.action is not None:
            continue
        if not message.raw_text and message.media is None:
            skipped["empty"] += 1
            continue
        if message.grouped_id is not None:
            skipped["albums"] += 1
            continue
        if is_unsupported_media(message):
            skipped["unsupported"] += 1
            continue
        posts.append(message)

    print(
        f"{channel_name}: найдено {len(posts)} подходящих постов; "
        f"пропущено элементов альбомов — {skipped['albums']}, "
        f"специальных/неподдерживаемых — {skipped['unsupported']}."
    )
    return posts, skipped


def build_pairs(source_posts: Sequence[Message], target_posts: Sequence[Message]) -> list[Pair]:
    """Жадно выбирает ближайший ещё не занятый target для каждого source."""
    available = sorted(
        ((post.date.timestamp(), post.id, post) for post in target_posts),
        key=lambda item: (item[0], item[1]),
    )
    timestamps = [item[0] for item in available]
    used_target_ids: set[int] = set()
    pairs: list[Pair] = []

    for source in sorted(source_posts, key=lambda post: (post.date, post.id)):
        if not available:
            break

        source_timestamp = source.date.timestamp()
        insertion = bisect_left(timestamps, source_timestamp)
        candidate_indexes = []
        if insertion > 0:
            candidate_indexes.append(insertion - 1)
        if insertion < len(available):
            candidate_indexes.append(insertion)

        best_index = min(
            candidate_indexes,
            key=lambda index: (
                abs(available[index][0] - source_timestamp),
                available[index][0],
                available[index][1],
            ),
        )
        target_timestamp, target_id, target = available.pop(best_index)
        timestamps.pop(best_index)

        if target_id in used_target_ids:
            raise RuntimeError("Внутренняя ошибка: целевой пост выбран повторно.")
        used_target_ids.add(target_id)
        pairs.append(
            Pair(
                source=source,
                target=target,
                difference_seconds=abs(target_timestamp - source_timestamp),
            )
        )

    return pairs


def format_date(value: datetime) -> str:
    return value.astimezone().strftime("%d.%m.%Y %H:%M:%S")


def format_difference(seconds: float) -> str:
    total_minutes = round(seconds / 60)
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    return f"{days} д. {hours:02}:{minutes:02}"


def show_preview(pairs: Sequence[Pair]) -> None:
    print(f"\nБудет обработано пар: {len(pairs)}")
    print("Первые сопоставления:")
    for pair in pairs[:PREVIEW_LIMIT]:
        print(
            f"  source #{pair.source.id} {format_date(pair.source.date)}"
            f"  ->  target #{pair.target.id} {format_date(pair.target.date)}"
            f"  (разница {format_difference(pair.difference_seconds)})"
        )
    if len(pairs) > PREVIEW_LIMIT:
        print(f"  ...и ещё {len(pairs) - PREVIEW_LIMIT} пар.")


def transferable_media(message: Message) -> object | None:
    # Предпросмотр ссылки Telegram создаст заново из текста.
    if isinstance(message.media, types.MessageMediaWebPage):
        return None
    return message.media


async def edit_with_flood_wait(
    client: TelegramClient,
    target_entity: object,
    pair: Pair,
) -> str:
    source = pair.source
    target = pair.target
    source_media = transferable_media(source)
    source_has_web_preview = isinstance(source.media, types.MessageMediaWebPage)
    target_has_attached_media = target.media is not None and not isinstance(
        target.media, types.MessageMediaWebPage
    )

    file_to_set: object | None
    if source_media is not None:
        file_to_set = source_media
    elif target_has_attached_media:
        # Явно удаляем старое вложение, если исходный пост только текстовый.
        file_to_set = types.InputMediaEmpty()
    else:
        file_to_set = None

    while True:
        try:
            await client.edit_message(
                target_entity,
                target.id,
                source.raw_text or "",
                formatting_entities=source.entities or [],
                link_preview=source_has_web_preview,
                file=file_to_set,
            )
            return "changed"
        except errors.MessageNotModifiedError:
            # После повторного запуска содержимое может уже совпадать.
            return "already_equal"
        except errors.FloodWaitError as exc:
            wait_seconds = int(exc.seconds) + 1
            print(f"Telegram просит подождать {wait_seconds} сек.")
            await asyncio.sleep(wait_seconds)


def write_log(rows: Sequence[dict[str, object]]) -> Path:
    path = Path(LOG_FILE).resolve()
    fieldnames = [
        "source_id",
        "source_date",
        "target_id",
        "target_date",
        "difference_seconds",
        "status",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


async def run(args: argparse.Namespace) -> None:
    api_id, api_hash, phone = read_credentials()
    client = TelegramClient(args.session, api_id, api_hash)

    await client.start(phone=phone)
    try:
        me = await client.get_me()
        print(f"\nВход выполнен: {utils.get_display_name(me)}")

        channels = await get_broadcast_channels(client)
        source_dialog = choose_channel(channels, "Выберите Канал 1 (источник)")
        target_dialog = choose_channel(channels, "Выберите Канал 2 (куда переносить)")

        if utils.get_peer_id(source_dialog.entity) == utils.get_peer_id(target_dialog.entity):
            raise SystemExit("Источник и целевой канал должны быть разными.")

        print("\nЗагрузка истории каналов...")
        source_posts, _ = await load_posts(
            client, source_dialog.entity, "Канал 1"
        )
        target_posts, _ = await load_posts(
            client, target_dialog.entity, "Канал 2"
        )

        if not source_posts:
            raise SystemExit("В Канале 1 нет подходящих постов.")
        if not target_posts:
            raise SystemExit("В Канале 2 нет подходящих постов.")

        pairs = build_pairs(source_posts, target_posts)
        show_preview(pairs)

        if len(source_posts) > len(target_posts):
            print(
                f"Внимание: в Канале 1 на {len(source_posts) - len(target_posts)} "
                "постов больше. Для них не хватит уникальных постов Канала 2."
            )

        if not args.yes:
            confirmation = input(
                "\nДля начала редактирования введите слово ИЗМЕНИТЬ: "
            ).strip()
            if confirmation != "ИЗМЕНИТЬ":
                print("Операция отменена, сообщения не изменялись.")
                return

        rows: list[dict[str, object]] = []
        changed = 0
        already_equal = 0
        failed = 0

        print("\nРедактирование начато...")
        for index, pair in enumerate(pairs, start=1):
            row: dict[str, object] = {
                "source_id": pair.source.id,
                "source_date": pair.source.date.isoformat(),
                "target_id": pair.target.id,
                "target_date": pair.target.date.isoformat(),
                "difference_seconds": round(pair.difference_seconds),
                "status": "",
                "error": "",
            }
            try:
                status = await edit_with_flood_wait(
                    client, target_dialog.entity, pair
                )
                row["status"] = status
                if status == "changed":
                    changed += 1
                else:
                    already_equal += 1
            except errors.RPCError as exc:
                failed += 1
                row["status"] = "failed"
                row["error"] = f"{type(exc).__name__}: {exc}"
                print(
                    f"[{index}/{len(pairs)}] Ошибка для target #{pair.target.id}: {exc}"
                )
            except Exception as exc:  # не прерываем всю миграцию из-за одного поста
                failed += 1
                row["status"] = "failed"
                row["error"] = f"{type(exc).__name__}: {exc}"
                print(
                    f"[{index}/{len(pairs)}] Неожиданная ошибка для "
                    f"target #{pair.target.id}: {exc}"
                )
            rows.append(row)

            if index % 20 == 0 or index == len(pairs):
                print(
                    f"Прогресс: {index}/{len(pairs)}; изменено {changed}; "
                    f"уже совпадало {already_equal}; ошибок {failed}."
                )
            await asyncio.sleep(0.7)

        log_path = write_log(rows)
        print(
            f"\nГотово. Изменено: {changed}; уже совпадало: {already_equal}; "
            f"ошибок: {failed}.\nЛог: {log_path}"
        )
    finally:
        await client.disconnect()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")


if __name__ == "__main__":
    main()