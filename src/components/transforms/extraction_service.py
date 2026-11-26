"""
Escolha do provedor de IA e o estado "IA indisponível".

`get_extraction_service()` lê `LLM_PROVIDER` (gemini por padrão, ou mistral) e
devolve **uma instância por processo**: a fonte (classificação de títulos) e o
normalizador (leitura do edital) compartilham o mesmo ritmo de chamadas e o
mesmo estado de cota. Sem chave, ou com provedor desconhecido, devolve um serviço
indisponível em vez de derrubar o fluxo: os itens que dependem da IA são adiados
para a próxima execução (constituição 1.1.0, Princípio V).
"""

import logging
import os
import threading
from typing import Dict, List, Optional

from src.core.interfaces import IExtractionService
from src.components.transforms.extraction_prompts import heuristic_title_classification

logger = logging.getLogger(__name__)

PROVIDER_ENV = "LLM_PROVIDER"
PROVIDER_GEMINI = "gemini"
PROVIDER_MISTRAL = "mistral"
DEFAULT_PROVIDER = PROVIDER_GEMINI

GEMINI_KEY_ENV = "GEMINI_API_KEY"
MISTRAL_KEY_ENV = "MISTRAL_API_KEY"


class AIUnavailableError(Exception):
    """A IA não pode ser usada no resto desta execução (cota diária, chave ausente ou inválida)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class UnavailableExtractionService(IExtractionService):
    """Serviço sem IA: a leitura levanta `AIUnavailableError`; os outros usos caem na reserva."""

    def __init__(self, reason: str):
        self.reason = reason

    def extract_from_pdf(self, pdf_bytes: bytes, filename: str, check_theme: bool = False):
        raise AIUnavailableError(self.reason)

    def extract_from_text(self, text: str, filename: str, check_theme: bool = False):
        raise AIUnavailableError(self.reason)

    def classify_document_titles(self, titles: List[str]) -> Dict[str, str]:
        return heuristic_title_classification(titles)

    def categorize_finep_by_description(self, description: str) -> str:
        return ""


_instance: Optional[IExtractionService] = None
_instance_lock = threading.Lock()


def _unavailable(reason: str) -> UnavailableExtractionService:
    logger.error("IA indisponível nesta execução: %s. Itens que dependem dela serão adiados.", reason)
    return UnavailableExtractionService(reason)


def _build_service() -> IExtractionService:
    provider = (os.getenv(PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()
    if provider == PROVIDER_GEMINI:
        if not (os.getenv(GEMINI_KEY_ENV) or "").strip():
            return _unavailable(f"chave do Gemini ausente ({GEMINI_KEY_ENV})")
        from src.components.transforms.gemini_client import GeminiExtractionService

        service = GeminiExtractionService()
        logger.info("Provedor de IA: Gemini (modelo %s).", service.model)
        return service
    if provider == PROVIDER_MISTRAL:
        if not (os.getenv(MISTRAL_KEY_ENV) or "").strip():
            return _unavailable(f"chave da Mistral ausente ({MISTRAL_KEY_ENV})")
        from src.components.transforms.mistral_client import MistralExtractionService

        service = MistralExtractionService()
        logger.info("Provedor de IA: Mistral (modelo %s).", service.llm_model)
        return service
    return _unavailable(f"provedor de IA desconhecido: {provider}")


def get_extraction_service() -> IExtractionService:
    """Devolve o serviço de IA deste processo, criando-o na primeira chamada."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = _build_service()
        return _instance


def reset_extraction_service() -> None:
    """Esquece a instância (para testes)."""
    global _instance
    with _instance_lock:
        _instance = None
