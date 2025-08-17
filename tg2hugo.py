import argparse
import json
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from markdownify import markdownify as html2md
from slugify import slugify

# ---------- Константы / утилиты ----------

IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# Удаление эмодзи из строк (для title)
_EMOJI_RE = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"  # флаги
    "\U0001f300-\U0001f5ff"  # символы и пиктограммы
    "\U0001f600-\U0001f64f"  # смайлы
    "\U0001f680-\U0001f6ff"  # транспорт/карты
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\U00002700-\U000027bf"  # dingbats
    "\U00002600-\U000026ff"  # разное
    "]+",
    flags=re.UNICODE,
)


def local_day_key(msg: dict, tz_name: str) -> str | None:
    """
    Вернёт ключ для группировки сообщений по дню (YYYY-MM-DD) в локальной TZ.
    """
    date_utc = msg.get("date_utc")
    if not date_utc:
        return None
    try:
        dt_utc = datetime.fromisoformat(date_utc.replace("Z", "+00:00"))
        dt_local = dt_utc.astimezone(ZoneInfo(tz_name))
        return dt_local.strftime("%Y-%m-%d")
    except Exception:
        return None


def strip_emojis_and_spaces(s: str) -> str:
    if not s:
        return s
    # уберём эмодзи
    s = _EMOJI_RE.sub("", s)
    # уберём variation selectors и zero-width joiners на всякий
    s = re.sub(r"[\uFE0E\uFE0F\u200D]", "", s)
    # схлопнем лишние пробелы
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def read_ndjson(path: Path) -> Iterable[dict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def first_nonempty_line(text: str) -> str:
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s:
            return s
    return ""


def reserve_unique_slug(base: str, used: set[str]) -> str:
    s = base
    i = 2
    while s in used:
        s = f"{base}-{i}"
        i += 1
    used.add(s)
    return s


def _has_text(rec: dict) -> bool:
    return bool(
        (
            rec.get("text_markdown")
            or rec.get("text_html")
            or rec.get("raw_text")
            or ""
        ).strip()
    )


def _parse_dt(s: str) -> datetime:
    # s вида "2025-07-05T16:25:50+00:00"
    return datetime.fromisoformat(s)


TG_POST_ID_RE = re.compile(r"https?://t\.me/(?:c/\d+/)?[\w_]+/(\d+)")


def tg_ids_from_links_list(links: list[dict]) -> list[int]:
    ids: list[int] = []
    for item in links or []:
        url = item.get("url") or ""
        m = TG_POST_ID_RE.search(url)
        if m:
            try:
                ids.append(int(m.group(1)))
            except ValueError:
                pass
    # уникализируем, сохраняя порядок
    seen = set()
    uniq = []
    for i in ids:
        if i not in seen:
            uniq.append(i)
            seen.add(i)
    return uniq


TITLE_PATTERNS = [
    # «Название» — Автор
    re.compile(r"^[\"“«](.+?)[\"”»]\s*[-—:]\s*(.+)$"),
    # Название — Автор
    re.compile(r"^(.+?)\s*[-—:]\s*(.+)$"),
    # «Название»
    re.compile(r"^[\"“«](.+?)[\"”»]\s*$"),
]


def extract_title(text: str, strict: bool) -> str | None:
    line = first_nonempty_line(text)
    if not line:
        return None
    if strict:
        # Только “Название — Автор”
        for pat in TITLE_PATTERNS[:2]:
            m = pat.match(line)
            if m:
                return f"{m.group(1).strip()} — {m.group(2).strip()}"
        return None
    else:
        for i, pat in enumerate(TITLE_PATTERNS):
            m = pat.match(line)
            if m:
                if i in (0, 1):
                    return f"{m.group(1).strip()} — {m.group(2).strip()}"
                else:
                    return m.group(1).strip()
        # Фолбэк — первая строка, но укоротим
        return line[:140].strip()


def to_site_dt(iso_utc: str, tz_name: str) -> datetime:
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ZoneInfo(tz_name))


