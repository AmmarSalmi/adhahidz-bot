## Telegram Quota Checker Bot

A fully dockerized Telegram bot that periodically checks Wilaya-level quota availability from a REST API and notifies subscribed users when quota opens.

### Prerequisites
- Docker + Docker Compose

### Create a Telegram bot token
- Create your bot with BotFather: [BotFather](https://t.me/BotFather)
- Copy the token into your `.env`

### Configure environment
1. Copy the example env file:

```bash
cp .env.example .env
```

2. Edit `.env` and set at least:
- `TELEGRAM_BOT_TOKEN`
- `QUOTA_API_BASE_URL` (default is `https://adhahi.dz`)

### Run

```bash
docker compose up --build
```

SQLite data persists in the named volume `bot-data` mounted at `/data`.

### Bot commands
- `/start`: pick a Wilaya and subscribe
- `/change`: change your Wilaya subscription
- `/status`: show your subscription and last known quota status
- `/stop`: unsubscribe

### Adapting to a different quota API
All API shape assumptions are centralized in `bot/api_client.py`:
- Update the endpoint path in `QuotaApiClient.fetch_wilaya_quotas()`
- Update the mapping logic in `parse_wilaya_quotas()` to extract:
  - wilaya code + name
  - availability boolean
  - remaining units (optional)

### Switching to webhooks later (optional)
This bot uses long-polling (`getUpdates`) for simplicity in Docker.
To switch to webhooks later, you typically need:
- A public HTTPS endpoint reachable by Telegram
- A reverse proxy (e.g. Caddy/Nginx/Traefik) terminating TLS
- Configure `setWebhook` with your public URL
- Run the bot in webhook mode in `bot/main.py` (PTB supports this)
