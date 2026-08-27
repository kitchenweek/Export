"""Telegram-бот для переноса постов между каналами через пользовательский аккаунт.

Управление происходит в личном чате с ботом. Сам бот служит только панелью
управления, а историю читает и целевые посты редактирует отдельная сессия
обычного Telegram-аккаунта через Telethon.
"""

from __future__ import annotations

import asyncio
import csv
import os
import tempfile
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from telethon import Button, TelegramClient, errors, events, types, utils
from telethon.sessions import StringSession
from telethon.tl.custom import Dialog, Message


# Предоставленные данные Telegram.
API_ID = 32200104
API_HASH = "4c657a43a0c2419cd5b18c44d09e68c1"
BOT_TOKEN = "8797332751:AAE_WMFhyYtNXrhyIq-xCky50Dzynlz3Xco"

# Можно задать TG_OWNER_ID на хостинге. Если оставить 0, владелец привязывается
# один раз командой /claim с кодом ниже.
OWNER_ID = int(os.getenv("TG_OWNER_ID", "0") or "0")
CLAIM_CODE = os.getenv("TG_CLAIM_CODE", "68274193")

USER_SESSION_FILE = os.getenv("TG_USER_SESSION", "user_account")
OWNER_FILE = Path("owner_id.txt")
LOG_FILE = Path("migration_log.csv")
CHANNELS_PER_PAGE = 8
PREVIEW_LIMIT = 12


@dataclass(frozen=True)
class AlbumInfo:
    message_ids: tuple[int, ...]
    caption_text: str
    caption_entities: tuple[object, ...]


@dataclass(frozen=True)
class Pair:
    source: Message
    target: Message
    difference_seconds: float
    album: AlbumInfo | None = None


def load_saved_owner() -> int | None:
    if OWNER_ID > 0:
        return OWNER_ID
    try:
        value = int(OWNER_FILE.read_text(encoding="utf-8").strip())
        return value if value > 0 else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def save_owner(owner_id: int) -> None:
    OWNER_FILE.write_text(str(owner_id), encoding="utf-8")


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
    client: TelegramClient,
    entity: object,
    *,
    allow_albums_without_media: bool = False,
) -> tuple[list[Message], dict[str, int], dict[int, AlbumInfo]]:
    posts: list[Message] = []
    album_messages: dict[int, list[Message]] = {}
    albums: dict[int, AlbumInfo] = {}
    skipped = {
        "album_groups": 0,
        "album_items": 0,
        "unsupported": 0,
        "empty": 0,
    }

    async for message in client.iter_messages(entity, reverse=True):
        if message.action is not None:
            continue
        if not message.raw_text and message.media is None:
            skipped["empty"] += 1
            continue
        if message.grouped_id is not None:
            album_messages.setdefault(message.grouped_id, []).append(message)
            continue
        if is_unsupported_media(message):
            skipped["unsupported"] += 1
            continue
        posts.append(message)

    skipped["album_groups"] = len(album_messages)
    skipped["album_items"] = sum(len(items) for items in album_messages.values())

    if allow_albums_without_media:
        for items in album_messages.values():
            ordered = sorted(items, key=lambda item: (item.date, item.id))
            captioned = [item for item in ordered if item.raw_text]
            caption_message = captioned[0] if captioned else None
            photos = [
                item
                for item in ordered
                if isinstance(item.media, types.MessageMediaPhoto)
            ]
            # Для альбома берём именно первое фото. Если фото нет, берём первый
            # медиаэлемент (например, видео), чтобы не терять весь пост.
            representative = photos[0] if photos else ordered[0]
            posts.append(representative)
            albums[representative.id] = AlbumInfo(
                message_ids=tuple(item.id for item in ordered),
                caption_text=(caption_message.raw_text if caption_message else ""),
                caption_entities=tuple(
                    caption_message.entities or [] if caption_message else []
                ),
            )

    posts.sort(key=lambda item: (item.date, item.id))
    return posts, skipped, albums


