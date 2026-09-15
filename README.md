# Syabas99 Telegram Deposit Report — Final Auto TrackingCode

This is the current stable Railway/GitHub version.

## Login stability

The Syabas99 login trackingCode changes on each login. This version no longer
depends on a fixed SITE_TRACKING_CODE.

With:

```
AUTO_TRACKING_CODE=true
```

the worker launches headless Chromium only when login/re-login is needed:

1. Opens the real Syabas99 login page.
2. Lets the site generate its latest trackingCode normally.
3. Fills SITE_USERNAME / SITE_PASSWORD.
4. Captures the real /users/login request and successful JSON response.
5. Stores data.id as accessId and data.token as accessToken in memory.
6. Closes Chromium.
7. All deposit/report requests continue through the fast direct API.

If another human login invalidates the bot session, SESSION GUARD detects it
and immediately performs the same fresh browser login again. Railway does not
need to be restarted.

This does not bypass CAPTCHA or interactive 2FA. If Syabas99 starts requiring
a human CAPTCHA/2FA challenge, the worker fails clearly and alerts instead of
pretending the login succeeded.

## Telegram behavior

- Same report date edits the same Telegram message.
- A new report date creates a new Telegram message.
- Shows all 24 labels: 0100..2300, then 0000.
- Future/unclosed rows show:
  `0 | RM 0.00 | TOTAL: 0 / RM 0.00`
- When an hour closes, that row changes to the verified real amount/count and
  real cumulative TOTAL.
- Plain-text message: no Telegram Copy code-block button.

## Railway

Run as an always-on service, not Cron.

Required new variables:

```
SITE_LOGIN_URL=https://jksyab99.u55y38.com/
AUTO_TRACKING_CODE=true
BROWSER_LOGIN_TIMEOUT_SECONDS=30
```

Keep SITE_TRACKING_CODE empty in auto mode.

The Docker image installs Playwright Chromium automatically.


## Exact hourly timing

Set:

```
REPORT_GRACE_SECONDS=0
```

The worker starts the new hourly calculation immediately after the top of the hour. There is no intentional +1 minute delay. If the upstream API has not finished closing the hour yet, the existing retry loop keeps retrying until the validated report succeeds.
