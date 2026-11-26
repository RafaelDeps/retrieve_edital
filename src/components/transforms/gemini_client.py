"""
Provedor Gemini (padrão) para a extração por IA, no nível gratuito.

- O texto do PDF é extraído localmente e só o texto é enviado (o PDF não sai da máquina).
- As chamadas respeitam um intervalo mínimo, compartilhado pelas threads.
- 429 por minuto: espera o tempo indicado pelo Google e tenta de novo, com teto.
- Cota diária esgotada, limite por minuto persistente ou chave inválida: a IA fica
  indisponível no resto da execução e `AIUnavailableError` sobe para quem chamou,
  que adia o item (constituição 1.1.0, Princípio V).

A chave nunca entra em mensagem de log nem de exceção.
"""

import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from google import genai
from google.genai import errors, types

from src.core.interfaces import IExtractionService
from src.components.transforms import extraction_prompts
from src.components.transforms.extraction_prompts import (
    SYSTEM_CLASSIFY,
    SYSTEM_EXTRACTION,
    SYSTEM_FINEP,
    build_classify_titles_prompt,
    build_extraction_prompt,
    build_finep_prompt,
    heuristic_title_classification,
    map_to_domain,
    normalize_finep_category,
    OffTopicError,
    theme_verdict,
)
from src.components.transforms.extraction_service import GEMINI_KEY_ENV, AIUnavailableError

logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
# 5 s = 12 chamadas por minuto, com folga abaixo das 15 do nível gratuito do
# Flash-Lite. Com 4 s o ritmo ficava no próprio limite, e retentativas somadas
# geravam 429 por minuto (visto na coleta completa de 25/11/2026).
DEFAULT_MIN_INTERVAL_SEC = 5.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_MAX_TOTAL_WAIT_SEC = 300.0
DEFAULT_TIMEOUT_SEC = 120.0
DEFAULT_RETRY_DELAY_SEC = 10.0

REASON_DAILY_QUOTA = "cota diária esgotada"
REASON_PERSISTENT_RATE_LIMIT = "limite por minuto persistente (tratado como cota diária)"
REASON_INVALID_KEY = "chave do Gemini inválida"

ERROR_DAILY = "daily"
ERROR_PER_MINUTE = "per_minute"
ERROR_AUTH = "auth"
ERROR_TRANSIENT = "transient"
ERROR_ITEM = "item"

DAILY_QUOTA_MARKER = "perday"
AUTH_REASONS = ("API_KEY_INVALID", "PERMISSION_DENIED", "UNAUTHENTICATED")
TRANSIENT_STATUS_CODES = (500, 502, 503, 504)
# Exceções de rede do transporte HTTP do SDK (httpx e afins), reconhecidas pelo
# nome para não depender de qual biblioteca o SDK usa por baixo.
NETWORK_ERROR_SUFFIXES = ("TimeoutException", "TransportError", "NetworkError", "ConnectError")
_DURATION_PATTERN = re.compile(r"^\s*([\d.]+)\s*s\s*$")


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s inválido (%r); usando o padrão %s.", name, raw, default)
        return default
    if value < 0:
        logger.warning("%s negativo (%r); usando o padrão %s.", name, raw, default)
        return default
    return value


def _error_details(exc: Exception) -> List[Dict[str, Any]]:
    """Os itens de `error.details` do JSON de erro do Google, quando existem."""
    payload = getattr(exc, "details", None)
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if not isinstance(payload, dict):
        return []
    error = payload.get("error", payload)
    details = error.get("details") if isinstance(error, dict) else None
    return [d for d in details if isinstance(d, dict)] if isinstance(details, list) else []


def _quota_ids(details: Iterable[Dict[str, Any]]) -> List[str]:
    ids = []
    for detail in details:
        for violation in detail.get("violations") or []:
            if isinstance(violation, dict) and violation.get("quotaId"):
                ids.append(str(violation["quotaId"]))
    return ids


