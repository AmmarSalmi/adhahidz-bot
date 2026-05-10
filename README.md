## Telegram Quota Checker Bot

A fully dockerized Telegram bot that periodically checks Wilaya-level quota availability from a REST API and notifies subscribed users when quota opens.

### Features
- **Quota Monitoring**: Periodically checks Wilaya-level quota availability from the REST API.
- **Interactive UI**: Navigate easily using persistent bottom menus and hierarchical inline keyboards instead of memorizing commands.
- **Multi-language Support (i18n)**: Fully localized interface in Arabic 🇩🇿, French 🇫🇷, and English 🇬🇧.
- **Notifications**: Alerts subscribed users when quota becomes available in their chosen Wilaya.
- **Concurrent Auto-Registration**: Automatically registers multiple users simultaneously, handles OTPs, and creates orders the moment quotas open. Now features **Seniority-Based Priority**, **Global Concurrency Throttling**, and **Aggressive Mode** (continuous scanning of open Wilayas to ensure no profile is missed, even if added after the Wilaya opened).
- **Intelligent Nudging**: When Wilayas are open, the bot aggressively nudges users with `pre-registered` profiles to verify their OTP, featuring a built-in **1-hour cooldown** to balance urgency with user experience.
- **CAPTCHA Solving**: Built-in support for local OCR (`ddddocr`) and third-party API (`2captcha`) for solving CAPTCHAs during automated workflows, with sequential fallback to minimize paid API usage.
- **Order Management**: Tracks the lifecycle of profiles (`pending`, `pre-registered`, `registered`, `ordered`), verifies pending orders, and sends 12-hour reminders for OTP verification.
- **Profile Usage Limits**: Enforces a fair-usage limit of 3 registration profiles per user to ensure system stability and performance.
- **Quota History & Analysis**: Automatically records every "OPEN" and "CLOSE" event for all Wilayas in the database. This data allows for analyzing quota patterns, measuring window durations, and understanding the frequency of availability changes. Recent history is visible directly in the admin stats panel.
- **Admin Access Control**: Includes a hidden admin dashboard with a toggleable "restricted mode", **live concurrency limit adjustment**, **live check interval updates**, **granular proxy controls**, and **recent quota event history**. Commands like `/checkprofile` are restricted to administrators.
- **Rate Limit Fail-Safe Strategy**: The bot is highly reactive to server-side blocking. If an HTTP 429 (Too Many Requests) is detected during quota monitoring, the bot automatically **increases the check interval by 30%** and immediately alerts the administrator with the error details.
- **Improved Log Stream**: Suppresses verbose library success logs (HTTP 200s) and silences noisy tracebacks for common non-critical events (like users blocking the bot) to keep the log stream high-signal.
- **Granular Proxy Management**: Admin can independently toggle residential proxy usage for three critical workflows: Wilaya monitoring, Auto-registration, and Profile status checking.
- **Sticky Session Isolation**: Each profile interaction (registration, login, ordering) is isolated in its own HTTP session. When proxying is enabled, it uses **Sticky Session IDs** based on the citizen's NIN to ensure IP consistency across the entire registration lifecycle, avoiding WAF blocks.
- **Resilient Registration Detection**: The bot now features enhanced detection for diverse API responses, including multilingual "already registered" messages (Arabic/French/English), allowing it to seamlessly transition from registration to the login+order flow without manual intervention.
- **Automatic Resource Optimization**: To conserve system resources and ensure data privacy, the bot now automatically detects when a user blocks it (via near real-time `ChatMember` updates and proactive checks during scheduled tasks) and immediately deletes all associated data, including Wilaya subscriptions and registration profiles.
- **Non-Blocking Asynchronous Execution**: The auto-registration engine now runs in the background, decoupled from the main monitor. This prevents slow CAPTCHA solving or large profile batches from blocking the quota monitor, ensuring that every Wilaya window is detected on time.
- **Self-Healing Registration Flows**: The bot now intelligently handles "Quota is not active" errors during registration. Instead of marking profiles as failed, it automatically resets them to `pending` and notifies the user, allowing for immediate retries if the quota re-opens. It also features a **startup recovery system** that cleans up any profiles stuck in transient states during a system reboot.
- **Intelligent UX Guardrails**: Prevents avoidable registration failures by implementing proactive input normalization and validation. The bot now intelligently recognizes and filters out common "skip" keywords (e.g., "skip", "aucun", "none", "لا") in multiple languages for optional fields like email, ensuring data sent to the API is always correctly formatted.
- **Modernized PTB Compatibility**: Fully optimized for `python-telegram-bot` v20/v21. Includes resolved `ChatMemberStatus` imports and a specialized warning suppression engine in `bot/main.py` to keep the console output clean and focused on critical events.
- **Robust Error Resilience**: Implemented a global exception handler that intelligently filters out harmless Telegram API errors (like "Message is not modified" from double-clicks) and gracefully handles blocked-bot scenarios. This ensures maximum uptime and a clean, high-signal log stream for production monitoring.
- **Admin Error & Warning Inbox**: A centralized, persistent monitoring system that intercepts all `ERROR` and `WARNING` log events. Features real-time Telegram notifications for admins, a filterable paginated dashboard (by level, status, and date range), and a resolution tracking system to manage system health.
- **Advanced Admin Audit Suite**: Includes a comprehensive set of maintenance tools:
  - **Mass Force Check**: Scans the entire database for data integrity issues with both **Standard** (notifies users) and **Silent** (audit-only) modes.
  - **Profile Inspector**: Allows admins to look up full details and real-time validation errors for any profile using its unique ID.
  - **Blocker Purge**: A manual cleanup tool that identifies and removes all data for users who have blocked the bot.
- **Strict Compliance Gate**: Implements a unified validation engine across all flows (Add, Edit, Audit). Any profile failing server standards (NIN, CNIBE, Phone, or Password complexity) is automatically marked as `is_valid=0` and strictly excluded from auto-registration batches until corrected by the user.
- **Strict Server-Sync Validation**: Implements exact `adhahi.dz` requirements to prevent avoidable registration failures:
  - **Password**: 8-16 characters, mandatory complexity (Upper/Lower/Digit/Symbol), and **explicitly forbids dots (`.`)**.
  - **Identifiers**: Exactly 18-digit NIN and 9-digit CNIBE.
  - **Contact**: Exactly 10-digit Phone starting with `0`.

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
- `/adminammar`: Open the hidden admin dashboard (Force Check, Purge, Inbox, Stats)
- `/checkprofile`: (Admin Only) Quick check if a profile NIN is registered on server

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
