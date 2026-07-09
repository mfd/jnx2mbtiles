# Telegram-бот — деплой на Oracle Cloud Free Tier

`bot.py` принимает `.jnx`, конвертирует в `.mbtiles` и возвращает ссылку на скачивание.  
Файлы передаются **напрямую по HTTP** в обход Telegram — никаких ограничений по размеру.

## Как это работает

```
Пользователь                    VPS (Oracle Cloud)
─────────────                   ──────────────────
/convert          ──Telegram──▶  генерирует токен, отдаёт curl-команду
curl -T file.jnx  ────HTTP────▶  принимает файл (любой размер)
                                 конвертирует jnx → mbtiles
                  ◀──Telegram──  ссылка на скачивание
curl -OJ <url>    ◀───HTTP────   отдаёт .mbtiles
```

Временные файлы хранятся N часов (по умолчанию 2), затем удаляются автоматически.

## Переменные окружения

| Переменная | Обязательная | Описание |
|---|---|---|
| `BOT_TOKEN` | да | Токен бота от [@BotFather](https://t.me/BotFather) |
| `VPS_URL` | да | Публичный адрес VPS, например `http://1.2.3.4:8080` |
| `HTTP_PORT` | нет | Порт встроенного HTTP-сервера (по умолчанию `8080`) |
| `EXPIRE_HOURS` | нет | Сколько часов хранить готовый файл (по умолчанию `2`) |

---

## Деплой на Oracle Cloud Free Tier

### 1. Создать инстанс

Oracle Cloud → Compute → Instances → Create

- Shape: **Ampere A1** (ARM, до 4 OCPU + 24 ГБ RAM — бесплатно навсегда)
- OS: Ubuntu 22.04

### 2. Открыть порт 8080

В Oracle Cloud есть два места, где нужно разрешить трафик.

**Security List** (Oracle Cloud Console):  
Networking → Virtual Cloud Networks → ваш VCN → Security Lists → Default →  
Ingress Rules → Add:
- Source: `0.0.0.0/0`
- Protocol: TCP
- Destination port: `8080`

**Брандмауэр на самом сервере:**
```bash
sudo iptables -I INPUT -p tcp --dport 8080 -j ACCEPT
sudo netfilter-persistent save   # сохранить между перезагрузками
```

### 3. Установить зависимости

```bash
sudo apt update && sudo apt install -y python3-pip git
git clone https://github.com/mfd/jnx2mbtiles.git
cd jnx2mbtiles
pip3 install -r requirements.txt
```

### 4. Создать systemd-сервис

```bash
sudo nano /etc/systemd/system/jnx2mb-bot.service
```

```ini
[Unit]
Description=jnx2mbtiles Telegram Bot
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/jnx2mbtiles
Environment="BOT_TOKEN=<токен от BotFather>"
Environment="VPS_URL=http://<публичный IP>:8080"
ExecStart=python3 bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jnx2mb-bot
```

### 5. Проверить логи

```bash
sudo journalctl -fu jnx2mb-bot
```

---

## Использование

В Telegram:
```
/convert
```

Бот ответит командой для загрузки. Выполнить на своём компьютере:
```bash
curl -T mymap.jnx http://<ip>:8080/upload/<token>/mymap.jnx
```

Когда конвертация закончится, бот пришлёт ссылку. Скачать:
```bash
curl -OJ http://<ip>:8080/download/<token>/mymap.mbtiles
```

Или просто открыть ссылку в браузере.
