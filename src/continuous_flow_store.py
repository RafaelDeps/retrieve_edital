"""
Índice dos editais publicados como fluxo contínuo (spec 010, issue #9).

Regra do responsável: o prazo está no edital ou na página; sem prazo, é fluxo
contínuo. Mas um edital pode entrar no site depois da coleta (o CONIF 10 foi
coletado em 24/11, um dia antes do PDF). Por isso o fluxo contínuo deduzido da
falta de prazo ("sem-prazo") é relido de tempos em tempos; o declarado por
escrito ("declarada") não precisa.

Formato: {link: {"origem": "declarada" | "sem-prazo", "lido_em": "AAAA-MM-DD"}}.
Fica em `registry/`, ao lado dos índices de processados e recusados, e não muda o
JSON do portal.
"""

import json
import logging
import os
import tempfile
from datetime import date, timedelta
from typing import Dict, Iterable, Optional

logger = logging.getLogger(__name__)

FILENAME = "fluxo_continuo.json"
DEFAULT_PATH = os.path.join("registry", FILENAME)
ORIGIN_DECLARED = "declarada"
ORIGIN_NO_DEADLINE = "sem-prazo"
REREAD_DAYS_ENV = "RELEITURA_FLUXO_CONTINUO_DIAS"
DEFAULT_REREAD_DAYS = 7


def path_next_to(processed_index_path: str) -> str:
    """O índice fica na mesma pasta do índice de processados (testes usam tmp)."""
    return os.path.join(os.path.dirname(processed_index_path) or ".", FILENAME)


def load(path: str = DEFAULT_PATH) -> Dict[str, Dict[str, str]]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("Índice de fluxo contínuo ilegível (%s): %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def record(
    entries: Dict[str, str],
    cleared: Iterable[str] = (),
    today: Optional[date] = None,
    path: str = DEFAULT_PATH,
) -> int:
    """Grava `entries` ({link: origem}) com a data de hoje e apaga `cleared`."""
    cleared = [link for link in cleared if link]
    if not entries and not cleared:
        return 0
    data = load(path)
    lido_em = (today or date.today()).isoformat()
    for link, origem in entries.items():
        if link:
            data[link] = {"origem": origem, "lido_em": lido_em}
    for link in cleared:
        data.pop(link, None)
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=folder, delete=False, encoding="utf-8", suffix=".tmp") as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2, sort_keys=True)
        tmp_path = tmp.name
    os.replace(tmp_path, path)
    return len(entries)


def reread_days() -> int:
    raw = os.getenv(REREAD_DAYS_ENV)
    try:
        value = int(raw) if raw and raw.strip() else DEFAULT_REREAD_DAYS
    except ValueError:
        value = DEFAULT_REREAD_DAYS
    return value if value > 0 else DEFAULT_REREAD_DAYS


def due_for_reread(entry: Optional[Dict[str, str]], today: date, days: int) -> bool:
    """
    Sem entrada (publicado antes desta regra): sim. "sem-prazo" lido há `days`
    dias ou mais: sim. Declarado por escrito: não.
    """
    if not entry:
        return True
    if entry.get("origem") != ORIGIN_NO_DEADLINE:
        return False
    try:
        lido_em = date.fromisoformat(entry.get("lido_em") or "")
    except ValueError:
        return True
    return lido_em <= today - timedelta(days=days)
