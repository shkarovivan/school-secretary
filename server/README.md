# School Secretary Server (этап 1: интерфейс-бот + школьные чаты MAX)

Интерфейс выбирается по токену в `.env`: сейчас **Telegram** (временно,
пока нет MAX-бота), позже MAX-бот — код транспорта взаимозаменяем.

## Установка

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить TELEGRAM_BOT_TOKEN и BRIDGE_PHONE
```

## Интерфейс Telegram (текущий)

1. В Telegram: @BotFather → `/newbot` → имя и username → скопировать токен.
2. Токен в `server/.env`: `TELEGRAM_BOT_TOKEN=...`.

## Разовый вход userbot-профиля (чтение школьных чатов)

```bash
python -m school_secretary.login        # SMS-код + 2FA-пароль в консоли
# или
python -m school_secretary.login --qr   # QR в консоли, сканировать приложением MAX
```

Сессия сохраняется в `profiles/default/session.db` и переживает перезапуски.

## Запуск

```bash
python -m school_secretary.app
```

Поднимает бот (long polling) + раз в 10 минут синхронизирует школьные чаты
в SQLite (`data/secretary.db`).

## Команды бота

- `/start` — первый вошедший становится владельцем
- `/chats` — список школьных чатов
- `/chat 9Д` — последние 15 сообщений чата (по номеру/названию)
- `/find деньги` — поиск по истории всех чатов
- `/sync` — внеплановая синхронизация
- `/status` — состояние базы и последней синхронизации

## Настройка школьных чатов

`SCHOOL_CHAT_FILTER` в `.env` — подстроки названий через запятую
(например `9Д,4Э,родител`). Пусто — синхронизируются все групповые чаты.

## Конфиденциальность

Userbot-профиль используется ТОЛЬКО для чтения; вся отправка — через
официального бота. Сессии и база лежат в `profiles/` и `data/`
(в .gitignore).