def build_pairs(
    source_posts: Sequence[Message],
    target_posts: Sequence[Message],
    source_albums: dict[int, AlbumInfo] | None = None,
) -> list[Pair]:
    """Для каждого source выбирает ближайший ещё не использованный target."""
    album_map = source_albums or {}
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
        candidates: list[int] = []
        if insertion > 0:
            candidates.append(insertion - 1)
        if insertion < len(available):
            candidates.append(insertion)

        best_index = min(
            candidates,
            key=lambda index: (
                abs(available[index][0] - source_timestamp),
                available[index][0],
                available[index][1],
            ),
        )
        target_timestamp, target_id, target = available.pop(best_index)
        timestamps.pop(best_index)

        if target_id in used_target_ids:
            raise RuntimeError("Целевой пост был выбран повторно.")
        used_target_ids.add(target_id)
        pairs.append(
            Pair(
                source=source,
                target=target,
                difference_seconds=abs(target_timestamp - source_timestamp),
                album=album_map.get(source.id),
            )
        )

    return pairs


def format_date(value: datetime) -> str:
    return value.astimezone().strftime("%d.%m.%Y %H:%M")


def format_difference(seconds: float) -> str:
    total_minutes = round(seconds / 60)
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    return f"{days} д. {hours:02}:{minutes:02}"


def describe_sent_code(sent_code: types.auth.SentCode) -> str:
    """Преобразует ответ Telegram в понятное описание доставки кода."""
    delivery_type = sent_code.type
    if isinstance(delivery_type, types.auth.SentCodeTypeApp):
        delivery = (
            "Код отправлен в системный чат «Telegram» внутри уже "
            "авторизованного приложения Telegram, не по SMS."
        )
    elif isinstance(delivery_type, types.auth.SentCodeTypeSms):
        delivery = "Код отправлен по SMS."
    elif isinstance(delivery_type, types.auth.SentCodeTypeCall):
        delivery = "Код будет продиктован автоматическим телефонным звонком."
    elif isinstance(delivery_type, types.auth.SentCodeTypeFlashCall):
        delivery = "Код определяется по номеру входящего flash-звонка."
    elif isinstance(delivery_type, types.auth.SentCodeTypeMissedCall):
        delivery = "Код — последние цифры номера пропущенного звонка."
    elif isinstance(delivery_type, types.auth.SentCodeTypeEmailCode):
        pattern = getattr(delivery_type, "email_pattern", "указанный адрес")
        delivery = f"Код отправлен на email: {pattern}."
    elif isinstance(delivery_type, types.auth.SentCodeTypeFragmentSms):
        delivery = "Код отправлен через Fragment."
    elif isinstance(delivery_type, types.auth.SentCodeTypeFirebaseSms):
        delivery = "Telegram выбрал доставку через защищённый SMS/push."
    elif isinstance(delivery_type, types.auth.SentCodeTypeSmsWord):
        delivery = "По SMS отправлено кодовое слово. Пришлите его полностью."
    elif isinstance(delivery_type, types.auth.SentCodeTypeSmsPhrase):
        delivery = "По SMS отправлена кодовая фраза. Пришлите её полностью."
    elif isinstance(delivery_type, types.auth.SentCodeTypeSetUpEmailRequired):
        delivery = "Telegram требует сначала настроить email для входа."
    else:
        delivery = f"Telegram выбрал способ доставки: {type(delivery_type).__name__}."

    length = getattr(delivery_type, "length", None)
    if isinstance(length, int) and length > 0:
        delivery += f" Длина кода: {length}."
    if isinstance(sent_code.timeout, int) and sent_code.timeout > 0:
        delivery += (
            f" Повторный запрос станет доступен примерно через "
            f"{sent_code.timeout} сек."
        )
    delivery += " Если код не появился, подождите указанное время и отправьте /resend."
    return delivery


