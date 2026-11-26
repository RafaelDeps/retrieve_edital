"""
Acesso ao www.gov.br com desvio pelo Jina Reader (issue #7, spec 006).

O www.gov.br derruba toda conexão vinda dos servidores do GitHub Actions, já no
handshake: qualquer identificação, IPv4, HTTP/1.1 e tentativas repetidas
(diagnóstico de 26/11/2026). Outros sites do governo respondem do mesmo servidor,
e do Brasil tudo funciona.

O fetcher tenta o acesso direto; se a conexão cair, refaz o mesmo acesso pelo
Jina Reader (https://r.jina.ai/<url>), que busca a página a partir dos
servidores dele: HTML original para páginas, texto para documentos. Detectado o
bloqueio, o resto da execução vai direto pelo Jina. Erro HTTP do próprio gov.br
(404, 500) não é bloqueio e sobe como antes.

Plano gratuito do Jina, sem cadastro: intervalo mínimo entre chamadas e novas
tentativas com teto. A chave opcional `JINA_API_KEY` só vai no cabeçalho e nunca
aparece em log.
"""

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from src.flow_health import warn_on_redirect

logger = logging.getLogger(__name__)

JINA_READER_URL = "https://r.jina.ai/"
JINA_KEY_ENV = "JINA_API_KEY"

DEFAULT_JINA_MIN_INTERVAL_SEC = 4.0
DEFAULT_JINA_MAX_RETRIES = 3
DEFAULT_JINA_MAX_TOTAL_WAIT_SEC = 120.0
DEFAULT_JINA_TIMEOUT_SEC = 60.0
DEFAULT_JINA_RETRY_DELAY_SEC = 10.0
JINA_RETRYABLE_STATUSES = (429, 500, 502, 503, 504)

MODE_DIRECT = "direct"
MODE_JINA = "jina"
MODE_FAILED = "failed"

REASON_RATE_LIMIT = "jina_429"
REASON_TIMEOUT = "jina_timeout"
REASON_CONNECTION = "jina_conexao"
REASON_EMPTY = "jina_vazio"

# Cabeçalho que o Jina põe antes do conteúdo em Markdown ("Title:", "URL Source:",
# "Number of Pages:"... até "Markdown Content:").
_JINA_HEADER = re.compile(r"\A.*?^Markdown Content:\s*\n?", re.DOTALL | re.MULTILINE)


class GovBrUnavailable(requests.RequestException):
    """O www.gov.br recusou a conexão e o desvio pelo Jina também falhou."""

    def __init__(self, reason: str, url: str):
        super().__init__(f"{reason} ({url})")
        self.reason = reason
        self.url = url


@dataclass
class FetchedDocument:
    content: Optional[bytes] = None  # acesso direto
    text: Optional[str] = None  # pelo Jina, sem o cabeçalho do Jina
    content_type: str = ""  # do acesso direto


