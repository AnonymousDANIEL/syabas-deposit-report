from __future__ import annotations

import logging
from typing import Any

import requests

from config import Config
from state import StateStore

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramBot:
    def __init__(self, cfg: Config, state: StateStore):
        self.cfg = cfg
        self.state = state
        self.base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
        self.session = requests.Session()

    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.post(
            f"{self.base}/{method}",
            json=payload,
            timeout=self.cfg.request_timeout_seconds,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(f"Telegram returned non-JSON HTTP {response.status_code}") from exc

        if not data.get("ok"):
            desc = str(data.get("description", "Unknown Telegram error"))
            # Editing the same exact text is harmless and should be treated as success.
            if "message is not modified" in desc.lower():
                return {"ok": True, "result": None}
            raise TelegramError(f"Telegram {method} failed: {desc}")
        return data

    def send_message(self, chat_id: str, text: str, *, silent: bool = True, parse_mode: str | None = None) -> int:
        if not str(chat_id).strip():
            raise TelegramError("TELEGRAM_CHAT_ID is empty")
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        data = self._call("sendMessage", payload)
        result = data.get("result") or {}
        if "message_id" not in result:
            raise TelegramError("Telegram sendMessage response missing message_id")
        return int(result["message_id"])

    def edit_message(self, chat_id: str, message_id: int, text: str, *, parse_mode: str | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        self._call("editMessageText", payload)

    def upsert_daily_report(self, report_date: str, text: str) -> int:
        ref = self.state.get_message(report_date)
        if ref:
            try:
                self.edit_message(ref.chat_id, ref.message_id, text)
                self.state.set_message(report_date, ref.chat_id, ref.message_id)
                log.info("Edited Telegram report message %s", ref.message_id)
                return ref.message_id
            except TelegramError as exc:
                msg = str(exc).lower()
                if "message to edit not found" not in msg and "message can't be edited" not in msg:
                    raise
                log.warning("Saved Telegram message cannot be edited; sending a replacement")

        message_id = self.send_message(self.cfg.telegram_chat_id, text, silent=True)
        self.state.set_message(report_date, self.cfg.telegram_chat_id, message_id)
        log.info("Sent new Telegram report message %s", message_id)
        return message_id

    def alert_once(self, fingerprint: str, text: str) -> None:
        if not self.cfg.telegram_alert_chat_id:
            return
        if self.state.get_last_error_fingerprint() == fingerprint:
            return
        self.send_message(self.cfg.telegram_alert_chat_id, text, silent=False)
        self.state.set_last_error_fingerprint(fingerprint)

    def clear_error(self) -> None:
        self.state.clear_last_error()

    def get_updates(self) -> list[dict[str, Any]]:
        data = self._call("getUpdates", {"limit": 100, "timeout": 0})
        return list(data.get("result") or [])
