"""
Índice dos editais já recomendados (spec 012): `registry/recomendacoes_enviadas.json`.

Só é gravado depois de TODAS as partes da mensagem terem sido enviadas: se uma
falhar, a semana seguinte manda tudo de novo (é melhor repetir do que perder).
"""

import json
import os
import tempfile
from datetime import date, timedelta
from typing import Dict, Iterable, List, Sequence, Tuple

from src.recommendation.matcher import closing_date

FILENAME = "recomendacoes_enviadas.json"


class SentStore:
    def __init__(self, path: str):
        self.path = path
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        self.sent: Dict[str, Dict[str, str]] = data if isinstance(data, dict) else {}

    def was_sent(self, link: str) -> bool:
        return (link or "").strip() in self.sent

    def mark_sent(self, editais: Iterable[Tuple[str, str]], today: date) -> None:
        for link, nome in editais:
            if link:
                self.sent[link] = {"enviado_em": today.isoformat(), "nome": nome}
        folder = os.path.dirname(self.path) or "."
        os.makedirs(folder, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=folder, delete=False, encoding="utf-8", suffix=".tmp") as tmp:
            json.dump(self.sent, tmp, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp.name, self.path)

    def reminder(self, editais: Sequence[dict], today: date, days: int) -> List[dict]:
        """Os já enviados, abertos, que fecham de hoje até `days` dias, pelo prazo."""
        limit = today + timedelta(days=days)
        due = []
        for edital in editais:
            closing = closing_date(edital)
            if (
                self.was_sent(edital.get("link"))
                and (edital.get("status") or "") == "aberto"
                and closing is not None
                and today <= closing <= limit
            ):
                due.append(edital)
        return sorted(due, key=lambda e: e.get("data_encerramento") or "")
