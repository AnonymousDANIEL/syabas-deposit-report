# Syabas99 Telegram Deposit Report — Stable V4

This version is designed for the requirement: **if another login invalidates the bot session, reclaim the Syabas99 login immediately instead of waiting for the next cron run.**

## What changed from V3

V3 used Railway Cron every 5 minutes. V4 is an **always-on Railway worker**.

- No 5-minute pre-login.
- No need to wait for the next cron tick.
- A lightweight authenticated health check runs every `SESSION_GUARD_SECONDS` (default 15 seconds).
- If the token/session is rejected after another login, the worker immediately calls `/users/login`, gets a new `accessId` + `token`, and retries the same request.
- The same immediate re-login logic is active during hourly report calculations, so a token rotation in the middle of a report does not have to wait for another scheduler cycle.
- Each hourly report runs after a short grace period (`REPORT_GRACE_SECONDS`, default 60 seconds) so the previous hour can finish settling.
- If the report validation/API fails, it retries every `REPORT_RETRY_SECONDS` (default 30 seconds) until the hour is successfully updated.

## Railway setup — IMPORTANT

### 1. Remove the Cron Schedule

V4 must stay running continuously. In Railway **Settings**, remove/disable the Cron Schedule such as:

```cron
*/5 * * * *
```

Do not use a cron schedule for V4.

### 2. Keep the service running

The included Dockerfile starts:

```bash
python main.py
```

`RUN_FOREVER=true` makes it remain active as a worker.

### 3. Keep the `/data` Volume

Mount Railway Volume at:

```text
/data
```

The state file stores the Telegram message ID and the last successful report slot. It does not need to store customer transaction rows.

## Required Variables

```text
SITE_API_URL=https://jksyab99.u55y38.com/api/v1/index.php
SITE_USERNAME=...
SITE_PASSWORD=...
SITE_MERCHANT_ID=10776
SITE_TRACKING_CODE=

TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

TARGET_COUNT=8300
TARGET_DEPOSIT=100000
REPORT_TIMEZONE=Asia/Kuala_Lumpur
REPORT_LINE_STYLE=full
STATE_PATH=/data/syabas_state.json

RUN_FOREVER=true
SESSION_GUARD_ENABLED=true
SESSION_GUARD_SECONDS=15
AUTH_RELOGIN_RETRIES=5
REPORT_GRACE_SECONDS=60
REPORT_RETRY_SECONDS=30

PRELOGIN_ENABLED=false
```

`TARGET_DEPOSIT` must be `100000`, not `100,000`.

## Runtime behavior

Example around 22:00 Malaysia time:

```text
21:59:45  session guard OK
22:00:00  new 2200 slot exists
22:01:00  report starts (default 60-second grace)
           fresh force-login
           fetch 0100..2200 data
           validate hourly sum vs cumulative total
           edit the same Telegram report message
```

If a person logs in at 22:12 and the site invalidates the worker's token:

```text
22:12:xx  health check is rejected
           SESSION LOST/ROTATED
           force /users/login immediately
           new accessId/token
           retry health check
           SESSION RECOVERED
```

With `SESSION_GUARD_SECONDS=15`, detection is normally within about 15 seconds. Lower values create more API traffic; 15 seconds is the recommended default.

## Important limitation

The worker can only detect another login if that login actually invalidates/rotates the worker's authenticated token. There is no separate known "someone logged in" event endpoint. If the backend permits multiple simultaneous tokens, there is nothing to recover because the bot session remains valid.

If a human keeps repeatedly logging in with the same account and the site allows only one session, the human and bot can repeatedly replace each other's session. The most reliable production setup is a dedicated Syabas99 account for the report worker if the backend supports one.

## Useful logs

Normal:

```text
DAEMON START ...
SESSION GUARD OK
HOURLY UPDATE DUE slot=2200 - running now
SESSION TAKEOVER OK - Syabas99 force-login reclaimed the account
RUN SUCCESS slot=2026-08-24:2200 ...
```

If another login invalidates the token:

```text
SESSION LOST/ROTATED ... Force-login starts immediately.
SESSION TAKEOVER OK ...
SESSION RECOVERED immediately ...
```

## One-time commands

Run once and exit:

```bash
python main.py --once
```

Test Telegram:

```bash
python main.py --test-telegram
```

Get Telegram chat IDs:

```bash
python main.py --get-chat-id
```
