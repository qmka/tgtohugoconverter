import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl import types as tl_types
from telethon.tl.types import Message

try:
    load_dotenv(dotenv_path=Path(".env"))
except Exception:
    pass


def _parse_iso_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    # поддержим YYYY-MM-DD и YYYY-MM-DDTHH:MM:SS
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.fromisoformat(s + "T00:00:00")
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt


def extract_links(msg: Message) -> list[dict]:
    links: list[dict] = []
    text = msg.message or ""

    # 1) entities
    if msg.entities:
        for e in msg.entities:
            try:
                if isinstance(e, tl_types.MessageEntityTextUrl) and getattr(
                    e, "url", None
                ):
                    links.append(
                        {
                            "url": e.url,
                            "text": (
                                text[e.offset : e.offset + e.length]
                                if e.length
                                else None
                            ),
                            "offset": e.offset,
                            "length": e.length,
                            "source": "entity",
                        }
                    )
                elif isinstance(e, tl_types.MessageEntityUrl):
                    # "голый" URL в тексте
                    url = text[e.offset : e.offset + e.length]
                    links.append(
                        {
                            "url": url,
                            "text": url,
                            "offset": e.offset,
                            "length": e.length,
                            "source": "entity",
                        }
                    )
            except Exception:
                # не валимся, просто пропускаем кривые сущности
                pass

    # 2) кнопки (reply_markup может быть None)
    rm = getattr(msg, "reply_markup", None)
    if rm and getattr(rm, "rows", None):
        for row in rm.rows:
            for btn in getattr(row, "buttons", []) or []:
                # у разных типов кнопок атрибут может называться по-разному, но обычно "url"
                url = getattr(btn, "url", None)
                if url:
                    # caption/текст у кнопки может прятаться в .text / .data — возьмём, если есть
                    btn_text = getattr(btn, "text", None) or None
                    links.append(
                        {
                            "url": url,
                            "text": btn_text,
                            "source": "button",
                        }
                    )

    return links


