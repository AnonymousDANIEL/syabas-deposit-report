# Syabas99 Telegram Deposit Report — Final V8

V8 fixes the Telegram display issue:

- Report is sent as **plain text** — no `<pre>` and no `<code>`.
- Therefore Telegram does **not** show the large **Copy** button.
- Exact hourly row format: `0100 -    0 | RM       0.00 | TOTAL:    0 / RM 0.00`.
- Always shows all 24 labels: `0100` through `2300`, then `0000`.
- Closed hours use real values; future hours show `0 / RM 0.00`.
- Cumulative `TOTAL:` stays at the latest verified real cumulative total.
- The same day edits the same Telegram message.
- A new report date creates a new Telegram message.
- On every Railway deploy/restart, V8 immediately re-renders the current Telegram message once, so formatting changes show immediately instead of waiting for the next hour.
- Always-on session guard/relogin behavior from V4+ is retained.

Railway should continue running as an always-on service (no cron).