def _env_number(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s inválido (%r); usando o padrão %s.", name, raw, default)
        return default
    return value if value >= 0 else default


def strip_jina_header(text: str) -> str:
    return _JINA_HEADER.sub("", text, count=1).strip()


def _is_connection_failure(exc: Exception) -> bool:
    """Conexão recusada, derrubada ou sem resposta: o sinal do bloqueio."""
    return isinstance(exc, (requests.ConnectionError, requests.ConnectTimeout))


class GovBrFetcher:
    """Acesso ao www.gov.br de uma fonte, numa execução."""

    def __init__(
        self,
        direct_session: Optional[requests.Session] = None,
        jina_session: Optional[requests.Session] = None,
        timeout: int = 30,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        source_name: str = "gov.br",
    ) -> None:
        self.direct_session = direct_session or requests.Session()
        self.jina_session = jina_session or requests.Session()
        self.timeout = timeout
        self.source_name = source_name
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_jina_call: Optional[float] = None
        self.min_interval = _env_number("JINA_MIN_INTERVAL_SEC", DEFAULT_JINA_MIN_INTERVAL_SEC)
        self.max_retries = int(_env_number("JINA_MAX_RETRIES", DEFAULT_JINA_MAX_RETRIES))
        self.max_total_wait = _env_number("JINA_MAX_TOTAL_WAIT_SEC", DEFAULT_JINA_MAX_TOTAL_WAIT_SEC)
        self.jina_timeout = _env_number("JINA_TIMEOUT_SEC", DEFAULT_JINA_TIMEOUT_SEC)
        self.mode = MODE_DIRECT
        self.jina_pages = 0
        self.failure_reason: Optional[str] = None

    # ---- API ----------------------------------------------------------------

    def get_html(self, url: str, listing: bool = False) -> str:
        response = self._direct(url)
        if response is not None:
            warn_on_redirect(url, response.url)
            return response.text
        return self._via_jina(url, as_html=True, listing=listing)

    def get_document(self, url: str) -> FetchedDocument:
        response = self._direct(url)
        if response is not None:
            headers = getattr(response, "headers", None) or {}
            return FetchedDocument(
                content=response.content,
                content_type=(headers.get("content-type") or "").lower(),
            )
        return FetchedDocument(text=self._via_jina(url, as_html=False, listing=False))

    # ---- acesso direto --------------------------------------------------------

    def _direct(self, url: str):
        """A resposta direta, ou None quando o caminho é o Jina."""
        if self.mode != MODE_DIRECT:
            return None
        try:
            response = self.direct_session.get(url, timeout=self.timeout)
            response.raise_for_status()
            return response
        except Exception as exc:
            if not _is_connection_failure(exc):
                raise
            self.mode = MODE_JINA
            logger.warning(
                "%s: www.gov.br recusou a conexão direta (%s); "
                "seguindo pelo Jina Reader no resto da execução.",
                self.source_name,
                type(exc).__name__,
            )
            return None

    # ---- Jina ------------------------------------------------------------------

    def _via_jina(self, url: str, as_html: bool, listing: bool) -> str:
        try:
            text = self._jina(url, as_html)
        except GovBrUnavailable as exc:
            self.failure_reason = exc.reason
            if listing:
                self.mode = MODE_FAILED
            logger.error(
                "%s: falha de rede: www.gov.br recusou a conexão e o desvio pelo "
                "Jina falhou (%s) em %s",
                self.source_name,
                exc.reason,
                url,
            )
            raise
        self.jina_pages += 1
        return text

    def _headers(self, as_html: bool) -> dict:
        headers = {}
        if as_html:
            headers["X-Return-Format"] = "html"
        key = (os.getenv(JINA_KEY_ENV) or "").strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _wait_interval(self) -> None:
        with self._lock:
            if self._last_jina_call is not None:
                remaining = self.min_interval - (self._clock() - self._last_jina_call)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_jina_call = self._clock()

    def _jina(self, url: str, as_html: bool) -> str:
        waited = 0.0
        attempt = 0
        while True:
            self._wait_interval()
            reason, delay = None, DEFAULT_JINA_RETRY_DELAY_SEC
            try:
                response = self.jina_session.get(
                    JINA_READER_URL + url,
                    headers=self._headers(as_html),
                    timeout=self.jina_timeout,
                )
            except requests.Timeout:
                reason = REASON_TIMEOUT
            except requests.RequestException:
                reason = REASON_CONNECTION
            else:
                status = getattr(response, "status_code", 200)
                if status == 200:
                    return self._validated(response.text or "", as_html, url)
                if status not in JINA_RETRYABLE_STATUSES:
                    raise GovBrUnavailable(f"jina_http_{status}", url)
                reason = REASON_RATE_LIMIT if status == 429 else f"jina_http_{status}"
                delay = _retry_after(response, delay)
            remaining = self.max_total_wait - waited
            if attempt >= self.max_retries or remaining <= 0:
                raise GovBrUnavailable(reason, url)
            delay = min(delay, remaining)
            attempt += 1
            logger.warning(
                "%s: Jina respondeu %s para %s; nova tentativa %s/%s em %.0fs.",
                self.source_name, reason, url, attempt, self.max_retries, delay,
            )
            self._sleep(delay)
            waited += delay

    @staticmethod
    def _validated(text: str, as_html: bool, url: str) -> str:
        if as_html:
            if "<html" not in text.lower():
                raise GovBrUnavailable(REASON_EMPTY, url)
            return text
        body = strip_jina_header(text)
        if not body:
            # Documento sem texto (PDF escaneado): o Jina respondeu, então não é
            # falha de rede. Segue vazio, como o pdfplumber faria no acesso
            # direto, e a extração registra "PDF sem texto".
            logger.info("Documento sem texto pelo Jina (provavelmente escaneado): %s", url)
        return body


def _retry_after(response, default: float) -> float:
    headers = getattr(response, "headers", None) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default
