"""
A mensagem semanal (spec 012, contrato `contracts/telegram_message.md`).

Um bloco por edital; as partes são cortadas só entre blocos, com até 3 900
caracteres (folga sob o limite de 4 096 do Telegram). Todo texto vindo de dados
passa por escape de HTML.
"""

import html
import logging
from datetime import date
from typing import List, Sequence

from src.recommendation.matcher import CONTINUOUS_FLOW, Recomendacao, closing_date

logger = logging.getLogger(__name__)

PART_LIMIT = 3900
EMPTY_WEEK = "Sem editais novos nesta semana."
DEGREE_LABEL = {
    "doutorado": "exige doutorado",
    "mestrado": "exige mestrado",
    "especializacao": "exige especialização",
    "graduacao": "exige graduação",
    "ensino_medio": "exige ensino médio",
}


def escape(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def deadline_label(edital: dict) -> str:
    if (edital.get("modalidade") or "") == CONTINUOUS_FLOW:
        return "fluxo contínuo"
    closing = closing_date(edital)
    return closing.strftime("%d/%m/%Y") if closing else "sem prazo informado"


def edital_block(rec: Recomendacao) -> str:
    edital = rec.edital
    minima = (edital.get("elegibilidade") or {}).get("titulacao_minima") or ""
    head = f"<b>{escape(edital.get('nome'))}</b>"
    info = f"Prazo: {deadline_label(edital)}"
    if minima in DEGREE_LABEL:
        info += f" · {DEGREE_LABEL[minima]}"
    lines = [head, info]
    for ind in rec.indicacoes:
        nome = escape(ind.professor.nome)
        if ind.aviso:
            nome += f" ({escape(ind.aviso)})"
        lines.append(f"• {nome} — {escape(ind.motivo)}" if ind.motivo else f"• {nome}")
    link = (edital.get("link") or "").strip()
    if link:
        lines.append(f'<a href="{html.escape(link, quote=True)}">Ver edital</a>')
    return "\n".join(lines)


def reminder_block(editais: Sequence[dict], days: int) -> str:
    lines = [f"⏰ <b>Fecham nos próximos {days} dias</b> (já recomendados)"]
    for edital in editais:
        lines.append(f"• {escape(edital.get('nome'))} — prazo {deadline_label(edital)}")
    return "\n".join(lines)


def header(today: date, campus: str, new_count: int) -> str:
    return (
        f"📬 <b>Editais recomendados — semana de {today.strftime('%d/%m/%Y')}</b>\n"
        f"Campus {escape(campus)} · {new_count} edita{'l novo' if new_count == 1 else 'is novos'}"
    )


def build_blocks(
    main: Sequence[Recomendacao],
    horizon: Sequence[Recomendacao],
    reminder: Sequence[dict],
    today: date,
    campus: str,
    reminder_days: int = 7,
) -> List[str]:
    """Os blocos em ordem: cabeçalho, principais, Horizon, lembrete."""
    blocks = [header(today, campus, len(main) + len(horizon))]
    if not main and not horizon:
        blocks.append(EMPTY_WEEK)
    blocks.extend(edital_block(rec) for rec in main)
    if horizon:
        blocks.append(
            f"🇪🇺 <b>Horizon Europe</b> (exige consórcio europeu) — "
            f"{len(horizon)} melhor{'' if len(horizon) == 1 else 'es'} combinaç{'ão' if len(horizon) == 1 else 'ões'}"
        )
        blocks.extend(edital_block(rec) for rec in horizon)
    if reminder:
        blocks.append(reminder_block(reminder, reminder_days))
    return blocks


def split_parts(blocks: Sequence[str], limit: int = PART_LIMIT) -> List[str]:
    """Junta os blocos em partes de até `limit`, sem cortar bloco; numera se > 1."""
    marker_room = len("\n(Parte 99/99)")
    budget = limit - marker_room
    parts: List[str] = []
    current = ""
    for block in blocks:
        if len(block) > budget:
            logger.warning("Bloco maior que o limite do Telegram; cortado nas linhas.")
            block = _shrink(block, budget)
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > budget and current:
            parts.append(current)
            current = block
        else:
            current = candidate
    if current:
        parts.append(current)
    if len(parts) > 1:
        parts = [f"{part}\n(Parte {i}/{len(parts)})" for i, part in enumerate(parts, 1)]
    return parts


def _shrink(block: str, budget: int) -> str:
    lines = block.split("\n")
    while len("\n".join(lines)) > budget and len(lines) > 3:
        lines.pop(-2)  # tira professores do fim, mantém título, prazo e link
    return "\n".join(lines)[:budget]
