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

    def _api_login(self, *, reason: str) -> None:
        """Legacy/static trackingCode login path."""
        form = {
            "username": self.cfg.site_username,
            "password": self.cfg.site_password,
            "passcode2fa": self.cfg.site_passcode_2fa,
            "trackingCode": self.cfg.site_tracking_code,
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
        Open the real login page in headless Chromium and let the site generate
        its current trackingCode. Capture the /users/login request + JSON response.

        This does not bypass CAPTCHA/2FA. If the site requires a human CAPTCHA or
        an interactive 2FA step, login fails clearly instead of pretending success.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise SyabasError(
                "AUTO_TRACKING_CODE is enabled but Playwright/Chromium is unavailable"
            ) from exc

        timeout_ms = self.cfg.browser_login_timeout_seconds * 1000
        captured: dict[str, Any] = {
            "payload": None,
            "tracking_code": "",
            "request_seen": False,
        }

        def is_login_request(response: Any) -> bool:
            try:
                req = response.request
                if req.method.upper() != "POST":
                    return False
                if "/api/v1/index.php" not in response.url:
                    return False
                raw = req.post_data or ""
                parsed = parse_qs(raw, keep_blank_values=True)
                module = (parsed.get("module") or [""])[0]
                return module == "/users/login"
            except Exception:
                return False

        def on_response(response: Any) -> None:
            if not is_login_request(response):
                return
            try:
                captured["request_seen"] = True
                raw = response.request.post_data or ""
                parsed = parse_qs(raw, keep_blank_values=True)
                captured["tracking_code"] = (
                    (parsed.get("trackingCode") or [""])[0]
                )
                payload = response.json()
                if isinstance(payload, dict):
                    captured["payload"] = payload
            except Exception as exc:
                log.warning("Could not parse browser login response: %s", exc)

        log.warning(
            "AUTO TRACKING LOGIN - opening Syabas99 login page to obtain fresh trackingCode"
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
            page.on("response", on_response)

            try:
                page.goto(
                    self.cfg.site_login_url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )

                # Give the page a moment to complete its IP/tracking initialization.
                page.wait_for_timeout(1200)

                username_selectors = [
                    'input[name="username"]',
                    'input#username',
                    'input[name="user"]',
                    'input[type="text"]',
                ]
                password_selectors = [
                    'input[name="password"]',
                    'input#password',
                    'input[type="password"]',
                ]

                def first_visible(selectors: list[str]):
                    for selector in selectors:
                        loc = page.locator(selector)
                        if loc.count() > 0:
                            first = loc.first
                            try:
                                if first.is_visible():
                                    return first
                            except Exception:
                                continue
                    return None

                user_input = first_visible(username_selectors)
                pass_input = first_visible(password_selectors)
                if user_input is None or pass_input is None:
                    raise SyabasError(
                        "Browser login page loaded, but username/password fields "
                        "could not be detected"
                    )

                user_input.fill(self.cfg.site_username)
                pass_input.fill(self.cfg.site_password)

                if self.cfg.site_passcode_2fa:
                    otp = first_visible(
                        [
                            'input[name="passcode2fa"]',
                            'input[name="otp"]',
                            'input[name="2fa"]',
                        ]
                    )
                    if otp is not None:
                        otp.fill(self.cfg.site_passcode_2fa)

                if self.cfg.site_captcha_output:
                    captcha = first_visible(
                        [
                            'input[name="captchaOutput"]',
                            'input[name="captcha"]',
                        ]
                    )
                    if captcha is not None:
                        captcha.fill(self.cfg.site_captcha_output)

                submit_selectors = [
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("Login")',
                    'button:has-text("LOGIN")',
                    'button:has-text("Sign In")',
                    'button:has-text("SIGN IN")',
                    '.btn-login',
                    '#login',
                ]
                submit = first_visible(submit_selectors)
                if submit is not None:
                    submit.click()
                else:
                    pass_input.press("Enter")

                deadline = time.monotonic() + self.cfg.browser_login_timeout_seconds
                while time.monotonic() < deadline:
                    if captured["payload"] is not None:
                        break
                    page.wait_for_timeout(200)

                payload = captured["payload"]
                if payload is None:
                    if captured["request_seen"]:
                        raise SyabasError(
                            "Browser saw /users/login but could not read its JSON response"
                        )
                    raise SyabasError(
                        "Browser login timed out before /users/login was observed. "
                        "The login page may have changed or may require CAPTCHA/2FA."
                    )

                tracking = str(captured["tracking_code"] or "")
                self.last_tracking_code = tracking
                log.info(
                    "AUTO TRACKING CODE captured successfully (length=%s)",
                    len(tracking),
                )
                self._accept_login_payload(payload, reason=reason)

                # Copy browser cookies into requests.Session in case the backend
                # starts depending on them in addition to accessId/accessToken.
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
            finally:
                context.close()
                browser.close()

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