def _is_network_error(exc: Exception) -> bool:
    """Timeout ou falha de conexão, venha do Python ou do transporte HTTP do SDK."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return any(
        name.endswith(NETWORK_ERROR_SUFFIXES) for name in (c.__name__ for c in type(exc).__mro__)
    )


def classify_api_error(exc: Exception) -> str:
    """Separa o erro em cota diária, limite por minuto, chave, transitório ou erro do item."""
    if _is_network_error(exc):
        return ERROR_TRANSIENT
    code = getattr(exc, "code", None)
    if not isinstance(exc, errors.APIError) or code is None:
        return ERROR_ITEM
    details = _error_details(exc)
    if code == 429:
        if any(DAILY_QUOTA_MARKER in quota.lower() for quota in _quota_ids(details)):
            return ERROR_DAILY
        return ERROR_PER_MINUTE
    reasons = {str(d.get("reason", "")) for d in details}
    status = str(getattr(exc, "status", "") or "")
    if code in (401, 403) or reasons & set(AUTH_REASONS) or status in AUTH_REASONS:
        return ERROR_AUTH
    if code in TRANSIENT_STATUS_CODES:
        return ERROR_TRANSIENT
    return ERROR_ITEM


def retry_delay_seconds(exc: Exception, default: float = DEFAULT_RETRY_DELAY_SEC) -> float:
    """Lê `RetryInfo.retryDelay` ("37s"); sem ele, o padrão."""
    for detail in _error_details(exc):
        delay = detail.get("retryDelay")
        if isinstance(delay, str):
            match = _DURATION_PATTERN.match(delay)
            if match:
                return float(match.group(1))
    return default


def _describe(exc: Exception) -> str:
    """Resumo do erro sem o corpo da resposta nem cabeçalhos (onde nada sensível deve vazar)."""
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    if code is not None:
        return f"{code} {status or ''}".strip()
    return type(exc).__name__


class RateLimiter:
    """Garante um intervalo mínimo entre chamadas, valendo para todas as threads."""

    def __init__(self, min_interval: float, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_call: Optional[float] = None

    def wait(self) -> None:
        with self._lock:
            if self._last_call is not None:
                remaining = self.min_interval - (self._clock() - self._last_call)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_call = self._clock()


class GeminiExtractionService(IExtractionService):
    """Extração por IA com o Gemini (texto local → JSON do edital)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        client: Any = None,
        model: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.model = model or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        self.max_retries = int(_env_number("GEMINI_MAX_RETRIES", DEFAULT_MAX_RETRIES))
        self.max_total_wait = _env_number("GEMINI_MAX_TOTAL_WAIT_SEC", DEFAULT_MAX_TOTAL_WAIT_SEC)
        timeout_sec = _env_number("GEMINI_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)
        if client is None:
            key = (api_key or os.getenv(GEMINI_KEY_ENV) or "").strip()
            if not key:
                raise ValueError(f"{GEMINI_KEY_ENV} ausente.")
            client = genai.Client(
                api_key=key, http_options=types.HttpOptions(timeout=int(timeout_sec * 1000))
            )
        self.client = client
        self._sleep = sleep
        self._limiter = RateLimiter(
            _env_number("GEMINI_MIN_INTERVAL_SEC", DEFAULT_MIN_INTERVAL_SEC), clock, sleep
        )
        self._state_lock = threading.Lock()
        self._unavailable_reason: Optional[str] = None

    # ---- estado ----------------------------------------------------------

    @property
    def unavailable_reason(self) -> Optional[str]:
        return self._unavailable_reason

    def _mark_unavailable(self, reason: str) -> AIUnavailableError:
        with self._state_lock:
            first_time = self._unavailable_reason is None
            if first_time:
                self._unavailable_reason = reason
            reason = self._unavailable_reason
        if first_time:
            logger.error(
                "IA indisponível no resto desta execução: %s. Os itens restantes serão adiados.",
                reason,
            )
        return AIUnavailableError(reason)

    # ---- chamada com limites ----------------------------------------------

    def _generate(self, system: str, prompt: str, context: str) -> str:
        waited = 0.0
        attempt = 0
        while True:
            if self._unavailable_reason is not None:
                raise AIUnavailableError(self._unavailable_reason)
            self._limiter.wait()
            try:
                response = self.client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=system,
                        response_mime_type="application/json",
                        temperature=0,
                    ),
                )
                return response.text or ""
            except Exception as exc:
                kind = classify_api_error(exc)
                if kind == ERROR_DAILY:
                    raise self._mark_unavailable(REASON_DAILY_QUOTA) from None
                if kind == ERROR_AUTH:
                    raise self._mark_unavailable(REASON_INVALID_KEY) from None
                if kind not in (ERROR_PER_MINUTE, ERROR_TRANSIENT):
                    raise
                remaining = self.max_total_wait - waited
                if attempt >= self.max_retries or remaining <= 0:
                    if kind == ERROR_PER_MINUTE:
                        raise self._mark_unavailable(REASON_PERSISTENT_RATE_LIMIT) from None
                    logger.error(
                        "Gemini indisponível temporariamente (%s) após %s tentativas: %s",
                        _describe(exc), attempt + 1, context,
                    )
                    raise
                delay = min(retry_delay_seconds(exc), remaining)
                attempt += 1
                logger.warning(
                    "Gemini %s em %s; nova tentativa %s/%s em %.0fs.",
                    _describe(exc), context, attempt, self.max_retries, delay,
                )
                self._sleep(delay)
                waited += delay

    # ---- usos --------------------------------------------------------------

    def extract_from_pdf(self, pdf_bytes: bytes, filename: str, check_theme: bool = False):
        text = extraction_prompts.extract_text_with_pdfplumber(pdf_bytes)
        return self.extract_from_text(text, filename, check_theme=check_theme)

    def extract_from_text(self, text: str, filename: str, check_theme: bool = False):
        if not (text or "").strip():
            logger.warning("Falha de extração: PDF sem texto (%s); IA não chamada.", filename)
            return None
        try:
            raw = self._generate(
                SYSTEM_EXTRACTION, build_extraction_prompt(text, check_theme), f"extract {filename}"
            )
            data = json.loads(raw)
            if check_theme:
                is_funding, motivo = theme_verdict(data)
                if not is_funding:
                    raise OffTopicError(motivo or "não é oportunidade de fomento")
            return map_to_domain(data)
        except (AIUnavailableError, OffTopicError):
            raise
        except json.JSONDecodeError:
            logger.error("Falha de extração: resposta do Gemini não é JSON (%s).", filename)
            return None
        except Exception as exc:
            logger.error("Falha de extração com o Gemini (%s): %s", filename, _describe(exc))
            return None

    def classify_document_titles(self, titles: List[str]) -> Dict[str, str]:
        if not titles:
            return {}
        try:
            raw = self._generate(
                SYSTEM_CLASSIFY, build_classify_titles_prompt(titles), "classify_document_titles"
            )
            classification = json.loads(raw)
            if not isinstance(classification, dict):
                raise ValueError("classificação não é um objeto JSON")
            logger.info("Classified %s titles: %s", len(titles), classification)
            return classification
        except Exception as exc:
            logger.warning(
                "Classificação de títulos pelo Gemini falhou (%s); usando a heurística.",
                getattr(exc, "reason", None) or _describe(exc),
            )
            return heuristic_title_classification(titles)

    def categorize_finep_by_description(self, description: str) -> str:
        if not (description or "").strip():
            return extraction_prompts.FINEP_DEFAULT_CATEGORY
        try:
            raw = self._generate(SYSTEM_FINEP, build_finep_prompt(description), "categorize_finep")
            data = json.loads(raw or "{}")
            return normalize_finep_category(data.get("categoria") if isinstance(data, dict) else "")
        except Exception as exc:
            logger.warning(
                "Categorização FINEP pelo Gemini falhou (%s).",
                getattr(exc, "reason", None) or _describe(exc),
            )
            return ""
