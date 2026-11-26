"""
Filtro de tema sem IA para as indicações da comunidade (spec 009).

O formulário do portal é público: chega qualquer edital, inclusive concurso de
emprego (a primeira indicação real foi o Concurso Público Banestes 2026). Os
casos óbvios são recusados aqui, antes de gastar IA. "Processo seletivo" sozinho
não está na lista: aparece tanto em vaga de emprego quanto em seleção de
bolsistas, que é fomento.
"""

import re
import unicodedata
from typing import Optional

# termo normalizado (sem acento, minúsculas) -> rótulo legível para a recusa
OFF_TOPIC_TERMS = {
    "concurso publico": "concurso público",
    "licitacao": "licitação",
    "pregao eletronico": "pregão eletrônico",
    "pregao presencial": "pregão presencial",
    "leilao": "leilão",
    "processo seletivo simplificado para contratacao": "processo seletivo para contratação",
    "vaga de estagio": "vaga de estágio",
    "selecao de estagiarios": "seleção de estagiários",
    "cargo efetivo": "cargo efetivo",
}
TEXT_LIMIT = 2000


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", plain).strip().lower()


def off_topic_term(*texts: str, limit: int = TEXT_LIMIT) -> Optional[str]:
    """Rótulo do primeiro termo de outro assunto encontrado, ou None."""
    haystack = " ".join(_normalize((t or "")[:limit]) for t in texts)
    for term, label in OFF_TOPIC_TERMS.items():
        if term in haystack:
            return label
    return None