def transferable_media(message: Message) -> object | None:
    if isinstance(message.media, types.MessageMediaWebPage):
        return None
    return message.media


async def edit_with_flood_wait(
    client: TelegramClient, target_entity: object, pair: Pair
) -> str:
    source = pair.source
    target = pair.target
    source_media = transferable_media(source)
    source_has_web_preview = pair.album is None and isinstance(
        source.media, types.MessageMediaWebPage
    )
    target_has_attached_media = target.media is not None and not isinstance(
        target.media, types.MessageMediaWebPage
    )

    if source_media is not None:
        file_to_set: object | None = source_media
    elif target_has_attached_media:
        file_to_set = types.InputMediaEmpty()
    else:
        file_to_set = None

    text_to_set = (
        pair.album.caption_text if pair.album is not None else source.raw_text or ""
    )
    entities_to_set = (
        list(pair.album.caption_entities)
        if pair.album is not None
        else source.entities or []
    )

    async def perform_edit(
        file_value: object | None,
        text_value: str,
        formatting_entities: Sequence[object],
    ) -> None:
        while True:
            try:
                await client.edit_message(
                    target_entity,
                    target.id,
                    text_value,
                    formatting_entities=list(formatting_entities),
                    link_preview=source_has_web_preview,
                    file=file_value,
                )
                return
            except errors.FloodWaitError as exc:
                await asyncio.sleep(int(exc.seconds) + 1)

    try:
        await perform_edit(file_to_set, text_to_set, entities_to_set)
        return "changed"
    except errors.MessageNotModifiedError:
        return "already_equal"
    except errors.RPCError as direct_error:
        # Некоторые фото Telegram не даёт переиспользовать напрямую. Тогда
        # скачиваем оригинал и загружаем его заново, чтобы сохранить пост.
        if (
            source_media is not None
            and type(direct_error).__name__ != "MediaCaptionTooLongError"
        ):
            try:
                with tempfile.TemporaryDirectory(prefix="tg_post_") as directory:
                    downloaded = await client.download_media(source, file=directory)
                    if downloaded:
                        await perform_edit(downloaded, text_to_set, entities_to_set)
                        return "changed_reuploaded"
            except errors.MessageNotModifiedError:
                return "already_equal"
            except (errors.RPCError, OSError):
                pass

        # Если тип медиа вообще нельзя поставить редактированием или подпись
        # слишком длинная, сохраняем полный текст поста без нового медиа.
        fallback_text = text_to_set or "Медиа не перенесено"
        remove_target_media = (
            types.InputMediaEmpty() if target_has_attached_media else None
        )
        try:
            await perform_edit(
                remove_target_media,
                fallback_text,
                entities_to_set if text_to_set else [],
            )
            return "changed_text_only"
        except errors.MessageNotModifiedError:
            return "already_equal"
        except errors.RPCError:
            # Повреждённые или неподдерживаемые entities не должны мешать
            # переносу самого текста.
            try:
                await perform_edit(remove_target_media, fallback_text, [])
                return "changed_text_only_plain"
            except errors.MessageNotModifiedError:
                return "already_equal"
            except errors.RPCError:
                if not target_has_attached_media:
                    raise direct_error

        # Telegram не всегда разрешает удалить старое медиа из существующего
        # поста. Тогда оставляем его и меняем подпись; она ограничена 1024
        # символами, поэтому форматирование в этом аварийном режиме очищается.
        safe_caption = fallback_text[:1024]
        try:
            await perform_edit(None, safe_caption, [])
            return "changed_caption_only"
        except errors.MessageNotModifiedError:
            return "already_equal"
        except errors.RPCError:
            raise direct_error


