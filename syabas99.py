from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from urllib.parse import parse_qs

import requests

from config import Config

log = logging.getLogger(__name__)
MONEY = Decimal("0.01")


class SyabasError(RuntimeError):
    pass


@dataclass(frozen=True)
class Totals:
    count: int
    amount: Decimal

    @classmethod
    def from_values(cls, count: Any, amount: Any) -> "Totals":
        return cls(
            count=int(count or 0),
            amount=Decimal(str(amount or 0)).quantize(MONEY, rounding=ROUND_HALF_UP),
        )


class SyabasClient:
    """
    Long-lived Syabas99 client.

    Stability behavior:
    - AUTO_TRACKING_CODE=true: every login is performed by a headless Chromium
      browser. The real Syabas99 login page generates its current trackingCode,
      the browser submits it, and we capture data.id/data.token from /users/login.
    - We do NOT store a fixed trackingCode when auto mode is enabled.
    - If another login invalidates this token, the authenticated API call detects
      the rejection and force-logins immediately with a fresh browser-generated
      trackingCode, then retries the exact API request.
    - Report requests still use direct HTTP API calls after login, so Chromium is
      only used for authentication/re-authentication.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Syabas99TelegramReport/5.0",
            }
        )
        self.access_id = ""
        self.access_token = ""
        self.last_login_monotonic = 0.0
        self.last_tracking_code = ""

    def _post_raw(self, form: dict[str, Any]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, self.cfg.request_retries + 1):
            try:
                response = self.session.post(
                    self.cfg.site_api_url,
                    data=form,
                    timeout=self.cfg.request_timeout_seconds,
                )
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise requests.HTTPError(
                        f"HTTP {response.status_code}", response=response
                    )
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise SyabasError(
                        f"API returned non-JSON response (HTTP {response.status_code})"
                    ) from exc
                if not isinstance(payload, dict):
                    raise SyabasError("API returned unexpected JSON structure")
                return payload
            except (requests.RequestException, SyabasError) as exc:
                last_exc = exc
                if attempt >= self.cfg.request_retries:
                    break
                sleep_s = min(2 ** (attempt - 1), 8)
                log.warning(
                    "API request failed (attempt %s): %s; retrying in %ss",
                    attempt,
                    exc,
                    sleep_s,
                )
                time.sleep(sleep_s)
        raise SyabasError(f"API request failed after retries: {last_exc}")

    @staticmethod
    def _is_success(payload: dict[str, Any]) -> bool:
        return str(payload.get("status", "")).upper() == "SUCCESS"

    @staticmethod
    def _message(payload: dict[str, Any]) -> str:
        data = payload.get("data")
        if isinstance(data, dict) and data.get("message"):
            return str(data.get("message"))
        return str(payload.get("message") or payload.get("error") or payload)

    def _accept_login_payload(self, payload: dict[str, Any], *, reason: str) -> None:
        if not self._is_success(payload):
            raise SyabasError(
                "Syabas99 login failed. "
                f"API response: {self._message(payload)}"
            )

        data = payload.get("data") or {}
        access_id = data.get("id")
        token = data.get("token")
        if not access_id or not token:
            raise SyabasError(
                "Login succeeded but response did not contain data.id/data.token"
            )

        self.access_id = str(access_id)
        self.access_token = str(token)
        self.last_login_monotonic = time.monotonic()

        if reason == "takeover":
            log.warning(
                "SESSION TAKEOVER OK - Syabas99 force-login reclaimed the account"
            )
        else:
            log.info("Syabas99 login OK")

    def _api_login(self, *, reason: str, tracking_code: str | None = None) -> None:
        """Direct /users/login using either a supplied fresh code or the static fallback."""
        form = {
            "username": self.cfg.site_username,
            "password": self.cfg.site_password,
            "passcode2fa": self.cfg.site_passcode_2fa,
            "trackingCode": self.cfg.site_tracking_code if tracking_code is None else tracking_code,
            "captchaOutput": self.cfg.site_captcha_output,
            "module": "/users/login",
            "merchantId": self.cfg.site_merchant_id,
            "accessId": "",
            "accessToken": "",
        }
        payload = self._post_raw(form)
        self._accept_login_payload(payload, reason=reason)

    def _browser_login(self, *, reason: str) -> None:
        """
        Perform the real Syabas99 login in headless Chromium and capture the
        exact /users/login request generated by the current frontend.

        Why this is more robust:
        - trackingCode may only be created when the login form is submitted;
          page-load-only capture is not enough.
        - We search all frames for the password field, fill the real form, then
          try Enter, form.requestSubmit(), and likely Login buttons.
        - We capture both URL-encoded and JSON request bodies.
        - If the browser login itself succeeds, we use its returned id/token.
        - If the request is seen but its response cannot be parsed, we immediately
          retry the direct API once with the exact fresh trackingCode we captured.

        This does not bypass CAPTCHA or interactive 2FA.
        """
        try:
            import json
            import re
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise SyabasError(
                "AUTO_TRACKING_CODE is enabled but Playwright/Chromium is unavailable"
            ) from exc

        timeout_ms = self.cfg.browser_login_timeout_seconds * 1000
        captured: dict[str, Any] = {
            "tracking_code": "",
            "login_request_seen": False,
            "login_payload": None,
            "login_module": "",
        }

        def parse_post_body(request: Any) -> dict[str, Any]:
            try:
                raw = request.post_data or ""
            except Exception:
                raw = ""
            if not raw:
                return {}

            # Syabas99 currently uses application/x-www-form-urlencoded.
            try:
                parsed = parse_qs(raw, keep_blank_values=True)
                if parsed:
                    return {k: (v[0] if isinstance(v, list) and v else v) for k, v in parsed.items()}
            except Exception:
                pass

            # Future-proof fallback in case frontend switches to JSON.
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
            return {}

        def on_request(request: Any) -> None:
            try:
                body = parse_post_body(request)
                module = str(body.get("module") or "")
                code = str(body.get("trackingCode") or "")
                if code:
                    captured["tracking_code"] = code
                    captured["login_module"] = module
                if module == "/users/login":
                    captured["login_request_seen"] = True
                    if code:
                        captured["tracking_code"] = code
            except Exception:
                return

        def on_response(response: Any) -> None:
            try:
                body = parse_post_body(response.request)
                module = str(body.get("module") or "")
                if module != "/users/login":
                    return
                captured["login_request_seen"] = True
                code = str(body.get("trackingCode") or "")
                if code:
                    captured["tracking_code"] = code
                payload = response.json()
                if isinstance(payload, dict):
                    captured["login_payload"] = payload
            except Exception:
                return

        log.warning(
            "AUTO TRACKING LOGIN - opening Syabas99 and submitting the real login form"
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context()
            page = context.new_page()
            page.on("request", on_request)
            page.on("response", on_response)

            try:
                page.goto(
                    self.cfg.site_login_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )
                page.wait_for_timeout(1200)

                def first_visible(frame: Any, selectors: list[str]) -> Any | None:
                    for selector in selectors:
                        try:
                            loc = frame.locator(selector)
                            count = min(loc.count(), 8)
                            for i in range(count):
                                item = loc.nth(i)
                                if item.is_visible():
                                    return item
                        except Exception:
                            continue
                    return None

                username_selectors = [
                    'input[name="username"]',
                    'input#username',
                    'input[name="user"]',
                    'input[name*="login" i]',
                    'input[id*="user" i]',
                    'input[id*="login" i]',
                    'input[type="email"]',
                    'input[type="text"]',
                ]
                password_selectors = [
                    'input[name="password"]',
                    'input#password',
                    'input[name*="pass" i]',
                    'input[id*="pass" i]',
                    'input[type="password"]',
                ]

                login_frame = None
                user_input = None
                pass_input = None

                # Login form can be in the main page or in an iframe.
                for frame in page.frames:
                    pwd = first_visible(frame, password_selectors)
                    if pwd is None:
                        continue
                    usr = first_visible(frame, username_selectors)
                    if usr is not None:
                        login_frame = frame
                        user_input = usr
                        pass_input = pwd
                        break

                if login_frame is None or user_input is None or pass_input is None:
                    # Compact diagnostics without exposing secrets.
                    try:
                        title = page.title()
                    except Exception:
                        title = ""
                    try:
                        body_preview = re.sub(r"\s+", " ", page.locator("body").inner_text())[:240]
                    except Exception:
                        body_preview = ""
                    raise SyabasError(
                        "Syabas99 login form was not detected in Chromium. "
                        f"url={page.url!r} title={title!r} page={body_preview!r}"
                    )

                user_input.fill(self.cfg.site_username)
                pass_input.fill(self.cfg.site_password)

                if self.cfg.site_passcode_2fa:
                    otp = first_visible(
                        login_frame,
                        [
                            'input[name="passcode2fa"]',
                            'input[name="otp"]',
                            'input[id*="otp" i]',
                            'input[id*="2fa" i]',
                        ],
                    )
                    if otp is not None:
                        otp.fill(self.cfg.site_passcode_2fa)

                if self.cfg.site_captcha_output:
                    captcha = first_visible(
                        login_frame,
                        [
                            'input[name="captchaOutput"]',
                            'input[name="captcha"]',
                            'input[id*="captcha" i]',
                        ],
                    )
                    if captcha is not None:
                        captcha.fill(self.cfg.site_captcha_output)

                def wait_for_login_request(seconds: float) -> bool:
                    deadline = time.monotonic() + seconds
                    while time.monotonic() < deadline:
                        if captured["login_request_seen"]:
                            return True
                        page.wait_for_timeout(150)
                    return bool(captured["login_request_seen"])

                # Attempt 1: Enter from password field.
                try:
                    pass_input.press("Enter")
                except Exception:
                    pass
                wait_for_login_request(2.0)

                # Attempt 2: native form submission, preserving page submit handlers.
                if not captured["login_request_seen"]:
                    try:
                        pass_input.evaluate(
                            """el => {
                                const f = el.form || el.closest('form');
                                if (f) {
                                    if (typeof f.requestSubmit === 'function') f.requestSubmit();
                                    else f.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}));
                                }
                            }"""
                        )
                    except Exception:
                        pass
                    wait_for_login_request(2.0)

                # Attempt 3: click likely login buttons in the same frame.
                if not captured["login_request_seen"]:
                    candidates = [
                        'button[type="submit"]',
                        'input[type="submit"]',
                        'button:has-text("Login")',
                        'button:has-text("LOGIN")',
                        'button:has-text("Log In")',
                        'button:has-text("Sign In")',
                        '[role="button"]:has-text("Login")',
                        '[onclick*="login" i]',
                        '.btn-login',
                        '#login',
                    ]
                    for selector in candidates:
                        try:
                            loc = login_frame.locator(selector)
                            count = min(loc.count(), 5)
                            clicked = False
                            for i in range(count):
                                item = loc.nth(i)
                                if item.is_visible():
                                    item.click(timeout=2000)
                                    clicked = True
                                    break
                            if clicked and wait_for_login_request(2.0):
                                break
                        except Exception:
                            continue

                # Attempt 4: scan visible clickable elements whose label looks like login.
                if not captured["login_request_seen"]:
                    try:
                        clickables = login_frame.locator(
                            "button, input[type=button], input[type=submit], [role=button], a"
                        )
                        for i in range(min(clickables.count(), 30)):
                            item = clickables.nth(i)
                            try:
                                if not item.is_visible():
                                    continue
                                label = (
                                    item.inner_text()
                                    or item.get_attribute("value")
                                    or item.get_attribute("aria-label")
                                    or ""
                                ).strip().lower()
                                if any(k in label for k in ("login", "log in", "sign in")):
                                    item.click(timeout=2000)
                                    if wait_for_login_request(2.0):
                                        break
                            except Exception:
                                continue
                    except Exception:
                        pass

                # Once the request exists, allow time for the JSON response.
                if captured["login_request_seen"] and captured["login_payload"] is None:
                    deadline = time.monotonic() + min(
                        self.cfg.browser_login_timeout_seconds, 10
                    )
                    while time.monotonic() < deadline:
                        if captured["login_payload"] is not None:
                            break
                        page.wait_for_timeout(150)

                tracking = str(captured["tracking_code"] or "")
                payload = captured["login_payload"]

                # Copy cookies before closing the browser in case backend binds login
                # state/trackingCode to a browser cookie.
                for cookie in context.cookies():
                    try:
                        self.session.cookies.set(
                            cookie["name"],
                            cookie["value"],
                            domain=cookie.get("domain") or None,
                            path=cookie.get("path") or "/",
                        )
                    except Exception:
                        pass

                if isinstance(payload, dict):
                    if tracking:
                        self.last_tracking_code = tracking
                        log.info(
                            "AUTO TRACKING CODE captured from real /users/login (length=%s)",
                            len(tracking),
                        )
                    self._accept_login_payload(payload, reason=reason)
                    return

                if captured["login_request_seen"] and tracking:
                    # Rare fallback: request was visible but response JSON wasn't.
                    self.last_tracking_code = tracking
                    log.warning(
                        "Login request captured but response was unavailable; "
                        "retrying direct API once with the exact fresh trackingCode"
                    )
                else:
                    try:
                        title = page.title()
                    except Exception:
                        title = ""
                    try:
                        body_preview = re.sub(r"\s+", " ", page.locator("body").inner_text())[:320]
                    except Exception:
                        body_preview = ""
                    raise SyabasError(
                        "Syabas99 browser could not trigger /users/login. "
                        f"url={page.url!r} title={title!r} page={body_preview!r}. "
                        "If this shows a security/CAPTCHA page, Railway browser login "
                        "is being challenged by the site."
                    )
            finally:
                context.close()
                browser.close()

        # Fallback path only when we captured the exact fresh code but not response JSON.
        self._api_login(reason=reason, tracking_code=tracking)

    def login(self, *, reason: str = "normal") -> None:
        if self.cfg.auto_tracking_code:
            self._browser_login(reason=reason)
        else:
            self._api_login(reason=reason)

    def force_login(self) -> None:
        """Immediately reclaim the account/session after token rotation."""
        self.access_id = ""
        self.access_token = ""
        self.login(reason="takeover")

    def _authenticated_post(self, form: dict[str, Any]) -> dict[str, Any]:
        if not self.access_id or not self.access_token:
            self.login()

        body = dict(form)
        body.update(
            {
                "merchantId": self.cfg.site_merchant_id,
                "accessId": self.access_id,
                "accessToken": self.access_token,
            }
        )

        payload = self._post_raw(body)
        if self._is_success(payload):
            return payload

        first_message = self._message(payload)
        log.warning(
            "SESSION LOST/ROTATED - authenticated request rejected: %s. "
            "Force-login starts immediately.",
            first_message,
        )

        last_payload = payload
        for attempt in range(1, self.cfg.auth_relogin_retries + 1):
            try:
                self.force_login()
                body["accessId"] = self.access_id
                body["accessToken"] = self.access_token
                last_payload = self._post_raw(body)
                if self._is_success(last_payload):
                    log.info("SESSION RECOVERED immediately on attempt %s", attempt)
                    return last_payload
                log.warning(
                    "Re-login attempt %s succeeded but API still rejected token: %s",
                    attempt,
                    self._message(last_payload),
                )
            except Exception as exc:
                log.warning("Immediate re-login attempt %s failed: %s", attempt, exc)
            if attempt < self.cfg.auth_relogin_retries:
                time.sleep(min(0.5 * attempt, 2.0))

        raise SyabasError(
            "Syabas99 session could not be reclaimed after immediate re-login attempts. "
            f"Last API response: {self._message(last_payload)}"
        )

    def session_healthcheck(self) -> None:
        form = {
            "includeSiteName": "1",
            "module": "/merchants/get",
        }
        self._authenticated_post(form)

    @staticmethod
    def _fmt_dt(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def transaction_totals(self, start: datetime, end: datetime) -> Totals:
        form = {
            "pageIndex": "0",
            "includeAdmin": "1",
            "background": "0",
            "transactionId": "",
            "name": "",
            "type": "DEPOSIT",
            "sDate": self._fmt_dt(start),
            "eDate": self._fmt_dt(end),
            "sCash": "",
            "eCash": "",
            "status": "COMPLETED",
            "agent": "",
            "bankId": "",
            "otherInfo": "",
            "module": "/transactions/getAllTransactions",
        }
        payload = self._authenticated_post(form)
        data = payload.get("data") or {}
        if "totalCount" not in data or "totalAmount" not in data:
            raise SyabasError(
                "Transaction API response missing totalCount/totalAmount"
            )
        return Totals.from_values(
            data.get("totalCount"),
            data.get("totalAmount"),
        )

    def daily_report_totals(self, report_date: datetime) -> Totals:
        date_text = report_date.strftime("%Y-%m-%d")
        form = {
            "sDate": date_text,
            "eDate": date_text,
            "period": "Daily",
            "type": "ALL",
            "module": "/reports/transactions",
        }
        payload = self._authenticated_post(form)
        data = payload.get("data") or {}
        day = data.get(date_text) or {}
        dep = day.get("DEPOSIT") or {}
        if "count" not in dep or "amount" not in dep:
            raise SyabasError(
                f"Daily report response missing DEPOSIT totals for {date_text}"
            )
        return Totals.from_values(
            dep.get("count"),
            dep.get("amount"),
        )
