import os
import io
import logging
import base64
import json
import time
from typing import Optional, Dict, Any, List, Callable, TypeVar

try:
    from mistralai import Mistral  # type: ignore[attr-defined]
except ImportError:
    try:
        from mistralai.client import Mistral  # type: ignore[attr-defined]
    except ImportError:
        from mistralai.client import MistralClient as Mistral

from src.core.interfaces import IExtractionService
from src.domain.models import EditalDomain
from src.components.transforms.extraction_prompts import (
    FINEP_CATEGORIES,
    SYSTEM_CLASSIFY,
    SYSTEM_EXTRACTION,
    SYSTEM_FINEP,
    build_classify_titles_prompt,
    build_extraction_prompt,
    build_finep_prompt,
    extract_text_with_pdfplumber,
    heuristic_title_classification,
    map_schedule_item,
    map_to_domain,
    normalize_finep_category,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Retry config for rate limit (429)
RATE_LIMIT_MAX_RETRIES = 10
RATE_LIMIT_INITIAL_WAIT_SEC = 60
RATE_LIMIT_BACKOFF_FACTOR = 2.0

# Teto de espera acumulada por chamada. Contar tentativas não limita o tempo: dez
# retentativas dobrando a partir de 60s somam mais de 17 horas, muito além de
# qualquer janela de job aceitável. O que importa é quanto tempo a chamada pode
# segurar o fluxo, então o corte é por tempo — a última espera é encurtada para
# não passar do teto, e depois disso a falha sobe para o chamador.
RATE_LIMIT_MAX_TOTAL_WAIT_SEC = 15 * 60


DEFAULT_OCR_MODEL = "mistral-ocr-latest"
DEFAULT_LLM_MODEL = "ministral-14b-latest"


def _get_response_headers(exc: Exception) -> Dict[str, str]:
    """Extrai headers HTTP de uma exceção do SDK Mistral (quando disponíveis)."""
    raw_headers = getattr(exc, "headers", None)
    if not raw_headers:
        raw_response = getattr(exc, "raw_response", None) or getattr(exc, "response", None)
        raw_headers = getattr(raw_response, "headers", None)
    if not raw_headers:
        return {}
    try:
        return {str(k).lower(): str(v).strip() for k, v in dict(raw_headers).items()}
    except Exception:
        return {}


def _is_non_retryable_quota_or_tier_error(exc: Exception) -> bool:
    """
    Detecta bloqueios permanentes do plano/tier em que esperar não resolve:
    - HTTP 403 / `tier_not_allowed` / `code: 1910` (modelo indisponível no plano)
    - HTTP 429 com header `x-ratelimit-limit-*` igual a `0` (limite zerado no plano)
    """
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        raw_response = getattr(exc, "raw_response", None) or getattr(exc, "response", None)
        status_code = getattr(raw_response, "status_code", None)

    text = str(exc).lower()
    if status_code == 403 or "tier_not_allowed" in text or '"code":"1910"' in text:
        return True

    headers = _get_response_headers(exc)
    for key, value in headers.items():
        if key.startswith("x-ratelimit-limit-") and value == "0":
            return True
    return False


def _is_rate_limit_error(exc: Exception) -> bool:
    if _is_non_retryable_quota_or_tier_error(exc):
        return False
    s = str(exc).lower()
    return "429" in s or "rate" in s and "limit" in s or "rate_limited" in s


# Nome antigo, mantido para quem importa daqui.
_extract_text_with_pdfplumber = extract_text_with_pdfplumber


def _call_with_rate_limit_retry(
    fn: Callable[[], T],
    context: str = "",
) -> T:
    """
    Executa fn(); em caso de 429 (rate limit) tenta de novo com backoff exponencial.

    O corte é por **tempo acumulado**, não por número de tentativas: contar
    tentativas não limita a duração, e dez retentativas dobrando a partir de 60s
    somariam mais de 17 horas. Ao esgotar o teto, a exceção sobe para o chamador,
    que registra a falha — e o canário do runner a torna visível.
    """
    last_exc = None
    total_waited = 0.0
    for attempt in range(RATE_LIMIT_MAX_RETRIES):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if _is_non_retryable_quota_or_tier_error(e):
                logger.error(
                    "Erro permanente de tier/limite zerado na Mistral (sem retry). Contexto: %s | Erro: %s",
                    context or "Mistral API",
                    e,
                )
                raise
            if not _is_rate_limit_error(e) or attempt == RATE_LIMIT_MAX_RETRIES - 1:
                raise

            remaining = RATE_LIMIT_MAX_TOTAL_WAIT_SEC - total_waited
            if remaining <= 0:
                logger.error(
                    "Rate limit persistente: teto de %.0fs de espera esgotado após "
                    "%s tentativas. Desistindo. Contexto: %s",
                    RATE_LIMIT_MAX_TOTAL_WAIT_SEC,
                    attempt,
                    context or "Mistral API",
                )
                raise

            wait_sec = RATE_LIMIT_INITIAL_WAIT_SEC * (RATE_LIMIT_BACKOFF_FACTOR ** attempt)
            # Encurta a última espera para não estourar o teto.
            wait_sec = min(wait_sec, remaining)
            logger.warning(
                "Rate limit hit (%s). Waiting %.0fs before retry %s/%s "
                "(%.0fs de %.0fs do teto usados). Context: %s",
                e,
                wait_sec,
                attempt + 1,
                RATE_LIMIT_MAX_RETRIES,
                total_waited + wait_sec,
                RATE_LIMIT_MAX_TOTAL_WAIT_SEC,
                context or "Mistral API",
            )
            time.sleep(wait_sec)
            total_waited += wait_sec
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Unexpected retry exit")


class MistralExtractionService(IExtractionService):
    """
    Service to extract structured data from PDF editais using Mistral OCR and LLM.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("MISTRAL_API_KEY")
        if not self.api_key:
            raise ValueError("MISTRAL_API_KEY is not set.")
        self.client = Mistral(api_key=self.api_key)
        self.ocr_model = os.getenv("MISTRAL_OCR_MODEL", DEFAULT_OCR_MODEL)
        self.llm_model = os.getenv("MISTRAL_LLM_MODEL", DEFAULT_LLM_MODEL)
        self._ocr_unavailable = False

    def _extract_pdf_text(self, pdf_bytes: bytes, filename: str, attempt: int) -> str:
        """
        Extrai texto do PDF via Mistral OCR; se o OCR estiver bloqueado no plano
        (ex.: x-ratelimit-limit-req-minute: 0 ou 403 tier_not_allowed), faz
        fallback imediato para extração local com pdfplumber.
        """
        if self._ocr_unavailable:
            logger.info("Mistral OCR indisponível neste plano; extraindo texto de %s via pdfplumber.", filename)
            return _extract_text_with_pdfplumber(pdf_bytes)

        uploaded_file_id = None
        try:
            logger.info(f"Uploading PDF {filename} to Mistral for OCR (Attempt {attempt})...")
            uploaded_file = _call_with_rate_limit_retry(
                lambda: self.client.files.upload(
                    file={
                        "file_name": filename,
                        "content": pdf_bytes,
                    },
                    purpose="ocr",
                ),
                context=f"upload {filename}",
            )
            uploaded_file_id = uploaded_file.id

            logger.info(f"File uploaded with ID: {uploaded_file_id}. Processing OCR...")
            ocr_response = _call_with_rate_limit_retry(
                lambda: self.client.ocr.process(
                    model=self.ocr_model,
                    document={
                        "type": "file",
                        "file_id": uploaded_file_id,
                    },
                ),
                context=f"OCR {filename}",
            )

            full_ocr_text = ""
            for page in ocr_response.pages:
                full_ocr_text += f"\n{page.markdown}"
            return full_ocr_text
        except Exception as exc:
            if _is_non_retryable_quota_or_tier_error(exc):
                self._ocr_unavailable = True
                logger.warning(
                    "Mistral OCR bloqueado/limite 0 no plano atual (%s). "
                    "Usando fallback local com pdfplumber para %s.",
                    exc,
                    filename,
                )
                return _extract_text_with_pdfplumber(pdf_bytes)
            raise
        finally:
            if uploaded_file_id:
                try:
                    self.client.files.delete(file_id=uploaded_file_id)
                except Exception as cleanup_err:
                    logger.warning(f"Failed to delete uploaded file {uploaded_file_id}: {cleanup_err}")

    def extract_from_pdf(self, pdf_bytes: bytes, filename: str) -> Optional[EditalDomain]:
        """
        Processes PDF bytes through Mistral OCR (or local pdfplumber fallback) and
        extracts structured data using LLM.
        """
        return self._extract(
            lambda attempt: self._extract_pdf_text(pdf_bytes, filename, attempt), filename
        )

    def extract_from_text(self, text: str, filename: str) -> Optional[EditalDomain]:
        """Só a etapa de LLM, para um documento que já chegou como texto (spec 006)."""
        return self._extract(lambda attempt: text or "", filename)

    def _extract(self, text_for_attempt, filename: str) -> Optional[EditalDomain]:
        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                full_ocr_text = text_for_attempt(attempt)
                if not full_ocr_text.strip():
                    logger.warning("Nenhum texto extraído do PDF %s.", filename)
                    return None

                logger.info("Text extraction completed. Extracting structured data...")

                # Extract structured data via LLM (with rate-limit retry)
                time.sleep(2)
                prompt = self._get_extraction_prompt(full_ocr_text)

                response = _call_with_rate_limit_retry(
                    lambda: self.client.chat.complete(
                        model=self.llm_model,
                        messages=[
                            {"role": "system", "content": SYSTEM_EXTRACTION},
                            {"role": "user", "content": prompt},
                        ],
                        response_format={"type": "json_object"},
                    ),
                    context=f"extract LLM {filename}",
                )

                raw_json = response.choices[0].message.content
                extracted_data = json.loads(raw_json)
                return self._map_to_domain(extracted_data)

            except Exception as e:
                logger.error("Mistral extraction failed for %s on attempt %s: %s", filename, attempt, e)
                if _is_non_retryable_quota_or_tier_error(e):
                    logger.error(
                        "Erro permanente de tier/limite zerado para %s. Abortando sem novas tentativas.",
                        filename,
                    )
                    return None
                if attempt >= max_retries:
                    logger.error("Max retries reached for %s. Returning None.", filename)
                    return None
                wait_time = 120 if _is_rate_limit_error(e) else attempt * 15
                logger.info("Waiting %s seconds before next attempt...", wait_time)
                time.sleep(wait_time)
    
    def classify_document_titles(self, titles: List[str]) -> Dict[str, str]:
        """
        Uses Mistral to classify a list of document titles within a notice group.
        Returns a mapping of title -> document_type (edital, anexo, alteração).
        """
        if not titles:
            return {}

        prompt = build_classify_titles_prompt(titles)
        try:
            response = _call_with_rate_limit_retry(
                lambda: self.client.chat.complete(
                    model=self.llm_model,
                    messages=[
                        {"role": "system", "content": SYSTEM_CLASSIFY},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                ),
                context="classify_document_titles",
            )
            raw_json = response.choices[0].message.content
            classification = json.loads(raw_json)
            logger.info("Classified %s titles: %s", len(titles), classification)
            return classification
        except Exception as e:
            logger.error(f"Failed to classify titles: {e}")
            return heuristic_title_classification(titles)

    FINEP_CATEGORIES = FINEP_CATEGORIES

    def categorize_finep_by_description(self, description: str) -> str:
        """
        Classifica um edital FINEP em uma das categorias, com base na descrição.
        Retorna: "divulgação de conhecimento", "extensão" ou "inovação".
        """
        if not (description or "").strip():
            return "inovação"
        prompt = build_finep_prompt(description)
        try:
            response = _call_with_rate_limit_retry(
                lambda: self.client.chat.complete(
                    model=self.llm_model,
                    messages=[
                        {"role": "system", "content": SYSTEM_FINEP},
                        {"role": "user", "content": prompt},
                    ],
                    response_format={"type": "json_object"},
                ),
                context="categorize_finep",
            )
            data = json.loads(response.choices[0].message.content or "{}")
            return normalize_finep_category(data.get("categoria"))
        except Exception as e:
            # Devolve vazio, não um palpite: retornar "inovação" aqui tornava uma
            # chave de API inválida indistinguível de uma classificação real — o
            # chamador recebia um valor plausível e seguia adiante. Cabe a ele
            # decidir o fallback.
            logger.warning("Mistral categorize_finep_by_description failed: %s", e)
            return ""

    def _get_extraction_prompt(self, ocr_text: str) -> str:
        return build_extraction_prompt(ocr_text)

    @staticmethod
    def _map_schedule_item(item: Dict[str, Any]) -> Dict[str, str]:
        return map_schedule_item(item)

    def _map_to_domain(self, data: Dict[str, Any]) -> EditalDomain:
        return map_to_domain(data)
