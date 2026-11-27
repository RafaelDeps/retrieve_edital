"""
Envio ao grupo do Telegram (spec 012).

Sem `TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`, nada é enviado (modo prévia). O
token nunca aparece no log: os erros mostram só o código HTTP e a descrição da API.
"""

import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ENV = "TELEGRAM_CHAT_ID"
API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SEC = 20
ATTEMPTS = 2
RETRY_WAIT_SEC = 3


class TelegramError(Exception):
    """O Telegram recusou ou não respondeu."""


class TelegramClient:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, session=None, sleep=time.sleep):
        self.token = (token if token is not None else os.getenv(TOKEN_ENV) or "").strip()
        self.chat_id = (chat_id if chat_id is not None else os.getenv(CHAT_ENV) or "").strip()
        self.session = session or requests
        self.sleep = sleep

    @property
    def can_send(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
        last = ""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self.session.post(API_URL.format(token=self.token), data=payload, timeout=TIMEOUT_SEC)
                if 200 <= response.status_code < 300:
                    return
                try:
                    description = response.json().get("description", "")
                except ValueError:
                    description = ""
                last = f"HTTP {response.status_code} {description}".strip()
            except requests.RequestException as exc:
                last = type(exc).__name__
            logger.warning("Telegram: envio falhou (tentativa %s/%s): %s", attempt, ATTEMPTS, last)
            if attempt < ATTEMPTS:
                self.sleep(RETRY_WAIT_SEC)
        raise TelegramError(last)
