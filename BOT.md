# Деплой бота на VPS — инструкция с нуля

Бот принимает `.jnx` файл прямо в Telegram, конвертирует в `.mbtiles` и отправляет обратно.  
Работает с телефона — curl не нужен.

```
Пользователь         VPS
────────────         ───
отправить .jnx  ──▶  скачать → конвертировать → отправить .mbtiles
получить .mbtiles ◀──
```

Лимит по умолчанию в Telegram Bot API — 50 МБ. Чтобы снять его до 2 ГБ, на VPS запускается локальный Bot API сервер (`telegram-bot-api`).

---

## Шаг 1. Зайти на VPS

```bash
ssh ubuntu@<публичный-IP>
```

---

## Шаг 2. Установить зависимости системы

```bash
sudo apt update && sudo apt install -y \
  python3-pip python3-venv git \
  make cmake g++ gperf zlib1g-dev libssl-dev
```

---

## Шаг 3. Собрать telegram-bot-api

Официальный локальный Bot API сервер от Telegram. Снимает ограничение 50 МБ.

```bash
git clone --recursive https://github.com/tdlib/telegram-bot-api.git ~/telegram-bot-api
cd ~/telegram-bot-api
mkdir build && cd build
cmake -DCMAKE_BUILD_TYPE=Release ..
cmake --build . --target telegram-bot-api
sudo cp telegram-bot-api /usr/local/bin/
cd ~
```

> Сборка занимает ~10–15 минут на ARM (Oracle Ampere A1).

---

## Шаг 4. Получить api_id и api_hash

1. Открыть **my.telegram.org** → Log in → API development tools
2. Создать приложение (название любое)
3. Скопировать **App api_id** и **App api_hash**

---

## Шаг 5. Запустить telegram-bot-api как сервис

```bash
sudo mkdir -p /var/lib/telegram-bot-api
sudo chown ubuntu:ubuntu /var/lib/telegram-bot-api
sudo nano /etc/systemd/system/telegram-bot-api.service
```

Вставить (заменить `YOUR_API_ID` и `YOUR_API_HASH`):

```ini
[Unit]
Description=Telegram Bot API Server
After=network.target

[Service]
User=ubuntu
ExecStart=/usr/local/bin/telegram-bot-api \
  --api-id=YOUR_API_ID \
  --api-hash=YOUR_API_HASH \
  --local \
  --http-port=8081 \
  --dir=/var/lib/telegram-bot-api
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot-api
```

Проверить:
```bash
sudo journalctl -fu telegram-bot-api
# должно быть: Listening on port 8081
```

---

## Шаг 6. Склонировать репозиторий

```bash
cd ~
git clone https://github.com/your-user/jnx2mb.git
cd jnx2mb
```

---

## Шаг 7. Установить Python-пакеты

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## Шаг 8. Заполнить .env

```bash
cp .env.example .env
nano .env
```

```env
BOT_TOKEN=<токен от @BotFather>
LOCAL_API_URL=http://localhost:8081
```

`Ctrl+O` → Enter → `Ctrl+X`

---

## Шаг 9. Запустить бота как сервис

```bash
sudo nano /etc/systemd/system/jnx2mb-bot.service
```

```ini
[Unit]
Description=jnx2mbtiles Telegram Bot
After=network.target telegram-bot-api.service

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/jnx2mb
EnvironmentFile=/home/ubuntu/jnx2mb/.env
ExecStart=/home/ubuntu/jnx2mb/venv/bin/python3 bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jnx2mb-bot
```

---

## Шаг 10. Проверить

```bash
sudo journalctl -fu jnx2mb-bot
# должно быть:
# Using local Bot API server: http://localhost:8081
# Bot polling started
```

Написать боту `/start` → отправить `.jnx` файл → получить `.mbtiles`.

---

## Управление

```bash
sudo systemctl restart jnx2mb-bot        # перезапустить (после изменения .env)
sudo journalctl -fu jnx2mb-bot           # логи бота
sudo journalctl -fu telegram-bot-api     # логи API сервера
```

---

## Переменные окружения

| Переменная | Обязательная | Описание |
|---|---|---|
| `BOT_TOKEN` | да | Токен от [@BotFather](https://t.me/BotFather) |
| `LOCAL_API_URL` | нет | Адрес локального Bot API сервера. Без него лимит файла — 50 МБ |
