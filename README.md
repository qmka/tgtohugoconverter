# tg to hugo converter

Инструменты для экспорта постов из Telegram-канала и конвертации их в формат статического сайта [Hugo](https://gohugo.io/).

## Возможности

- Экспорт сообщений из публичных и приватных каналов Telegram в формате **NDJSON**.
- Поддержка текстов, форматирования (жирный, курсив, ссылки, кодовые блоки и т.д.).
- Загрузка вложений (изображения и альбомы).
- Склейка сообщений одного дня в один пост Hugo (удобно, если в Telegram текст разбит лимитом).
- Конвертация NDJSON → Markdown-файлы для Hugo:
  - правильный front matter (TOML);
  - автоматическая генерация slug;
  - вставка картинок в начало поста;
  - переписывание внутренних ссылок на `relref`;
  - удаление эмодзи из заголовков.

Обратите внимание, что если в канале много постов в один день, то все они склеются.
Выбор функциональности (клеить или нет) пока отсутствует, т.к. скрипты делались под эту особенность моего канала.

## Установка

1. Клонируй репозиторий:
```
git clone https://github.com/qmka/tgtohugoconverter.git
cd tgtohugoconverter
```
2. Установи зависимости через uv:
```
uv sync
```

3. Создай .env файл в корне проекта и добавь туда свои ключи:
```
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
```
API-ключи можно получить в my.telegram.org.

## Использование
### 1. Экспорт сообщений из Telegram
    
Скрипт: *fetch_telegram_posts.py*

```
uv run python fetch_telegram_posts.py --channel name --outdir ./tg_export --limit 20 --media
```

Флаги:

```
--channel — канал @username или https://t.me/...

--outdir — каталог, куда сохранить messages.ndjson + медиа.

--limit N — количество сообщений, если не указано — все.

--media — скачать вложенные изображения/файлы.

--since YYYY-MM-DD — дата начала выборки.

--until YYYY-MM-DD — дата конца выборки.

--latest — взять последние N сообщений.

--from-start — брать посты от начала канала, т.к по умолчанию - с конца.
```

Примеры:
```
# Последние 50 постов с медиа
uv run python fetch_telegram_posts.py --channel name --outdir ./tg_export --limit 50 --media --latest

# Посты за август 2024
uv run python fetch_telegram_posts.py --channel name --outdir ./tg_export --since 2024-08-01 --until 2024-09-01
```

### 2. Конвертация в Hugo

Скрипт: *tg2hugo.py*

```
uv run python tg2hugo.py --ndjson ./tg_export/messages.ndjson --out ./site/content/blog --static ./site/static/images
```
Флаги:
```
--ndjson FILE — путь к messages.ndjson.

--out DIR — каталог для Markdown-файлов Hugo.

--static DIR — каталог для картинок Hugo.

--tz Europe/Amsterdam — часовой пояс для дат (по умолчанию UTC).

--image-placement top|bottom — куда вставлять изображения (по умолчанию top).

--source-link — добавлять ссылку на оригинальный пост в конце.

--dry-run — ничего не писать на диск, только проверить обработку.
```
### 3. Настройка Hugo

После конвертации файлы появятся в:
```
./site/content/blog — Markdown-посты.

./site/static/images — изображения.
```
При необходимости замените /blog на /posts (как в стандартной теме Hugo)

Дальше можно проверять сайт Hugo стандартной командой:
```
hugo server --buildDrafts
```