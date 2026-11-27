"""
Recomendação semanal de editais a professores no Telegram (spec 012).

Roteiro: professores do Horizon → editais abertos ainda não recomendados →
vetores (com cache) → quem recomendar → mensagem em partes → envio ao grupo →
índice de enviados. Sem credenciais do Telegram, só mostra a mensagem no log.

Uso: `python -m src.flows.recommend_flow`. Código de saída 1 quando nada pôde ser
enviado por falha (Horizon sem reserva, Telegram recusando); 0 no resto.
"""

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Callable, List, Optional

from dotenv import load_dotenv

from src.recommendation import horizon_profiles
from src.recommendation.embeddings import CACHE_FILENAME, EmbeddingCache, GeminiEmbeddingService, IEmbeddingService
from src.recommendation.matcher import _env_number, edital_text, recommend
from src.recommendation.message import build_blocks, split_parts
from src.recommendation.sent_store import FILENAME as SENT_FILENAME
from src.recommendation.sent_store import SentStore
from src.recommendation.telegram_client import TelegramClient, TelegramError

logger = logging.getLogger(__name__)

MAX_HORIZON_ENV = "RECOMENDACAO_MAX_HORIZON"
REMINDER_DAYS_ENV = "RECOMENDACAO_LEMBRETE_DIAS"
DEFAULT_MAX_HORIZON = 5
DEFAULT_REMINDER_DAYS = 7
SUMMARY_ENV = "GITHUB_STEP_SUMMARY"


def load_open_editais(output_dir: str) -> List[dict]:
    editais = []
    for path in sorted(Path(output_dir).glob("*.json")):
        try:
            edital = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(edital, dict) and (edital.get("status") or "") == "aberto" and edital.get("link"):
            editais.append(edital)
    return editais


def _write_summary(lines: List[str]) -> None:
    path = os.getenv(SUMMARY_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("### Recomendação semanal\n\n" + "\n".join(f"- {line}" for line in lines) + "\n")


def run(
    today: Optional[date] = None,
    profiles_loader: Optional[Callable] = None,
    embedder: Optional[IEmbeddingService] = None,
    telegram: Optional[TelegramClient] = None,
    output_dir: str = "data/output",
    registry_dir: str = "registry",
) -> int:
    today = today or date.today()
    campus = horizon_profiles.campus_name()
    loader = profiles_loader or (lambda: horizon_profiles.load_professors(registry_dir, today, campus=campus))
    professors, origin = loader()
    if not professors:
        logger.error("Sem professores (Horizon indisponível e sem cópia anterior): nada enviado.")
        _write_summary(["❌ Horizon ETL indisponível e sem cópia anterior: nada enviado."])
        return 1
    with_lattes = sum(1 for p in professors if p.tem_lattes)
    logger.info("Professores carregados: %s (%s com Lattes), origem %s.", len(professors), with_lattes, origin)

    editais = load_open_editais(output_dir)
    sent = SentStore(os.path.join(registry_dir, SENT_FILENAME))
    new_editais = [e for e in editais if not sent.was_sent(e.get("link"))]

    cache = EmbeddingCache(os.path.join(registry_dir, CACHE_FILENAME))
    # Todos os abertos: os já recomendados entram só nas médias da centragem
    # (seus vetores já estão no cache), não na mensagem.
    texts = [p.texto for p in professors] + [edital_text(e) for e in editais]
    vectors, pending = cache.vectors_for(texts, embedder or GeminiEmbeddingService(), today)
    cache.prune(today)

    recs = recommend(new_editais, professors, vectors, today, reference=editais)
    main = sorted((r for r in recs if not r.is_horizon), key=lambda r: r.edital.get("data_encerramento") or "9999")
    max_horizon = int(_env_number(MAX_HORIZON_ENV, DEFAULT_MAX_HORIZON, int))
    horizon = sorted((r for r in recs if r.is_horizon), key=lambda r: -r.best_score)[:max_horizon]
    reminder_days = int(_env_number(REMINDER_DAYS_ENV, DEFAULT_REMINDER_DAYS, int))
    reminder = sent.reminder(editais, today, reminder_days)
    parts = split_parts(build_blocks(main, horizon, reminder, today, campus, reminder_days))

    new_texts = {edital_text(e) for e in new_editais}
    pending_editais = len([t for t in pending if t in new_texts])
    summary = [
        f"Professores: {len(professors)} ({with_lattes} com Lattes), origem {origin}",
        f"Editais recomendados: {len(main)} (+{len(horizon)} do Horizon)",
        f"Vetores novos: {cache.new_count}",
        f"Pendentes por cota da IA: {len(pending)} textos ({pending_editais} editais)",
        f"Partes: {len(parts)}",
    ]
    telegram = telegram or TelegramClient()
    if not telegram.can_send:
        for i, part in enumerate(parts, 1):
            logger.info("Prévia, parte %s/%s:\n%s", i, len(parts), part)
        logger.info("Modo prévia: nada enviado (sem TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID).")
        cache.save()
        _write_summary(summary + ["Modo prévia: nada enviado."])
        return 0
    try:
        for part in parts:
            telegram.send(part)
    except TelegramError as exc:
        logger.error("Telegram recusou o envio (%s): nada marcado como enviado.", exc)
        cache.save()
        _write_summary(summary + [f"❌ Falha no envio ao Telegram: {exc}"])
        return 1
    sent.mark_sent([(r.edital.get("link"), r.edital.get("nome") or "") for r in main + horizon], today)
    cache.save()
    logger.info("Enviadas %s parte(s) ao grupo.", len(parts))
    _write_summary(summary + [f"✅ Enviadas {len(parts)} parte(s) ao grupo."])
    return 0


if __name__ == "__main__":
    load_dotenv(".env")
    logging.basicConfig(level=logging.INFO)
    sys.exit(run())
