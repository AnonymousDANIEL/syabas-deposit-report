from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or value.strip() == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return "" if value is None else value.strip()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw.strip() == "" else int(raw)


@dataclass(frozen=True)
class Config:
    site_api_url: str
    site_username: str
    site_password: str
    site_merchant_id: str
    site_tracking_code: str
    site_passcode_2fa: str
    site_captcha_output: str
    site_login_url: str
    auto_tracking_code: bool
    browser_login_timeout_seconds: int

    telegram_bot_token: str
    telegram_chat_id: str
    telegram_alert_chat_id: str

    target_count: int
    target_deposit: Decimal
    report_timezone: str
    report_line_style: str

    state_path: str
    request_timeout_seconds: int
    request_retries: int
    validation_retries: int
    validation_retry_delay_seconds: int
    strict_final_daily_validation: bool
    show_validation_status: bool
    dry_run: bool
    job_timeout_seconds: int
    force_run: bool
    prelogin_enabled: bool
    prelogin_minute: int
    run_forever: bool
    session_guard_enabled: bool
    session_guard_seconds: int
    auth_relogin_retries: int
    report_grace_seconds: int
    report_retry_seconds: int

    @classmethod
    def from_env(cls) -> "Config":
        style = _env("REPORT_LINE_STYLE", "full").lower()
        if style not in {"full", "simple"}:
            raise RuntimeError("REPORT_LINE_STYLE must be 'full' or 'simple'")

        return cls(
            site_api_url=_env(
                "SITE_API_URL",
                "https://jksyab99.u55y38.com/api/v1/index.php",
            ),
            site_username=_env("SITE_USERNAME", required=True),
            site_password=_env("SITE_PASSWORD", required=True),
            site_merchant_id=_env("SITE_MERCHANT_ID", required=True),
            site_tracking_code=_env("SITE_TRACKING_CODE", ""),
            site_passcode_2fa=_env("SITE_PASSCODE_2FA", ""),
            site_captcha_output=_env("SITE_CAPTCHA_OUTPUT", ""),
            site_login_url=_env("SITE_LOGIN_URL", "https://jksyab99.u55y38.com/"),
            auto_tracking_code=_bool("AUTO_TRACKING_CODE", True),
            browser_login_timeout_seconds=max(10, _int("BROWSER_LOGIN_TIMEOUT_SECONDS", 30)),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", required=True),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID", ""),
            telegram_alert_chat_id=_env("TELEGRAM_ALERT_CHAT_ID", ""),
            target_count=_int("TARGET_COUNT", 8300),
            target_deposit=Decimal(_env("TARGET_DEPOSIT", "100000")),
            report_timezone=_env("REPORT_TIMEZONE", "Asia/Kuala_Lumpur"),
            report_line_style=style,
            state_path=_env("STATE_PATH", "/data/syabas_state.json"),
            request_timeout_seconds=_int("REQUEST_TIMEOUT_SECONDS", 30),
            request_retries=_int("REQUEST_RETRIES", 3),
            validation_retries=_int("VALIDATION_RETRIES", 3),
            validation_retry_delay_seconds=_int("VALIDATION_RETRY_DELAY_SECONDS", 4),
            strict_final_daily_validation=_bool("STRICT_FINAL_DAILY_VALIDATION", False),
            show_validation_status=_bool("SHOW_VALIDATION_STATUS", False),
            dry_run=_bool("DRY_RUN", False),
            job_timeout_seconds=_int("JOB_TIMEOUT_SECONDS", 240),
            force_run=_bool("FORCE_RUN", False),
            prelogin_enabled=_bool("PRELOGIN_ENABLED", False),
            prelogin_minute=_int("PRELOGIN_MINUTE", 55),
            run_forever=_bool("RUN_FOREVER", True),
            session_guard_enabled=_bool("SESSION_GUARD_ENABLED", True),
            session_guard_seconds=max(5, _int("SESSION_GUARD_SECONDS", 15)),
            auth_relogin_retries=max(1, _int("AUTH_RELOGIN_RETRIES", 5)),
            report_grace_seconds=max(0, _int("REPORT_GRACE_SECONDS", 0)),
            report_retry_seconds=max(5, _int("REPORT_RETRY_SECONDS", 30)),
        )