def iso_with_offset(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def ensure_unique_path(base: Path) -> Path:
    if not base.exists():
        return base
    stem, suffix = base.stem, base.suffix
    i = 2
    while True:
        cand = base.with_name(f"{stem}-{i}{suffix}")
        if not cand.exists():
            return cand
        i += 1


def make_slug(title: str, extra: str = "") -> str:
    s = slugify(title, lowercase=True, max_length=80, allow_unicode=False)
    if not s:
        s = "post"
    if extra:
        s = f"{s}-{slugify(extra, lowercase=True)}"
    return s


def normalize_markdown(md: str) -> str:
    md = md.replace("\r\n", "\n")
    md = re.sub(r"\[(.*?)\]\((\s*)\)", r"[\1](#)", md)
    return md.strip() + "\n"


# --- UTF-16 safe slicing для телеграмных offsets/length (они в UTF-16 code units) ---
def _utf16_slice(s: str, start_units: int, end_units: int) -> str:
    # Кодируем в UTF-16-LE без BOM: 2 байта на code unit
    b = s.encode("utf-16-le")
    a = start_units * 2
    b_end = end_units * 2
    return b[a:b_end].decode("utf-16-le")


def _utf16_splice(s: str, start_units: int, end_units: int, insert: str) -> str:
    b = s.encode("utf-16-le")
    a = start_units * 2
    b_end = end_units * 2
    out = b[:a] + insert.encode("utf-16-le") + b[b_end:]
    return out.decode("utf-16-le")


# --- Строим Markdown из raw_text + entities ---

TG_LINK_MD_RE = re.compile(r"\((https?://t\.me/(?:c/\d+/)?[\w_]+/(\d+))\)")

# Ищем три варианта: [текст](url), <url>, голый url
TG_MD_LINK_RE = re.compile(
    r"\[(?P<text>[^\]]+)\]\((?P<url>https?://t\.me/(?:c/\d+/)?[\w_]+/(?P<id>\d+))\)"
)
TG_AUTOLINK_RE = re.compile(r"<(?P<url>https?://t\.me/(?:c/\d+/)?[\w_]+/(?P<id>\d+))>")
TG_BARE_URL_RE = re.compile(
    r"(?<!\()(?<!<)(?P<url>https?://t\.me/(?:c/\d+/)?[\w_]+/(?P<id>\d+))(?!\))"
)


# Сопоставление entity -> пары маркеров
ENTITY_MARKS = {
    "MessageEntityBold": ("**", "**"),
    "MessageEntityItalic": ("*", "*"),
    "MessageEntityUnderline": ("<u>", "</u>"),  # Hugo ок с HTML-инлайном
    "MessageEntityStrike": ("~~", "~~"),
    "MessageEntitySpoiler": ('<span class="spoiler">', "</span>"),
    "MessageEntityCode": ("`", "`"),
    # Pre оформим как тройные бэктики на отдельной строке:
    "MessageEntityPre": ("```", "```"),
}


def build_markdown_from_entities(raw_text: str, entities: list[dict]) -> str:
    """
    Собираем Markdown из raw_text + entities.
    Поддержка: ссылки (TextUrl/Url), жирный/курсив/подчёрк/зачёрк/спойлер/код/пре.
    offsets/length от Telegram — в UTF-16 code units → используем utf16-сплайсы.
    Замены применяем с конца, чтобы не сдвигать смещения.
    """
    if not raw_text:
        return ""
    if not entities:
        return raw_text

    spans = []
    for e in entities or []:
        et = e.get("type")
        off = e.get("offset")
        ln = e.get("length")
        if off is None or ln is None:
            continue
        start = int(off)
        end = int(off) + int(ln)

        if et == "MessageEntityTextUrl" and e.get("url"):
            url = e["url"]
            txt = _utf16_slice(raw_text, start, end)
            txt = txt.replace("[", "\\[").replace("]", "\\]")
            repl = f"[{txt}]({url})"
            spans.append(("replace", start, end, repl))

        elif et == "MessageEntityUrl":
            url_txt = _utf16_slice(raw_text, start, end)
            repl = f"[{url_txt}]({url_txt})"
            spans.append(("replace", start, end, repl))

        elif et in ENTITY_MARKS:
            open_m, close_m = ENTITY_MARKS[et]
            content = _utf16_slice(raw_text, start, end)
            if et == "MessageEntityPre":
                lang = e.get("language")
                fence = f"```{lang}" if lang else "```"
                repl = f"{fence}\n{content}\n```"
            else:
                repl = f"{open_m}{content}{close_m}"
            spans.append(("replace", start, end, repl))

        # упоминания/хэштеги можно добавить позже при желании

    if not spans:
        return raw_text

    # Применяем с конца (по start), чтобы не ломать последующие offsets
    spans.sort(key=lambda x: x[1], reverse=True)
    out = raw_text
    for _kind, s, e, repl in spans:
        out = _utf16_splice(out, s, e, repl)
    return out


def rewrite_internal_links(md: str, id2relref: dict[int, str]) -> str:
    # [текст](https://t.me/.../123)
    def _sub_md(m: re.Match) -> str:
        pid = int(m.group("id"))
        rel = id2relref.get(pid)
        if not rel:
            return m.group(0)
        text = m.group("text")
        return f'[{text}]({{{{< relref "{rel}" >}}}})'

    # <https://t.me/.../123> → [https://t.me/.../123]({{<relref>}})
    def _sub_auto(m: re.Match) -> str:
        pid = int(m.group("id"))
        rel = id2relref.get(pid)
        if not rel:
            return m.group(0)
        url = m.group("url")
        return f'[{url}]({{{{< relref "{rel}" >}}}})'

    # голый https://t.me/.../123 → [https://t.me/.../123]({{<relref>}})
    def _sub_bare(m: re.Match) -> str:
        pid = int(m.group("id"))
        rel = id2relref.get(pid)
        if not rel:
            return m.group(0)
        url = m.group("url")
        return f'[{url}]({{{{< relref "{rel}" >}}}})'

    md = TG_MD_LINK_RE.sub(_sub_md, md)
    md = TG_AUTOLINK_RE.sub(_sub_auto, md)
    md = TG_BARE_URL_RE.sub(_sub_bare, md)
    return md


def copy_media_to_static(
    media_paths: list[str],
    out_static_dir: Path,
    ts_for_name: datetime,
    slug_for_name: str,
) -> list[str]:
    out_static_dir.mkdir(parents=True, exist_ok=True)
    site_paths: list[str] = []
    for idx, m in enumerate(media_paths, start=1):
        src = Path(m)
        if not src.exists():
            print(f"[warn] media not found: {src}")
            continue
        ext = src.suffix.lower()
        stamp = ts_for_name.strftime("%Y-%m-%d-%H-%M-%S")
        base = (
            f"{stamp}-{slug_for_name}-{idx}{ext}"
            if ext
            else f"{stamp}-{slug_for_name}-{idx}"
        )
        dst = out_static_dir / base
        dst = ensure_unique_path(dst)
        shutil.copy2(src, dst)
        if ext in IMG_EXTS:
            site_paths.append(f"/images/{dst.name}")
    return site_paths


def compose_front_matter_toml(title: str, date_str: str) -> str:
    safe_title = title.replace('"', '\\"')
    return (
        "+++\n"
        f'title = "{safe_title}"\n'
        f'date = "{date_str}"\n'
        "tags = []\n"
        "+++\n\n"
    )


def pick_album_parent(records: list[dict]) -> dict:
    """
    Выбираем родителя альбома:
    1) запись с непустым текстом (text_markdown/html/raw_text),
    2) иначе — с минимальным id.
    """

    def has_text(rec: dict) -> bool:
        return bool(
            (
                rec.get("text_markdown")
                or rec.get("text_html")
                or rec.get("raw_text")
                or ""
            ).strip()
        )

    with_text = [r for r in records if has_text(r)]
    if with_text:
        # если вдруг несколько — берём с минимальным id среди «текстовых»
        return min(with_text, key=lambda r: int(r["id"]))
    return min(records, key=lambda r: int(r["id"]))


def merge_album_media(records: list[dict]) -> list[str]:
    """Собираем все media_files альбома по id по возрастанию."""
    ordered = sorted(records, key=lambda r: int(r["id"]))
    out = []
    for r in ordered:
        for p in r.get("media_files") or []:
            out.append(p)
    return out


@dataclass
class Options:
    ndjson: Path
    out_md_dir: Path
    static_images_dir: Path
    tz: str
    image_placement: str  # top|bottom|none
    source_link: bool
    strict_title: bool
    dry_run: bool
    skip_empty: bool
    append_id: bool
    overwrite: bool


# ---------- Основная логика ----------


def convert_one(msg: dict, opts: Options):
    """
    Возвращает (путь_к_md_файлу, содержимое) или None (пропуск).
    """
    # Дата
    date_iso = msg.get("date_utc")
    if not date_iso:
        return None
    dt_site = to_site_dt(date_iso, opts.tz)
    date_str = iso_with_offset(dt_site)

    # Текст → Markdown
    text_md = (msg.get("text_markdown") or "").strip()
    text_html = (msg.get("text_html") or "").strip()
    raw_text = (msg.get("raw_text") or "").strip()

    if text_md:
        body_md = text_md
    elif text_html:
        body_md = html2md(text_html, strip=["span"])
    else:
        body_md = raw_text

    body_md = normalize_markdown(body_md)
    if opts.skip_empty and not body_md.strip():
        return None

    # Заголовок
    title = extract_title(body_md, strict=opts.strict_title)
    if not title:
        title = f"Пост из Telegram от {dt_site.strftime('%Y-%m-%d %H:%M')}"
    title = strip_emojis_and_spaces(title)

    # Имя файла
    base_slug = make_slug(title)
    if opts.append_id:
        base_slug = f"{base_slug}-tg{msg.get('id', '')}"
    md_path = opts.out_md_dir / f"{base_slug}.md"
    if not opts.overwrite:
        md_path = ensure_unique_path(md_path)

    # Медиа
    media_base = opts.ndjson.parent  # <--- базовая папка экспорта (tg_export)
    media_files = [
        str((media_base / p).resolve()) for p in (msg.get("media_files") or [])
    ]
    site_image_links = copy_media_to_static(
        media_files,
        out_static_dir=opts.static_images_dir,
        ts_for_name=dt_site,
        slug_for_name=base_slug,
    )

    # Тело: картинки + текст + источник (опц.)
    chunks = []
    if opts.image_placement == "top" and site_image_links:
        for link in site_image_links:
            chunks.append(f"![]({link})")
        chunks.append("")

    chunks.append(body_md.strip())

    if opts.image_placement == "bottom" and site_image_links:
        chunks.append("")
        for link in site_image_links:
            chunks.append(f"![]({link})")

    if opts.source_link:
        src = msg.get("link")
        if src:
            chunks.append("")
            chunks.append(f"Источник: {src}")

    content_body = "\n".join(chunks).rstrip() + "\n"

    # Front matter
    front = compose_front_matter_toml(title=title, date_str=date_str)
    return md_path, front + content_body


def convert_one_with_links(
    msg: dict, opts: Options, pre_title: str, pre_slug: str, id2relref: dict[int, str]
):
    date_iso = msg.get("date_utc")
    if not date_iso:
        return None
    dt_site = to_site_dt(date_iso, opts.tz)
    date_str = iso_with_offset(dt_site)

    # Текст → Markdown
    text_md = (msg.get("text_markdown") or "").strip()
    text_html = (msg.get("text_html") or "").strip()
    raw_text = (msg.get("raw_text") or "").strip()
    entities = msg.get("entities") or []
    has_rich_entities = any(
        e.get("type")
        in (
            "MessageEntityTextUrl",
            "MessageEntityUrl",
            "MessageEntityBold",
            "MessageEntityItalic",
            "MessageEntityUnderline",
            "MessageEntityStrike",
            "MessageEntitySpoiler",
            "MessageEntityCode",
            "MessageEntityPre",
        )
        for e in entities
    )

    if has_rich_entities:
        body_md = build_markdown_from_entities(raw_text, entities)
    else:
        if text_md:
            body_md = text_md
        elif text_html:
            body_md = html2md(text_html, strip=["span"])
        else:
            body_md = raw_text

    # Если в text_markdown/HTML ссылка потерялась — восстановим из raw_text+entities
    # Критерий простой: если есть хотя бы один MessageEntityTextUrl/Url — строим заново.
    has_link_entities = any(
        e.get("type") in ("MessageEntityTextUrl", "MessageEntityUrl") for e in entities
    )
    if has_link_entities:
        body_md = build_markdown_from_entities(raw_text, entities)

    body_md = normalize_markdown(body_md)
    if opts.skip_empty and not body_md.strip():
        return None

    # Заголовок и slug берём из pass1 (детерминируемо)
    title = pre_title
    base_slug = pre_slug
    if opts.append_id:
        base_slug = f"{base_slug}-tg{msg.get('id', '')}"

    # Переписываем внутренние ссылки на relref
    body_md = rewrite_internal_links(body_md, id2relref)
    body_md_before = body_md
    body_md = rewrite_internal_links(body_md, id2relref)

    # Если в тексте не было markdown-ссылок, а в экспортных links есть t.me/<ID>,
    # добавим внизу "См. также" с relref-ами.
    if body_md == body_md_before:
        # есть ли ссылочные ID в тексте (по идее уже заменили бы) — если нет, глянем buttons
        link_ids = tg_ids_from_links_list(msg.get("links") or [])
        extras = []
        for pid in link_ids:
            if pid in id2relref:
                # добавим только те, что НЕ встречаются как голые урлы в тексте
                rel = id2relref[pid]
                # простая проверка: если исходный t.me/<id> нигде не фигурирует в тексте
                # (в любой из форм), только тогда добавим «См. также»
                if not re.search(rf"t\.me/(?:c/\d+/)?[\w_]+/{pid}\b", body_md):
                    extras.append(f'- {{< relref "{rel}" >}}')
        if extras:
            body_md += "\n\n**См. также:**\n" + "\n".join(extras) + "\n"

    # Имя файла
    md_path = opts.out_md_dir / f"{base_slug}.md"
    if not opts.overwrite:
        md_path = ensure_unique_path(md_path)

    # Медиа
    media_files = msg.get("media_files") or []
    site_image_links = copy_media_to_static(
        media_files,
        out_static_dir=opts.static_images_dir,
        ts_for_name=dt_site,
        slug_for_name=base_slug,
    )

    # Тело: картинки + текст + (источник опц.)
    chunks = []
    if opts.image_placement == "top" and site_image_links:
        for link in site_image_links:
            chunks.append(f"![]({link})")
        chunks.append("")

    chunks.append(body_md.strip())

    if opts.image_placement == "bottom" and site_image_links:
        chunks.append("")
        for link in site_image_links:
            chunks.append(f"![]({link})")

    if opts.source_link:
        src = msg.get("link")
        if src:
            chunks.append("")
            chunks.append(f"Источник: {src}")

    content_body = "\n".join(chunks).rstrip() + "\n"

    # Front matter
    front = compose_front_matter_toml(title=title, date_str=date_str)
    return md_path, front + content_body


def convert_one_with_links_and_finalslug(
    msg: dict, opts: Options, pre_title: str, final_slug: str, id2relref: dict[int, str]
):
    date_iso = msg.get("date_utc")
    if not date_iso:
        return None
    dt_site = to_site_dt(date_iso, opts.tz)
    date_str = iso_with_offset(dt_site)

    # --- текст -> markdown (учитываем entities) ---
    text_md = (msg.get("text_markdown") or "").strip()
    text_html = (msg.get("text_html") or "").strip()
    raw_text = (msg.get("raw_text") or "").strip()
    entities = msg.get("entities") or []

    has_rich_entities = any(
        e.get("type")
        in (
            "MessageEntityTextUrl",
            "MessageEntityUrl",
            "MessageEntityBold",
            "MessageEntityItalic",
            "MessageEntityUnderline",
            "MessageEntityStrike",
            "MessageEntitySpoiler",
            "MessageEntityCode",
            "MessageEntityPre",
        )
        for e in entities
    )

    if has_rich_entities:
        body_md = build_markdown_from_entities(raw_text, entities)
    else:
        if text_md:
            body_md = text_md
        elif text_html:
            body_md = html2md(text_html, strip=["span"])
        else:
            body_md = raw_text

    body_md = normalize_markdown(body_md)
    body_md = rewrite_internal_links(body_md, id2relref).strip()

    # --- заголовок ---
    try:
        title = strip_emojis_and_spaces(pre_title.strip())
    except NameError:
        title = pre_title.strip()

    # --- имя файла ---
    md_path = opts.out_md_dir / f"{final_slug}.md"

    # --- медиа: абсолютные пути -> копирование -> ссылки ---
    media_files = [
        str((Path(opts.ndjson).parent / Path(f)).resolve())
        for f in (msg.get("media_files") or [])
    ]
    site_image_links = copy_media_to_static(
        media_files,
        out_static_dir=opts.static_images_dir,
        ts_for_name=dt_site,
        slug_for_name=final_slug,
    )

    # --- сборка тела: картинки сразу под +++ , затем текст, затем (опц.) источник ---
    parts: list[str] = []

    if site_image_links:
        parts.append("\n".join(f"![]({link})" for link in site_image_links))

    if body_md:
        parts.append(body_md)

    if opts.source_link:
        src = (msg.get("link") or "").strip()
        if src:
            # добавим «Источник: …» без лишних пустых строк после
            parts.append(f"Источник: {src}")

    content_body = "\n\n".join(parts).rstrip()  # <- никаких пустых строк в конце

    front = compose_front_matter_toml(
        title=title, date_str=date_str
    )  # оканчивается на \n\n
    return md_path, front + content_body


def compute_title_and_slug_for_msg(msg: dict, opts: Options) -> tuple[str, str]:
    # дата не нужна для слага, но нужна для fallback-титла
    date_iso = msg.get("date_utc")
    dt_site = (
        to_site_dt(date_iso, opts.tz) if date_iso else datetime.now(ZoneInfo(opts.tz))
    )

    text_md = (msg.get("text_markdown") or "").strip()
    text_html = (msg.get("text_html") or "").strip()
    raw_text = (msg.get("raw_text") or "").strip()

    if text_md:
        body_md = text_md
    elif text_html:
        body_md = html2md(text_html, strip=["span"])
    else:
        body_md = raw_text

    body_md = normalize_markdown(body_md)
    title = (
        extract_title(body_md, strict=opts.strict_title)
        or f"Пост из Telegram от {dt_site.strftime('%Y-%m-%d %H:%M')}"
    )
    # режем эмодзи, как договаривались
    try:
        title = strip_emojis_and_spaces(title)
    except NameError:
        # если у тебя ещё нет функции strip_emojis_and_spaces из прошлого шага — добавь её
        pass

    slug = make_slug(title)
    return title, slug


def run(opts: Options) -> None:
    """
    Режим: один .md на календарный день (в TZ из opts.tz).
    Все тексты дня склеиваются, все медиа дня приклеиваются туда же.
    Ссылки t.me/.../<id> переписываются на relref дневного поста.
    """
    opts.out_md_dir.mkdir(parents=True, exist_ok=True)
    opts.static_images_dir.mkdir(parents=True, exist_ok=True)

    # ---- Вспомогалки ----
    def _has_text(m: dict) -> bool:
        return bool(
            (
                m.get("text_markdown") or m.get("text_html") or m.get("raw_text") or ""
            ).strip()
        )

    def _body_markdown_for_msg(m: dict) -> str:
        # Собираем Markdown из raw_text + entities (жирный/курсив/ссылки), иначе fallback на text_markdown/html/raw_text
        raw_text = (m.get("raw_text") or "").strip()
        text_md = (m.get("text_markdown") or "").strip()
        text_ht = (m.get("text_html") or "").strip()
        ents = m.get("entities") or []

        has_rich = any(
            e.get("type")
            in (
                "MessageEntityTextUrl",
                "MessageEntityUrl",
                "MessageEntityBold",
                "MessageEntityItalic",
                "MessageEntityUnderline",
                "MessageEntityStrike",
                "MessageEntitySpoiler",
                "MessageEntityCode",
                "MessageEntityPre",
            )
            for e in ents
        )

        if has_rich:
            md = build_markdown_from_entities(raw_text, ents)
        else:
            md = text_md or (html2md(text_ht, strip=["span"]) if text_ht else raw_text)

        return normalize_markdown(md)

    def _first_line(text: str) -> str:
        for ln in (text or "").splitlines():
            s = ln.strip()
            if s:
                return s
        return ""

    # ---- Читаем экспорт и группируем по дню ----
    all_msgs: list[dict] = list(read_ndjson(opts.ndjson))

    buckets: dict[str, list[dict]] = {}
    for m in all_msgs:
        day = local_day_key(m, opts.tz)
        if not day:
            continue
        buckets.setdefault(day, []).append(m)

    # ---- PASS 1: считаем заголовки/слаги для дней и строим карту id -> relref ----
    used_slugs = {p.stem for p in opts.out_md_dir.glob("*.md")}
    day_prepared: dict[str, dict] = (
        {}
    )  # day -> {title, slug, date_iso, msgs_sorted, media_paths_abs}
    id2relref: dict[int, str] = {}

    for day, recs in buckets.items():
        # сортировка сообщений дня по возрастанию id
        recs_sorted = sorted(recs, key=lambda r: int(r["id"]))

        # заголовок: первая непустая строка первого текстового сообщения, иначе "Фотоальбом от YYYY-MM-DD"
        title = ""
        for r in recs_sorted:
            if _has_text(r):
                title = _first_line(_body_markdown_for_msg(r))
                break
        if not title:
            title = f"Фотоальбом от {day}"
        try:
            title = strip_emojis_and_spaces(title)
        except NameError:
            pass

        base_slug = make_slug(title)
        final_slug = reserve_unique_slug(base_slug, used_slugs)

        # дата для фронтматтера: берём самое раннее сообщение дня (локальная дата уже одинакова)
        first_dt_site = to_site_dt(recs_sorted[0]["date_utc"], opts.tz)
        date_str = iso_with_offset(first_dt_site)

        # Соберём абсолютные пути к медиа всего дня
        media_abs: list[str] = []
        base_dir = Path(opts.ndjson).parent
        for r in recs_sorted:
            for p in r.get("media_files") or []:
                media_abs.append(str((base_dir / p).resolve()))

        # Сохраняем подготовку дня
        day_prepared[day] = {
            "title": title,
            "slug": final_slug,
            "date_str": date_str,
            "msgs_sorted": recs_sorted,
            "media_abs": media_abs,
        }

    # Карта id -> relref на дневной файл
    for _day, info in day_prepared.items():
        rel = f'blog/{info["slug"]}.md'
        for r in info["msgs_sorted"]:
            try:
                id2relref[int(r["id"])] = rel
            except Exception:
                continue

    # ---- PASS 2: генерим по одному .md на день ----
    total = 0
    written = 0

    for day in sorted(day_prepared.keys()):
        info = day_prepared[day]
        title = info["title"]
        final_slug = info["slug"]
        date_str = info["date_str"]
        msgs = info["msgs_sorted"]
        media_abs = info["media_abs"]

        # Сборка тела: склеиваем тексты всех сообщений дня в порядке id
        parts: list[str] = []
        for m in msgs:
            if not _has_text(m):
                continue
            md = _body_markdown_for_msg(m)
            # Переписываем внутр. ссылки t.me/.../<id> на relref дневных файлов
            md = rewrite_internal_links(md, id2relref)
            if md.strip():
                parts.append(md.strip())

        body_md = ""
        if parts:
            body_md = ("\n\n---\n\n").join(parts) + "\n"

        # Копируем все картинки дня в static/images
        dt_site = to_site_dt(msgs[0]["date_utc"], opts.tz)  # метка для имен картинок
        site_image_links = copy_media_to_static(
            media_abs,
            out_static_dir=opts.static_images_dir,
            ts_for_name=dt_site,
            slug_for_name=final_slug,
        )

        # Вставка картинок
        chunks = []
        if opts.image_placement == "top" and site_image_links:
            for link in site_image_links:
                chunks.append(f"![]({link})")
            chunks.append("")
        if body_md:
            chunks.append(body_md.strip())
        if opts.image_placement == "bottom" and site_image_links:
            if body_md:
                chunks.append("")
            for link in site_image_links:
                chunks.append(f"![]({link})")

        content_body = ("\n".join(chunks)).rstrip() + "\n"

        # Front matter и запись
        front = compose_front_matter_toml(title=title, date_str=date_str)
        md_path = opts.out_md_dir / f"{final_slug}.md"

        total += 1
        if opts.dry_run:
            print(f"[DRY] -> {md_path.name}")
            print("     title:", title)
            print("     size :", len(front + content_body), "bytes")
            continue

        md_path.write_text(front + content_body, encoding="utf-8")
        written += 1

    # ---- отчёт ----
    if opts.dry_run:
        print(f"\nDRY RUN: дней {total}, к записи {total}")
    else:
        print(f"\nГотово: дней {total}, записано {written}")


# ---------- CLI ----------


def parse_args() -> Options:
    p = argparse.ArgumentParser(
        description="Convert Telegram export (NDJSON) to Hugo posts"
    )

    p.add_argument(
        "--ndjson",
        type=Path,
        default=Path("tg_export/messages.ndjson"),
        help="путь к messages.ndjson (по умолчанию ./tg_export/messages.ndjson)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path("site/content/blog"),
        help="папка для .md (по умолчанию ./site/content/blog)",
    )
    p.add_argument(
        "--static",
        type=Path,
        default=Path("site/static/images"),
        help="папка static/images хьюго-сайта (по умолчанию ./site/static/images)",
    )

    p.add_argument(
        "--tz",
        default="Europe/Amsterdam",
        help="часовой пояс для даты (по умолчанию Europe/Amsterdam)",
    )
    p.add_argument(
        "--image-placement",
        choices=["top", "bottom", "none"],
        default="bottom",
        help="куда вставлять изображения",
    )
    p.add_argument(
        "--source-link",
        choices=["on", "off"],
        default="off",
        help="добавлять ссылку на пост в TG в конец",
    )
    p.add_argument(
        "--strict-title",
        action="store_true",
        help="извлекать title только формата «Название — Автор»",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="сухой прогон без записи файлов"
    )
    p.add_argument(
        "--skip-empty", action="store_true", help="пропускать посты без текста"
    )
    p.add_argument(
        "--append-id", action="store_true", help="добавлять -tg<ID> к имени файла"
    )
    p.add_argument(
        "--overwrite", action="store_true", help="перезаписывать существующие .md"
    )

    args = p.parse_args()
    return Options(
        ndjson=args.ndjson,
        out_md_dir=args.out,
        static_images_dir=args.static,
        tz=args.tz,
        image_placement=args.image_placement,
        source_link=(args.source_link == "on"),
        strict_title=args.strict_title,
        dry_run=args.dry_run,
        skip_empty=args.skip_empty,
        append_id=args.append_id,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    opts = parse_args()
    run(opts)
