"""
Vetores de significado (embeddings) com cache (spec 012).

Cada perfil de professor e cada edital vira um vetor; textos de tema parecido
ficam próximos mesmo em línguas diferentes (testado em 27/11/2026: "saúde pública"
fica a 0,85 de "medical imaging"). Só textos novos ou alterados são calculados: o
cache fica em `registry/embeddings_cache.json`, com vetores em meia precisão
(float16) e base64, para ficar pequeno no git.
"""

import base64
import hashlib
import json
import logging
import math
import os
import struct
import tempfile
from abc import ABC, abstractmethod
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from src.components.transforms.extraction_service import AIUnavailableError

logger = logging.getLogger(__name__)

MODEL = "gemini-embedding-001"
DIM = 768
BATCH = 100
# Nível gratuito: limite de TOKENS por minuto nos embeddings (validação real de
# 27/11/2026, quota EmbedContentTokensPerMinutePerProjectPerModel-FreeTier). Os
# lotes são limitados também pelo tamanho do texto, e o limite por minuto espera
# o tempo que a API pede antes de tentar de novo.
BATCH_MAX_CHARS = 60_000
PER_MINUTE_ATTEMPTS = 3
MAX_WAIT_SEC = 90
CACHE_FILENAME = "embeddings_cache.json"
CACHE_TTL_DAYS = 60
TASK_TYPE = "SEMANTIC_SIMILARITY"


def text_key(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def encode(vector: Sequence[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}e", *vector)).decode("ascii")


def decode(value: str) -> List[float]:
    raw = base64.b64decode(value)
    return list(struct.unpack(f"<{len(raw) // 2}e", raw))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norm if norm else 0.0


class IEmbeddingService(ABC):
    @abstractmethod
    def embed(self, texts: List[str]) -> List[List[float]]:
        """Um vetor por texto. `AIUnavailableError` sem cota ou sem chave."""


class GeminiEmbeddingService(IEmbeddingService):
    """Embeddings do Gemini no nível gratuito (constituição, princípio V)."""

    def __init__(self, api_key: Optional[str] = None, client=None, sleep=None):
        import time

        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self._client = client
        self._sleep = sleep or time.sleep

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise AIUnavailableError("GEMINI_API_KEY ausente")
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def embed(self, texts: List[str]) -> List[List[float]]:
        from google.genai import types

        from src.components.transforms.gemini_client import (
            ERROR_AUTH,
            ERROR_DAILY,
            ERROR_PER_MINUTE,
            classify_api_error,
        )

        from src.components.transforms.gemini_client import retry_delay_seconds

        for attempt in range(1, PER_MINUTE_ATTEMPTS + 1):
            try:
                response = self._get_client().models.embed_content(
                    model=MODEL,
                    contents=list(texts),
                    config=types.EmbedContentConfig(output_dimensionality=DIM, task_type=TASK_TYPE),
                )
                return [list(item.values) for item in response.embeddings]
            except AIUnavailableError:
                raise
            except Exception as exc:
                kind = classify_api_error(exc)
                if kind == ERROR_PER_MINUTE and attempt < PER_MINUTE_ATTEMPTS:
                    delay = min(retry_delay_seconds(exc, 60.0) + 1, MAX_WAIT_SEC)
                    logger.info("Embeddings: limite por minuto; esperando %.0f s.", delay)
                    self._sleep(delay)
                    continue
                if kind in (ERROR_DAILY, ERROR_PER_MINUTE, ERROR_AUTH):
                    raise AIUnavailableError(f"embeddings indisponíveis ({kind})") from None
                raise
        raise AIUnavailableError("embeddings indisponíveis (per_minute)")


def _batches(texts: Sequence[str]) -> List[List[str]]:
    """Lotes de até BATCH textos e BATCH_MAX_CHARS caracteres."""
    batches: List[List[str]] = []
    current: List[str] = []
    size = 0
    for text in texts:
        if current and (len(current) >= BATCH or size + len(text) > BATCH_MAX_CHARS):
            batches.append(current)
            current, size = [], 0
        current.append(text)
        size += len(text)
    if current:
        batches.append(current)
    return batches


class EmbeddingCache:
    """{sha256 do texto: vetor}, com a data do último uso para a limpeza."""

    def __init__(self, path: str):
        self.path = path
        self.vectors: Dict[str, Dict[str, str]] = {}
        self.new_count = 0
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if data.get("modelo") != MODEL or data.get("dim") != DIM:
            logger.warning("Cache de vetores de outro modelo ou dimensão: descartado.")
            return
        self.vectors = data.get("vetores") or {}

    def save(self) -> None:
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        payload = {"modelo": MODEL, "dim": DIM, "vetores": self.vectors}
        with tempfile.NamedTemporaryFile("w", dir=folder, delete=False, encoding="utf-8", suffix=".tmp") as tmp:
            json.dump(payload, tmp, ensure_ascii=False, sort_keys=True)
        os.replace(tmp.name, self.path)

    def vectors_for(
        self, texts: Sequence[str], service: IEmbeddingService, today: date
    ) -> Tuple[Dict[str, List[float]], List[str]]:
        """
        ({sha: vetor}, textos pendentes). Calcula só o que falta, em lotes; no
        primeiro `AIUnavailableError`, para e devolve o resto como pendente.
        """
        unique = list(dict.fromkeys(t for t in texts if t))
        missing = [t for t in unique if text_key(t) not in self.vectors]
        pending: List[str] = []
        done = 0
        for batch in _batches(missing):
            try:
                computed = service.embed(batch)
            except AIUnavailableError as exc:
                logger.warning("Vetores pendentes (%s): %s textos.", exc.reason, len(missing) - done)
                pending = missing[done:]
                break
            done += len(batch)
            for text, vector in zip(batch, computed):
                self.vectors[text_key(text)] = {"v": encode(vector), "usado_em": today.isoformat()}
                self.new_count += 1
        result: Dict[str, List[float]] = {}
        for text in unique:
            entry = self.vectors.get(text_key(text))
            if entry:
                entry["usado_em"] = today.isoformat()
                result[text_key(text)] = decode(entry["v"])
        return result, pending

    def prune(self, today: date, ttl_days: int = CACHE_TTL_DAYS) -> int:
        limit = (today - timedelta(days=ttl_days)).isoformat()
        old = [key for key, entry in self.vectors.items() if (entry.get("usado_em") or "") < limit]
        for key in old:
            del self.vectors[key]
        return len(old)
