## Telegram Quota Checker Bot

A fully dockerized Telegram bot that periodically checks Wilaya-level quota availability from a REST API and notifies subscribed users when quota opens.

### Features
- **Quota Monitoring**: Periodically checks Wilaya-level quota availability from the REST API.
- **Notifications**: Alerts subscribed users when quota becomes available in their chosen Wilaya.
- **Auto-Registration**: Comprehensive profile management to automatically register users, handle OTPs, and create orders the moment quotas open. Includes support for selecting payment methods (CASH, TPE, EN_LIGNE).
- **CAPTCHA Solving**: Built-in support for local OCR (`ddddocr`) and third-party API (`2captcha`) for solving CAPTCHAs during automated workflows, with sequential fallback to minimize paid API usage.
- **Order Management**: Tracks the lifecycle of profiles (`pending`, `pre-registered`, `registered`, `ordered`), verifies pending orders, and sends 12-hour reminders for OTP verification.

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

Optional tuning:
- `CHECK_INTERVAL_SECONDS`: how often to poll the API (default `300`)
- `CONFIRM_FETCHES`: when `available=true` is detected, re-fetch this many extra times before notifying (default `2`)
- `CONFIRM_DELAY_SECONDS`: delay between confirmation re-fetches in seconds (default `1`)
- `TWO_CAPTCHA_API_KEY`: API key for 2Captcha service (optional, falls back to local `ddddocr` if not provided)

### Run

```bash
docker compose up --build
```

SQLite data persists in the named volume `bot-data` mounted at `/data`.

### Bot commands

**Quota & Monitoring**
- `/start`: Subscribe to wilaya quota notifications
- `/change`: Change your subscribed wilaya
- `/status`: Check your current subscription status
- `/stop`: Unsubscribe from notifications
- `/fetchinfo`: Last fetch time & watched wilayas

**Profile & Auto-Registration**
- `/addprofile`: Add an auto-registration profile
- `/profiles`: List your registration profiles
- `/viewprofile`: View full profile details (incl. password)
- `/editprofile`: Edit a registration profile
- `/deleteprofile`: Delete a registration profile
- `/reorder`: Change profile priority order
- `/checkprofile`: Check if a profile NIN is registered on server
- `/verifyotp`: Verify OTP for a submitted profile
- `/register`: Manual adhahi.dz registration flow
- `/testcaptchasolvers`: Test both CAPTCHA solvers side-by-side
- `/help`: Show all available commands

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
