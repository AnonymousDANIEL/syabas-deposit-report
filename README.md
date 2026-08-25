# Syabas99 Telegram Deposit Report — Final V5

Final production layout + Stable V4 always-on/session-recovery behavior.

## Telegram behavior

- Same report date: always **edit the same Telegram message**.
- At `0000`, the previous date is finalized.
- At the next `0100`, a **new Telegram message** is sent for the new report date.
- Future hours are not shown.
- A closed hour with zero deposits is still shown as zero.
- Every update rebuilds all closed hours from fresh API data, so late COMPLETED deposits can correct earlier hours.
- Validation failure never overwrites the last good Telegram report.

## Final layout

```text
Syabas99 Deposit Report

Date : 24/08/2026
Target : 8,300
Target Deposit : 100,000

TOTAL COUNT : 3,529
TOTAL AMOUNT : RM 120,054.91


0100    197   RM   6,424.50     197 / RM   6,424.50
0200    198   RM   7,074.46     395 / RM  13,498.96
...
2300    207   RM   7,798.46   3,529 / RM 120,054.91
```

Telegram HTML `<pre>` mode is used so the columns stay aligned in a monospace font. There is no HOUR/COUNT/AMOUNT/CUMULATIVE header and no repeated `TOTAL:` on hourly lines.

## Railway

V5 remains an always-on worker. **Do not add a Cron Schedule.**

Keep a Railway volume mounted at:

```text
/data
```

Required/recommended variables:

```text
SITE_API_URL=https://jksyab99.u55y38.com/api/v1/index.php
SITE_USERNAME=...
SITE_PASSWORD=...
SITE_MERCHANT_ID=10776
SITE_TRACKING_CODE=

TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_ALERT_CHAT_ID=

TARGET_COUNT=8300
TARGET_DEPOSIT=100000
REPORT_TIMEZONE=Asia/Kuala_Lumpur
STATE_PATH=/data/syabas_state.json

RUN_FOREVER=true
SESSION_GUARD_ENABLED=true
SESSION_GUARD_SECONDS=15
AUTH_RELOGIN_RETRIES=5
REPORT_GRACE_SECONDS=60
REPORT_RETRY_SECONDS=30
PRELOGIN_ENABLED=false

REQUEST_TIMEOUT_SECONDS=30
REQUEST_RETRIES=3
VALIDATION_RETRIES=3
VALIDATION_RETRY_DELAY_SECONDS=4
STRICT_FINAL_DAILY_VALIDATION=false
SHOW_VALIDATION_STATUS=false
DRY_RUN=false
FORCE_RUN=false
```

`REPORT_LINE_STYLE` may remain in Railway from an older version; V5 ignores it and always uses the final aligned layout.

## Stability behavior

- Fresh login before every hourly report calculation.
- Session guard checks the current authenticated session continuously.
- If another login invalidates the worker token, it force-logins again and retries immediately.
- Hourly update failure retries without waiting for the next hour.
- The report date itself is the Telegram message key, which is why a new day automatically sends a new message instead of editing yesterday.

## Local checks

```bash
python -m py_compile *.py
python test_logic.py
```