def write_log(rows: Sequence[dict[str, object]]) -> Path:
    path = LOG_FILE.resolve()
    fieldnames = [
        "source_id",
        "source_date",
        "target_id",
        "target_date",
        "difference_seconds",
        "album_first_media_only",
        "source_album_message_ids",
        "status",
        "fallback",
        "error",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


async def main() -> None:
    control_bot = TelegramClient(StringSession(), API_ID, API_HASH)
    user_client = TelegramClient(USER_SESSION_FILE, API_ID, API_HASH)

    await control_bot.start(bot_token=BOT_TOKEN)
    await user_client.connect()

    owner_id = load_saved_owner()
    login_state: dict[str, object] = {"step": "idle"}
    channel_cache: dict[int, Dialog] = {}
    migration_state: dict[str, object] = {}
    migration_lock = asyncio.Lock()

    async def delete_sensitive(event: events.NewMessage.Event) -> None:
        try:
            await event.delete()
        except errors.RPCError:
            pass

    async def is_authorized() -> bool:
        return await user_client.is_user_authorized()

    async def help_text() -> str:
        authorized = await is_authorized()
        status = "подключён" if authorized else "не подключён"
        return (
            "Бот переноса постов\n\n"
            f"Пользовательский аккаунт: {status}.\n\n"
            "/login — войти в обычный Telegram-аккаунт\n"
            "/resend — повторно запросить код входа\n"
            "/migrate — выбрать два канала и начать перенос\n"
            "/status — проверить состояние\n"
            "/cancel — отменить текущий выбор\n"
            "/help — показать команды\n\n"
            "Каждый запуск /migrate заново разрешает использовать посты "
            "Канала 2, но внутри одного запуска каждый пост используется "
            "только один раз."
        )

    async def get_channels() -> list[Dialog]:
        result: list[Dialog] = []
        async for dialog in user_client.iter_dialogs():
            if dialog.is_channel and getattr(dialog.entity, "broadcast", False):
                result.append(dialog)
        return sorted(result, key=lambda item: (item.name or "").casefold())

    def channel_title(dialog: Dialog, max_length: int = 48) -> str:
        name = dialog.name or "Без названия"
        username = getattr(dialog.entity, "username", None)
        label = f"{name} (@{username})" if username else name
        if len(label) > max_length:
            label = label[: max_length - 1] + "…"
        return label

    def channel_keyboard(stage: str, page: int) -> tuple[str, list[list[Button]]]:
        exclude_id = None
        source_for_exclusion = migration_state.get("source")
        if stage == "target" and isinstance(source_for_exclusion, Dialog):
            exclude_id = source_for_exclusion.entity.id

        channels = [
            dialog
            for dialog in channel_cache.values()
            if dialog.entity.id != exclude_id
        ]
        total_pages = max(
            1, (len(channels) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE
        )
        page = max(0, min(page, total_pages - 1))
        start = page * CHANNELS_PER_PAGE
        current = channels[start : start + CHANNELS_PER_PAGE]

        rows: list[list[Button]] = [
            [
                Button.inline(
                    channel_title(dialog),
                    data=f"pick:{stage}:{dialog.entity.id}".encode(),
                )
            ]
            for dialog in current
        ]
        navigation: list[Button] = []
        if page > 0:
            navigation.append(
                Button.inline("⬅️", data=f"page:{stage}:{page - 1}".encode())
            )
        navigation.append(
            Button.inline(
                f"{page + 1}/{total_pages}", data=f"noop:{stage}:{page}".encode()
            )
        )
        if page + 1 < total_pages:
            navigation.append(
                Button.inline("➡️", data=f"page:{stage}:{page + 1}".encode())
            )
        rows.append(navigation)
        rows.append([Button.inline("Отмена", data=b"cancel")])

        title = (
            "Выберите Канал 1 — источник:"
            if stage == "source"
            else "Выберите Канал 2 — канал с сырыми постами:"
        )
        return title, rows

    async def begin_migration(chat_id: int) -> None:
        if migration_lock.locked():
            await control_bot.send_message(chat_id, "Перенос уже выполняется.")
            return
        if not await is_authorized():
            await control_bot.send_message(
                chat_id, "Сначала подключите аккаунт командой /login."
            )
            return

        await control_bot.send_message(chat_id, "Загружаю список каналов…")
        channels = await get_channels()
        if len(channels) < 2:
            await control_bot.send_message(
                chat_id, "В аккаунте должно быть доступно минимум два канала."
            )
            return

        channel_cache.clear()
        channel_cache.update({dialog.entity.id: dialog for dialog in channels})
        migration_state.clear()
        migration_state["stage"] = "source"
        text, buttons = channel_keyboard("source", 0)
        await control_bot.send_message(chat_id, text, buttons=buttons)

    async def prepare_pairs(event: events.CallbackQuery.Event) -> None:
        source = migration_state.get("source")
        target = migration_state.get("target")
        if not isinstance(source, Dialog) or not isinstance(target, Dialog):
            await event.edit("Выбор каналов потерян. Запустите /migrate заново.")
            return

        await event.edit("Загружаю всю историю двух каналов и сопоставляю даты…")
        try:
            source_result, target_result = await asyncio.gather(
                load_posts(
                    user_client,
                    source.entity,
                    allow_albums_without_media=True,
                ),
                load_posts(user_client, target.entity),
            )
            source_posts, source_skipped, source_albums = source_result
            target_posts, target_skipped, _ = target_result
        except errors.RPCError as exc:
            await event.edit(f"Не удалось загрузить историю: {type(exc).__name__}: {exc}")
            migration_state.clear()
            return

        if not source_posts or not target_posts:
            await event.edit(
                "Нет подходящих постов. Альбомы, опросы и служебные сообщения "
                "не участвуют в переносе."
            )
            migration_state.clear()
            return

        pairs = build_pairs(source_posts, target_posts, source_albums)
        migration_state["pairs"] = pairs
        migration_state["stage"] = "confirm"

        lines = [
            "Сопоставление готово.",
            "",
            f"Канал 1: {channel_title(source, 80)} — {len(source_posts)} постов",
            f"Канал 2: {channel_title(target, 80)} — {len(target_posts)} постов",
            f"Будет обработано: {len(pairs)} пар",
            (
                "Альбомов Канала 1 будет перенесено с первым фото: "
                f"{source_skipped['album_groups']}"
            ),
            (
                "Элементов альбомов Канала 2 пропущено: "
                f"{target_skipped['album_items']}"
            ),
            "",
            "Первые пары:",
        ]
        for pair in pairs[:PREVIEW_LIMIT]:
            lines.append(
                f"#{pair.source.id} {format_date(pair.source.date)} → "
                f"#{pair.target.id} {format_date(pair.target.date)} "
                f"({format_difference(pair.difference_seconds)})"
            )
        if len(pairs) > PREVIEW_LIMIT:
            lines.append(f"…и ещё {len(pairs) - PREVIEW_LIMIT}.")
        if len(source_posts) > len(target_posts):
            lines.extend(
                [
                    "",
                    f"Для {len(source_posts) - len(target_posts)} исходных постов "
                    "не хватит уникальных постов Канала 2.",
                ]
            )

        await event.edit(
            "\n".join(lines),
            buttons=[
                [Button.inline("Начать перенос", data=b"run")],
                [Button.inline("Отмена", data=b"cancel")],
            ],
        )

    async def run_migration(chat_id: int) -> None:
        async with migration_lock:
            source = migration_state.get("source")
            target = migration_state.get("target")
            pairs = migration_state.get("pairs")
            if (
                not isinstance(source, Dialog)
                or not isinstance(target, Dialog)
                or not isinstance(pairs, list)
            ):
                await control_bot.send_message(
                    chat_id, "Данные переноса потеряны. Запустите /migrate заново."
                )
                return

            status_message = await control_bot.send_message(
                chat_id, f"Перенос начат: 0/{len(pairs)}"
            )
            rows: list[dict[str, object]] = []
            album_first_media_lines: list[str] = []
            fallback_lines: list[str] = []
            changed = 0
            already_equal = 0
            failed = 0

            for index, pair in enumerate(pairs, start=1):
                row: dict[str, object] = {
                    "source_id": pair.source.id,
                    "source_date": pair.source.date.isoformat(),
                    "target_id": pair.target.id,
                    "target_date": pair.target.date.isoformat(),
                    "difference_seconds": round(pair.difference_seconds),
                    "album_first_media_only": pair.album is not None,
                    "source_album_message_ids": ",".join(
                        str(message_id)
                        for message_id in (
                            pair.album.message_ids if pair.album is not None else ()
                        )
                    ),
                    "status": "",
                    "fallback": "",
                    "error": "",
                }
                try:
                    result = await edit_with_flood_wait(
                        user_client, target.entity, pair
                    )
                    row["status"] = result
                    if result.startswith("changed"):
                        changed += 1
                    else:
                        already_equal += 1
                    if result in {"changed_text_only", "changed_text_only_plain"}:
                        row["fallback"] = "media_skipped_full_text_kept"
                        fallback_lines.append(
                            f"Исходный №{pair.source.id} → целевой "
                            f"№{pair.target.id}: медиа пропущено, полный текст сохранён"
                        )
                    elif result == "changed_caption_only":
                        row["fallback"] = "target_media_kept_caption_limited"
                        fallback_lines.append(
                            f"Исходный №{pair.source.id} → целевой "
                            f"№{pair.target.id}: оставлено прежнее медиа, "
                            "подпись ограничена 1024 символами"
                        )
                except Exception as exc:
                    failed += 1
                    row["status"] = "failed"
                    row["error"] = f"{type(exc).__name__}: {exc}"

                if pair.album is not None:
                    album_ids = ", ".join(
                        f"№{message_id}"
                        for message_id in pair.album.message_ids
                    )
                    result_label = (
                        "перенесено первое фото"
                        if row["status"]
                        in {"changed", "changed_reuploaded", "already_equal"}
                        else (
                            "фото не принято, применён резервный перенос"
                            if row["status"]
                            in {
                                "changed_text_only",
                                "changed_text_only_plain",
                                "changed_caption_only",
                            }
                            else "ошибка переноса"
                        )
                    )
                    album_first_media_lines.append(
                        f"Исходный альбом {album_ids} → целевой пост "
                        f"№{pair.target.id} ({result_label})"
                    )
                rows.append(row)

                if index % 20 == 0 or index == len(pairs):
                    try:
                        await status_message.edit(
                            f"Перенос: {index}/{len(pairs)}\n"
                            f"Изменено: {changed}\n"
                            f"Уже совпадало: {already_equal}\n"
                            f"Ошибок: {failed}"
                        )
                    except errors.MessageNotModifiedError:
                        pass
                await asyncio.sleep(0.7)

            log_path = write_log(rows)
            await control_bot.send_file(
                chat_id,
                log_path,
                caption=(
                    f"Готово. Изменено: {changed}; уже совпадало: "
                    f"{already_equal}; ошибок: {failed}; резервных переносов: "
                    f"{len(fallback_lines)}; альбомов обработано: "
                    f"{len(album_first_media_lines)}."
                ),
            )

            if album_first_media_lines:
                header = "Альбомы, перенесённые только с первым фото:\n\n"
                chunks: list[str] = []
                current = header
                for line in album_first_media_lines:
                    addition = line + "\n"
                    if len(current) + len(addition) > 3800:
                        chunks.append(current.rstrip())
                        current = addition
                    else:
                        current += addition
                if current.strip():
                    chunks.append(current.rstrip())
                for chunk in chunks:
                    await control_bot.send_message(chat_id, chunk)

            if fallback_lines:
                header = "Посты, перенесённые в резервном режиме:\n\n"
                chunks = []
                current = header
                for line in fallback_lines:
                    addition = line + "\n"
                    if len(current) + len(addition) > 3800:
                        chunks.append(current.rstrip())
                        current = addition
                    else:
                        current += addition
                if current.strip():
                    chunks.append(current.rstrip())
                for chunk in chunks:
                    await control_bot.send_message(chat_id, chunk)
            migration_state.clear()

    @control_bot.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        nonlocal owner_id
        if not event.is_private:
            return

        sender_id = event.sender_id
        text = (event.raw_text or "").strip()

        if owner_id is None:
            if text == f"/claim {CLAIM_CODE}":
                owner_id = sender_id
                save_owner(sender_id)
                await event.respond(
                    "Доступ закреплён за вашим аккаунтом. Теперь отправьте /login."
                )
            else:
                await event.respond(
                    "Бот ещё не привязан. Владелец должен отправить команду "
                    "/claim с кодом доступа."
                )
            return

        if sender_id != owner_id:
            await event.respond("Нет доступа.")
            return

        if text in {"/start", "/help"}:
            await event.respond(await help_text())
            return

        if text == "/status":
            if await is_authorized():
                me = await user_client.get_me()
                await event.respond(
                    "Пользовательский аккаунт подключён: "
                    f"{utils.get_display_name(me)} (id={me.id})."
                )
            else:
                await event.respond("Пользовательский аккаунт не подключён.")
            return

        if text == "/login":
            if await is_authorized():
                await event.respond("Аккаунт уже подключён. Используйте /migrate.")
                return
            login_state.clear()
            login_state["step"] = "phone"
            await event.respond(
                "Отправьте номер телефона обычного Telegram-аккаунта в "
                "международном формате, например +491234567890."
            )
            return

        if text == "/resend":
            if login_state.get("step") != "code":
                await event.respond(
                    "Сейчас нет ожидающего кода. Сначала отправьте /login и номер."
                )
                return
            phone = str(login_state.get("phone", ""))
            try:
                sent_code = await user_client.send_code_request(phone)
            except errors.FloodWaitError as exc:
                await event.respond(
                    f"Telegram пока запрещает повторный запрос. Подождите "
                    f"{exc.seconds} секунд."
                )
                return
            except errors.PhoneCodeExpiredError:
                login_state.clear()
                login_state["step"] = "idle"
                await event.respond("Старый запрос истёк. Запустите /login заново.")
                return
            except errors.RPCError as exc:
                await event.respond(
                    f"Telegram отклонил повторный запрос: "
                    f"{type(exc).__name__}: {exc}"
                )
                return
            await event.respond(describe_sent_code(sent_code))
            return

        if text == "/migrate":
            await begin_migration(sender_id)
            return

        if text == "/cancel":
            if migration_lock.locked():
                await event.respond("Идущий перенос остановить этой командой нельзя.")
            else:
                login_state.clear()
                login_state["step"] = "idle"
                migration_state.clear()
                await event.respond("Текущая операция отменена.")
            return

        step = login_state.get("step")
        if step == "phone":
            phone = text.replace(" ", "")
            if not phone.startswith("+") or not phone[1:].isdigit():
                await event.respond("Неверный формат. Пример: +491234567890")
                return
            try:
                sent_code = await user_client.send_code_request(phone)
            except errors.PhoneNumberInvalidError:
                await event.respond("Telegram не принял этот номер. Проверьте его.")
                return
            except errors.FloodWaitError as exc:
                await event.respond(
                    f"Слишком много попыток. Подождите {exc.seconds} секунд."
                )
                return
            except errors.RPCError as exc:
                await event.respond(
                    f"Telegram не отправил код: {type(exc).__name__}: {exc}"
                )
                return
            login_state["phone"] = phone
            login_state["step"] = "code"
            await delete_sensitive(event)
            await control_bot.send_message(
                sender_id,
                describe_sent_code(sent_code)
                + "\n\nПришлите полученный код сюда. Сообщение с кодом "
                "будет сразу удалено ботом.",
            )
            return

        if step == "code":
            phone = str(login_state.get("phone", ""))
            # Обычный цифровой код можно прислать с пробелами. Кодовое слово
            # или фразу, если Telegram выбрал такой способ, сохраняем как есть.
            digits_only = "".join(
                character for character in text if character.isdigit()
            )
            has_letters = any(character.isalpha() for character in text)
            code = text.strip() if has_letters else digits_only
            await delete_sensitive(event)
            try:
                await user_client.sign_in(phone=phone, code=code)
            except errors.SessionPasswordNeededError:
                login_state["step"] = "password"
                await control_bot.send_message(
                    sender_id,
                    "На аккаунте включена двухэтапная аутентификация. Пришлите "
                    "пароль 2FA; сообщение будет сразу удалено.",
                )
                return
            except errors.PhoneCodeInvalidError:
                await control_bot.send_message(
                    sender_id, "Код неверный. Пришлите новый код ещё раз."
                )
                return
            except errors.PhoneCodeExpiredError:
                login_state.clear()
                login_state["step"] = "idle"
                await control_bot.send_message(
                    sender_id, "Код истёк. Запустите /login заново."
                )
                return
            login_state.clear()
            login_state["step"] = "idle"
            await control_bot.send_message(
                sender_id, "Аккаунт подключён. Теперь используйте /migrate."
            )
            return

        if step == "password":
            password = text
            await delete_sensitive(event)
            try:
                await user_client.sign_in(password=password)
            except errors.PasswordHashInvalidError:
                await control_bot.send_message(
                    sender_id, "Пароль неверный. Попробуйте ещё раз."
                )
                return
            login_state.clear()
            login_state["step"] = "idle"
            await control_bot.send_message(
                sender_id, "Аккаунт подключён. Теперь используйте /migrate."
            )
            return

        await event.respond("Неизвестная команда. Используйте /help.")

    @control_bot.on(events.CallbackQuery)
    async def on_callback(event: events.CallbackQuery.Event) -> None:
        if owner_id is None or event.sender_id != owner_id:
            await event.answer("Нет доступа", alert=True)
            return

        data = event.data.decode("utf-8", errors="ignore")
        if data == "cancel":
            if migration_lock.locked():
                await event.answer("Перенос уже выполняется", alert=True)
                return
            migration_state.clear()
            await event.answer("Отменено")
            await event.edit("Операция отменена.")
            return

        if data == "run":
            if migration_lock.locked():
                await event.answer("Перенос уже выполняется", alert=True)
                return
            await event.answer("Запускаю")
            await event.edit("Запуск переноса…")
            await run_migration(event.sender_id)
            return

        parts = data.split(":")
        if len(parts) != 3:
            await event.answer()
            return
        action, stage, value = parts

        if action == "noop":
            await event.answer()
            return

        if action == "page" and stage in {"source", "target"}:
            text, buttons = channel_keyboard(stage, int(value))
            await event.answer()
            await event.edit(text, buttons=buttons)
            return

        if action != "pick" or stage not in {"source", "target"}:
            await event.answer()
            return

        dialog = channel_cache.get(int(value))
        if dialog is None:
            await event.answer("Канал больше не найден", alert=True)
            return

        if stage == "source":
            migration_state["source"] = dialog
            migration_state["stage"] = "target"
            text, buttons = channel_keyboard("target", 0)
            await event.answer(f"Источник: {channel_title(dialog)}")
            await event.edit(text, buttons=buttons)
            return

        source = migration_state.get("source")
        if not isinstance(source, Dialog):
            await event.answer("Сначала выберите источник", alert=True)
            return
        if dialog.entity.id == source.entity.id:
            await event.answer("Каналы должны быть разными", alert=True)
            return

        migration_state["target"] = dialog
        await event.answer(f"Канал 2: {channel_title(dialog)}")
        await prepare_pairs(event)

    print("Управляющий Telegram-бот запущен.")
    try:
        await control_bot.run_until_disconnected()
    finally:
        await user_client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass