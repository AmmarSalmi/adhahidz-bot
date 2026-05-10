## Telegram Quota Checker Bot

A fully dockerized Telegram bot that periodically checks Wilaya-level quota availability from a REST API and notifies subscribed users when quota opens.

### Features
- **Quota Monitoring**: Periodically checks Wilaya-level quota availability from the REST API.
- **Interactive UI**: Navigate easily using persistent bottom menus and hierarchical inline keyboards instead of memorizing commands.
- **Multi-language Support (i18n)**: Fully localized interface in Arabic 🇩🇿, French 🇫🇷, and English 🇬🇧.
- **Notifications**: Alerts subscribed users when quota becomes available in their chosen Wilaya.
- **Concurrent Auto-Registration**: Automatically registers multiple users simultaneously, handles OTPs, and creates orders the moment quotas open. Now features **Seniority-Based Priority** (ordering users by their join date) and **Global Concurrency Throttling** to ensure system stability and avoid server-side blocking.
- **CAPTCHA Solving**: Built-in support for local OCR (`ddddocr`) and third-party API (`2captcha`) for solving CAPTCHAs during automated workflows, with sequential fallback to minimize paid API usage.
- **Order Management**: Tracks the lifecycle of profiles (`pending`, `pre-registered`, `registered`, `ordered`), verifies pending orders, and sends 12-hour reminders for OTP verification.
- **Profile Usage Limits**: Enforces a fair-usage limit of 3 registration profiles per user to ensure system stability and performance.
- **Quota History & Analysis**: Automatically records every "OPEN" and "CLOSE" event for all Wilayas in the database. This data allows for analyzing quota patterns, measuring window durations, and understanding the frequency of availability changes. Recent history is visible directly in the admin stats panel.
- **Admin Access Control**: Includes a hidden admin dashboard with a toggleable "restricted mode", **live concurrency limit adjustment**, **live check interval updates**, **granular proxy controls**, and **recent quota event history**. Commands like `/checkprofile` are restricted to administrators.
- **Rate Limit Fail-Safe Strategy**: The bot is highly reactive to server-side blocking. If an HTTP 429 (Too Many Requests) is detected during quota monitoring, the bot automatically **increases the check interval by 30%** and immediately alerts the administrator with the error details.
- **Improved Log Stream**: Suppresses verbose library success logs (HTTP 200s) to keep the stream focused on critical bot activity and Wilaya monitoring events.
- **Granular Proxy Management**: Admin can independently toggle Databay residential proxy usage for three critical workflows: Wilaya quota monitoring, Automated registration/ordering, and Profile status checking. When proxying is enabled for Wilaya checks, the bot prompts for a specific check interval to optimize bandwidth.

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
- `MAX_CONCURRENT_SESSIONS`: global limit of simultaneous registration/login connections (default `50`)
- `PROXY_WILAYA`: enable proxy for background quota checks (default `false`)
- `PROXY_AUTOREG`: enable proxy for auto-registration flow (default `false`)
- `PROXY_CHECKPROF`: enable proxy for `/checkprofile` command (default `false`)

### Run

```bash
docker compose up --build
```

SQLite data persists in the named volume `bot-data` mounted at `/data`.

### Bot commands

Most interaction is now handled via the built-in menus, but commands are still available:

**General Navigation**
- `/start` or `/menu`: Open the interactive main menu
- `/help`: Show all available commands

**Quota & Monitoring**
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
- `/verifyotp`: Verify OTP for a submitted profile
- `/register`: Manual adhahi.dz registration flow
- `/testcaptchasolvers`: Test both CAPTCHA solvers side-by-side

**Admin Commands**
- `/checkprofile`: (Admin Only) Check if a profile NIN is registered on server
- `/adminammar`: Open the hidden admin dashboard

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