def sanitize_channel_name(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^https?://t\.me/", "", raw)
    raw = raw.strip("@/")
    return raw


def entity_to_dict(e) -> dict:
    d = {"type": e.__class__.__name__, "offset": e.offset, "length": e.length}
    # некоторые entity имеют доп. поля
    for attr in ("url", "language", "user_id", "document_id", "custom_emoji_id"):
        if hasattr(e, attr) and getattr(e, attr) is not None:
            d[attr] = getattr(e, attr)
    return d


def msg_to_record(
    msg: Message, channel_username: str, media_paths: list[str] | None
) -> dict:
    link = f"https://t.me/{channel_username}/{msg.id}" if channel_username else None
    return {
        "id": msg.id,
        "grouped_id": getattr(msg, "grouped_id", None),
        "date_utc": msg.date.isoformat() if msg.date else None,
        "views": getattr(msg, "views", None),
        "forwards": getattr(msg, "forwards", None),
        "replies": (msg.replies.replies if msg.replies else None),
        "link": link,
        "raw_text": msg.message or "",
        "text_markdown": getattr(msg, "text_markdown", None) or "",
        "text_html": getattr(msg, "text_html", None) or "",
        "entities": [entity_to_dict(e) for e in (msg.entities or [])],
        "links": extract_links(msg),
        "has_media": bool(msg.media),
        "media_files": media_paths or [],
        "is_pinned": bool(getattr(msg, "pinned", False)),
        "is_forward": bool(msg.fwd_from),
        "post_author": getattr(msg, "post_author", None),
    }


async def fetch_channel(
    channel: str,
    outdir: Path,
    *,
    limit: int | None = None,
    since: str | None = None,  # "YYYY-MM-DD" или "YYYY-MM-DDTHH:MM:SS"
    until: str | None = None,  # "YYYY-MM-DD" или "YYYY-MM-DDTHH:MM:SS"
    take_from: str = "end",  # "end" (новое→старое) или "start" (старое→новое)
    media: bool = False,
    overwrite_media: bool = False,
    session: str = ".session_telegram",
    api_id: int | None = None,
    api_hash: str | None = None,
):
    """
    Экспорт сообщений канала в NDJSON (messages.ndjson) + медиа (в outdir/media).

    Пример вызова:
      await fetch_channel("channelname", Path("tg_export"), limit=20, since="2024-08-01", until="2024-08-31",
                          take_from="end", media=True, overwrite_media=False, session=".session_telegram")
    """
    # --- утилиты ---

    def _parse_iso_dt(s: str | None) -> datetime | None:
        """Поддержка YYYY-MM-DD и YYYY-MM-DDTHH:MM:SS; приводим к UTC-aware."""
        if not s:
            return None
        try:
            if "T" in s:
                dt = datetime.fromisoformat(s)
            else:
                dt = datetime.fromisoformat(s + "T00:00:00")
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt

    def _normalize_channel(s: str) -> str:
        s = s.strip()
        if s.startswith("https://t.me/"):
            s = s[len("https://t.me/") :]
            s = s.split("/")[0]
        if s.startswith("@"):
            s = s[1:]
        return s

    # --- подготовка окружения/клиента ---

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    media_dir = outdir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    # API-ключи: либо переданы параметрами, либо из переменных окружения
    if api_id is None:
        api_id = int(os.getenv("TELEGRAM_API_ID") or 0)
    if api_hash is None:
        api_hash = os.getenv("TELEGRAM_API_HASH")

    if not api_id or not api_hash:
        raise RuntimeError(
            "Не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH (env) или параметры api_id/api_hash"
        )

    session_path = Path(session)

    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.start()

    channel_username = _normalize_channel(channel)
    entity = await client.get_entity(channel_username)

    # --- даты/направление/лимиты ---

    since_dt = _parse_iso_dt(since)  # нижняя граница (включительно)
    until_dt = _parse_iso_dt(until)  # верхняя граница (включительно)

    # если задан период, лимит применяем ПОСЛЕ фильтрации; иначе можно отдать limit в итератор
    iter_limit = None if (since_dt or until_dt) else (limit or None)
    direction_end = take_from == "end"  # True: новое→старое; False: старое→новое

    async def _iter_msgs():
        if direction_end:
            async for m in client.iter_messages(entity, limit=iter_limit):
                yield m
        else:
            async for m in client.iter_messages(entity, reverse=True, limit=iter_limit):
                yield m

    def _in_range(d: datetime) -> tuple[bool, str]:
        # d — aware UTC от Telethon
        if until_dt and d > until_dt:
            return False, "too_new"
        if since_dt and d < since_dt:
            return False, "too_old"
        return True, ""

    ndjson_path = outdir / "messages.ndjson"
    f = ndjson_path.open("w", encoding="utf-8")

    count_written = 0
    try:
        async for msg in _iter_msgs():
            if not isinstance(msg, Message):
                continue
            if not (msg.message or msg.media):
                continue

            ok, why = _in_range(msg.date) if (since_dt or until_dt) else (True, "")
            if since_dt or until_dt:
                if direction_end:
                    # новое -> старое
                    if not ok:
                        if why == "too_new":
                            continue  # ещё слишком «свежее» — идём дальше
                        if why == "too_old":
                            break  # ушли ниже интервала — дальше только старее
                else:
                    # старое -> новое
                    if not ok:
                        if why == "too_old":
                            continue  # ещё слишком старое — идём дальше
                        if why == "too_new":
                            break  # выше интервала — дальше только новее

            # --- скачивание медиа ---
            media_paths: list[str] = []
            if media and msg.media:
                try:
                    download_target = None
                    if overwrite_media:
                        # сделаем предсказуемое имя <id>.<ext>, перезапишем при необходимости
                        ext = ""
                        try:
                            fname = (
                                getattr(getattr(msg, "file", None), "name", "") or ""
                            )
                            ext = Path(fname).suffix or ""
                        except Exception:
                            ext = ""
                        download_target = media_dir / f"{msg.id}{ext}"
                        if download_target.exists():
                            try:
                                download_target.unlink()
                            except Exception:
                                pass

                    path = await msg.download_media(
                        file=download_target or (media_dir / f"{msg.id}")
                    )
                    if path:
                        # запишем путь относительным к outdir
                        media_paths.append(os.path.relpath(path, outdir))
                        print(f"[media] msg {msg.id} -> {path}")
                except Exception as e:
                    print(f"[warn] media for msg {msg.id} skipped: {e}")

            # --- запись строки в NDJSON ---
            rec = msg_to_record(msg, channel_username, media_paths)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count_written += 1

            # если задавали лимит вместе с периодом — применяем ПОСЛЕ фильтрации
            if (since_dt or until_dt) and limit and count_written >= limit:
                break

    except FloodWaitError as e:
        print(f"Flood wait: retry after {e.seconds} seconds")
    finally:
        f.close()
        await client.disconnect()

    print(f"Готово: записано {count_written} сообщений в {ndjson_path}")


def parse_args():
    p = argparse.ArgumentParser(
        description="Export Telegram channel messages with formatting"
    )
    p.add_argument(
        "--channel", required=True, help="username (@name) или ссылка t.me/xxx"
    )
    p.add_argument("--outdir", default="export", help="папка для выгрузки")
    p.add_argument(
        "--limit", type=int, default=0, help="сколько постов брать (0 = все)"
    )
    p.add_argument("--since", type=str, default=None, help="с какой даты (YYYY-MM-DD)")
    p.add_argument("--until", type=str, default=None, help="по какую дату (YYYY-MM-DD)")
    p.add_argument("--media", action="store_true", help="скачивать вложения")
    p.add_argument(
        "--overwrite-media",
        action="store_true",
        help="перезаписывать уже скачанные медиа",
    )
    p.add_argument(
        "--take-from",
        choices=["start", "end"],
        default="end",
        help="откуда считать limit: start=первые (старые), end=последние (новые)",
    )
    p.add_argument(
        "--session",
        type=str,
        default=".session_telegram",
        help="путь к файлу Telethon-сессии (по умолчанию ./.session_telegram)",
    )
    return p.parse_args()


def to_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


if __name__ == "__main__":
    import asyncio
    from pathlib import Path

    args = parse_args()
    outdir = Path(args.outdir)

    asyncio.run(
        fetch_channel(
            channel=args.channel,
            outdir=outdir,
            limit=(args.limit if args.limit and args.limit > 0 else None),
            since=getattr(
                args, "since", None
            ),  # строки вида YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS
            until=getattr(args, "until", None),
            take_from=getattr(
                args, "take_from", "end"
            ),  # "end" (новое→старое) или "start"
            media=getattr(args, "media", False),  # РАНЬШЕ было download_media
            overwrite_media=getattr(args, "overwrite_media", False),
            session=getattr(args, "session", ".session_telegram"),
            # api_id / api_hash можно не передавать — возьмутся из env TELEGRAM_API_ID / TELEGRAM_API_HASH
        )
    )
