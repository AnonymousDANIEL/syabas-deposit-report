from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class MessageRef:
    chat_id: str
    message_id: int
    updated_at: str


class StateStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"messages": {}, "last_error": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("state root is not object")
            data.setdefault("messages", {})
            data.setdefault("last_error", {})
            return data
        except Exception:
            # Never block report generation because of a damaged state file.
            return {"messages": {}, "last_error": {}}

    def save(self, data: dict[str, Any]) -> None:
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)

    def get_message(self, report_date: str) -> MessageRef | None:
        data = self.load()
        raw = (data.get("messages") or {}).get(report_date)
        if not raw:
            return None
        try:
            return MessageRef(
                chat_id=str(raw["chat_id"]),
                message_id=int(raw["message_id"]),
                updated_at=str(raw.get("updated_at", "")),
            )
        except Exception:
            return None

    def set_message(self, report_date: str, chat_id: str, message_id: int) -> None:
        data = self.load()
        messages = data.setdefault("messages", {})
        messages[report_date] = asdict(
            MessageRef(
                chat_id=str(chat_id),
                message_id=int(message_id),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        # Keep a small rolling state only.
        if len(messages) > 14:
            for key in sorted(messages.keys())[:-14]:
                messages.pop(key, None)
        self.save(data)


    def get_last_successful_slot(self) -> str:
        data = self.load()
        return str(data.get("last_successful_slot", ""))

    def set_last_successful_slot(self, slot_key: str) -> None:
        data = self.load()
        data["last_successful_slot"] = str(slot_key)
        data["last_successful_slot_at"] = datetime.now(timezone.utc).isoformat()
        self.save(data)

    def get_last_error_fingerprint(self) -> str:
        data = self.load()
        return str((data.get("last_error") or {}).get("fingerprint", ""))

    def set_last_error_fingerprint(self, fingerprint: str) -> None:
        data = self.load()
        data["last_error"] = {
            "fingerprint": fingerprint,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save(data)

    def clear_last_error(self) -> None:
        data = self.load()
        data["last_error"] = {}
        self.save(data)
