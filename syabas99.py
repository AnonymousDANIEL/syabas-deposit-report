from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

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

    Important stability behavior:
    - Login once on startup / before a report.
    - A lightweight authenticated health check runs in daemon mode.
    - If another login rotates/invalidates this token, the next authenticated call
      detects the rejection and FORCE-LOGINS immediately, then retries the same call.
    - Report requests use the same recovery path, so a token rotation in the middle
      of an hourly calculation is recovered without waiting for the next scheduler tick.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "Syabas99TelegramReport/4.0",
            }
        )
        self.access_id = ""
        self.access_token = ""
        self.last_login_monotonic = 0.0

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
        return str(payload.get("message") or payload.get("error") or payload)

    def login(self, *, reason: str = "normal") -> None:
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
        if not self._is_success(payload):
            raise SyabasError(
                "Syabas99 login failed. If the site now requires CAPTCHA/2FA/trackingCode, "
                f"update the related environment variables. API response: {self._message(payload)}"
            )

        data = payload.get("data") or {}
        access_id = data.get("id")
        token = data.get("token")
        if not access_id or not token:
            raise SyabasError("Login succeeded but response did not contain data.id/data.token")

        self.access_id = str(access_id)
        self.access_token = str(token)
        self.last_login_monotonic = time.monotonic()
        if reason == "takeover":
            log.warning("SESSION TAKEOVER OK - Syabas99 force-login reclaimed the account")
        else:
            log.info("Syabas99 login OK")

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

        # Do not wait for the next Railway tick. If another login has rotated the
        # current token, immediately login back and retry the exact same API call.
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
                # Very short backoff only; we intentionally do not wait for a cron cycle.
                time.sleep(min(0.5 * attempt, 2.0))

        raise SyabasError(
            "Syabas99 session could not be reclaimed after immediate re-login attempts. "
            f"Last API response: {self._message(last_payload)}"
        )

    def session_healthcheck(self) -> None:
        """
        Lightweight auth probe. If the token has been invalidated by another login,
        _authenticated_post() immediately force-logins and retries.
        """
        form = {
            "includeSiteName": "1",
            "module": "/merchants/get",
        }
        self._authenticated_post(form)

    @staticmethod
    def _fmt_dt(value: datetime) -> str:
        return value.strftime("%Y-%m-%d %H:%M:%S")

    def transaction_totals(self, start: datetime, end: datetime) -> Totals:
        """
        Uses /transactions/getAllTransactions only for server-calculated
        totalCount/totalAmount. Transaction rows are not stored.
        """
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
            raise SyabasError("Transaction API response missing totalCount/totalAmount")
        return Totals.from_values(data.get("totalCount"), data.get("totalAmount"))

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
            raise SyabasError(f"Daily report response missing DEPOSIT totals for {date_text}")
        return Totals.from_values(dep.get("count"), dep.get("amount"))
