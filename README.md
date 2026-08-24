# Syabas99 → Telegram Hourly Deposit Report

Production-oriented hourly report job for the Syabas99 admin API.

## What it does

- Logs in with the Syabas99 username/password on every run.
- Never hard-codes or stores `accessId` / `accessToken`; a fresh token is obtained from `/users/login`.
- Uses `/transactions/getAllTransactions` with `DEPOSIT + COMPLETED`.
- **Does not paginate through all 300+ pages.** It uses the API's own `totalCount` and `totalAmount` for each completed hour and does not store transaction rows/customer data.
- Re-queries every already-completed hour each run, so late-completed transactions are corrected automatically.
- At `21:05`, for example, the latest closed bucket is `20:00:00–20:59:59`, displayed as `2100`.
- At `00:05`, the previous day's last bucket `23:00:00–23:59:59` is displayed as `0000`, completing the 24-hour report.
- Verifies the sum of all hourly buckets against a cumulative API query before Telegram is updated.
- At day close it also cross-checks `/reports/transactions` Daily totals.
- Creates one Telegram message per report date and edits the same message once per new hourly slot.
- Railway runs a 5-minute watchdog; if the scheduled hourly update fails, the next tick retries automatically without duplicating successful updates.
- A 240-second hard timeout prevents a stuck run from blocking future cron executions.
- Stores only Telegram message IDs in `/data/syabas_state.json`; no customer data is stored.

## Telegram output

```text
Syabas99 Deposit Report

Date : 23/08/2026
Target : 8,300
Target Deposit : 100,000

TOTAL COUNT : 3,181
TOTAL AMOUNT : RM 103,545.22

0100 - ...
0200 - ...
...
2100 - ...
```

`REPORT_LINE_STYLE=full` also shows cumulative totals on each line.

## 1. Security first

If a password/token was ever exposed in a screenshot/chat, change the Syabas99 password before deployment. Do not put the password or Telegram bot token in GitHub code.

## 2. Create Telegram Bot

1. Open Telegram and chat with **@BotFather**.
2. Send `/newbot` and follow the prompts.
3. Copy the Bot Token and keep it private.
4. Add the bot to the target group/channel. For a channel, make the bot an admin with permission to post/edit messages.
5. To get a chat ID, first send `/start` to the bot (or send a message in the group), then run:

```bash
python main.py --get-chat-id
```

Put the returned ID in `TELEGRAM_CHAT_ID`.

## 3. Local test

```bash
cp .env.example .env
# Edit .env with your OWN secrets
pip install -r requirements.txt
python main.py --test-telegram
DRY_RUN=true python main.py
```

If login fails and the backend requires the browser's tracking code, set `SITE_TRACKING_CODE`. `SITE_PASSCODE_2FA` and `SITE_CAPTCHA_OUTPUT` are also supported, but CAPTCHA that changes every login cannot be safely automated and should cause the job to stop instead of bypassing it.

## 4. Railway deployment (recommended)

1. Put this folder in a private GitHub repository.
2. Railway → **New Project** → **Deploy from GitHub Repo**.
3. Add all `.env.example` values under Railway **Variables**. Do not upload `.env`.
4. Add a Railway **Volume** to this service and mount it at:

```text
/data
```

This preserves the Telegram `message_id` so the same daily message can keep being edited after restarts.

5. In Service → Settings → **Cron Schedule**, set the reliability watchdog:

```cron
*/5 * * * *
```

Railway cron uses UTC. Running every 5 minutes avoids relying on one exact minute. The code itself decides whether a new Malaysia hourly slot is due, updates it once, and exits. If a run fails, a later 5-minute tick retries the same slot.

6. Start command is already the Dockerfile default:

```text
python main.py
```

Each cron run finishes and exits. The computer does **not** need to remain on.

## Why a 5-minute watchdog instead of one hourly tick?

Railway does not guarantee exact-to-the-minute cron execution, and it skips a new cron run if a previous run is still active. The watchdog runs every 5 minutes; successful slots are remembered so repeated ticks do nothing. Failed slots are retried on the next tick.

Example:

- first successful tick after 21:00 → includes through 20:59:59 → line `2100`
- first successful tick after 22:00 → includes through 21:59:59 → line `2200`
- first successful tick after 00:00 → includes previous day 23:00–23:59 → line `0000`

## Changing Target values

Change Railway Variables only:

```text
TARGET_COUNT=9000
TARGET_DEPOSIT=120000
```

No code change or rebuild is required for normal variable updates after Railway redeploys/restarts the cron service with the new environment.

## Reliability behavior

- 5-minute watchdog with idempotent per-slot success state.
- 240-second hard job timeout so a stuck run cannot block future ticks.
- HTTP retries with exponential backoff.
- Fresh login every cron run.
- Automatic re-login once if an authenticated API request is rejected.
- Full hourly re-calculation every run (not incremental local addition).
- Decimal money arithmetic.
- Hourly-sum vs cumulative-total validation before Telegram update.
- Daily API cross-check after all 24 hours close.
- Existing Telegram message is never overwritten when validation fails.
- Optional `TELEGRAM_ALERT_CHAT_ID` sends one alert per unique error until the error changes or a successful run clears it.

## Important assumptions confirmed from the captured API

Login module:

```text
/users/login
```

Hourly/cumulative source:

```text
/transactions/getAllTransactions
```

with:

```text
type=DEPOSIT
status=COMPLETED
```

Daily verification source:

```text
/reports/transactions
period=Daily
```

If Syabas99 changes these private backend API contracts later, the code may require an update.
