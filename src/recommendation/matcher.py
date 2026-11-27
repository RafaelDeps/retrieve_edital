"""
Quem recomendar para cada edital (spec 012).

1. Filtros: edital aberto com prazo ≥ hoje + 3 dias (ou fluxo contínuo) e
   titulação do professor ≥ a exigida (desconhecida passa, com aviso).
2. Semelhança: cosseno entre o vetor do edital e o do perfil.
3. Centragem dupla: da semelhança bruta tiram-se a média do professor (contra
   todos os editais) e a do edital (contra todos os professores). Na prévia real
   de 27/11/2026, perfis curtos sem Lattes pareciam "um pouco com tudo" e
   apareciam em quase todos os editais; centrados, conta o quanto o professor
   combina com ESTE edital além do que combina com qualquer um.
4. Limite: semelhança bruta ≥ LIMITE_MINIMO e ajustada ≥ DESVIOS × desvio das
   ajustadas do edital (só quem se destaca).
5. Os 5 maiores pela ajustada; no empate, quem tem Lattes primeiro.

O motivo de cada indicação sai só dos dados do professor, sem IA (princípio II).
"""

import os
import statistics
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from src.recommendation.embeddings import cosine, text_key
from src.recommendation.horizon_profiles import Professor, degree_rank

MIN_THRESHOLD_ENV = "RECOMENDACAO_LIMITE_MINIMO"
DEVIATIONS_ENV = "RECOMENDACAO_DESVIOS"
MAX_PROFESSORS_ENV = "RECOMENDACAO_MAX_PROFESSORES"
MIN_DAYS_ENV = "RECOMENDACAO_PRAZO_MINIMO_DIAS"
DEFAULT_MIN_THRESHOLD = 0.6
DEFAULT_DEVIATIONS = 1.5
DEFAULT_MAX_PROFESSORS = 5
DEFAULT_MIN_DAYS = 3

CONTINUOUS_FLOW = "fluxo-contínuo"
HORIZON_SOURCE = "horizon"
EDITAL_DESCRIPTION_CHARS = 2000
REASON_MAX_CHARS = 120
MIN_WORD_LENGTH = 4
NO_DEGREE_WARNING = "sem Lattes no Horizon: confirmar titulação"


def _env_number(name: str, default: float, cast=float) -> float:
    raw = os.getenv(name)
    try:
        value = cast(raw) if raw and raw.strip() else default
    except ValueError:
        value = default
    return value if value > 0 else default


def _plain_words(text: str) -> set:
    decomposed = unicodedata.normalize("NFKD", text or "")
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    words = "".join(ch if ch.isalnum() else " " for ch in plain).split()
    return {w for w in words if len(w) >= MIN_WORD_LENGTH}


@dataclass
class Indicacao:
    professor: Professor
    score: float
    motivo: str
    aviso: str = ""


@dataclass
class Recomendacao:
    edital: dict
    indicacoes: List[Indicacao] = field(default_factory=list)
    limite: float = 0.0

    @property
    def best_score(self) -> float:
        return max((i.score for i in self.indicacoes), default=0.0)

    @property
    def is_horizon(self) -> bool:
        return (self.edital.get("fonte_key") or "") == HORIZON_SOURCE


def edital_text(edital: dict) -> str:
    eleg = edital.get("elegibilidade") or {}
    parts = [
        edital.get("nome") or "",
        (edital.get("descrição") or "")[:EDITAL_DESCRIPTION_CHARS],
        "Áreas: " + "; ".join(eleg.get("areas_conhecimento") or []) if eleg.get("areas_conhecimento") else "",
        eleg.get("requisitos_resumo") or "",
        "Temas: " + "; ".join(edital.get("tags") or []) if edital.get("tags") else "",
    ]
    return "\n".join(p for p in parts if p)


def closing_date(edital: dict) -> Optional[date]:
    try:
        return date.fromisoformat((edital.get("data_encerramento") or "").strip())
    except ValueError:
        return None


def eligible_edital(edital: dict, today: date, min_days: Optional[int] = None) -> bool:
    if (edital.get("status") or "") != "aberto":
        return False
    if (edital.get("modalidade") or "") == CONTINUOUS_FLOW:
        return True
    min_days = int(min_days if min_days is not None else _env_number(MIN_DAYS_ENV, DEFAULT_MIN_DAYS, int))
    closing = closing_date(edital)
    return closing is not None and closing >= today + timedelta(days=min_days)


def degree_ok(professor: Professor, titulacao_minima: str) -> Tuple[bool, str]:
    """(pode ser indicado, aviso)."""
    aviso = "" if professor.titulacao else NO_DEGREE_WARNING
    if not titulacao_minima:
        return True, aviso
    if professor.titulacao is None:
        return True, NO_DEGREE_WARNING
    return degree_rank(professor.titulacao) >= degree_rank(titulacao_minima), ""


def threshold(scores: Sequence[float]) -> float:
    """Desvios-padrão acima de zero nas semelhanças já centradas do edital."""
    deviations = _env_number(DEVIATIONS_ENV, DEFAULT_DEVIATIONS)
    if len(scores) < 2:
        return 0.0
    return deviations * statistics.pstdev(scores)


def _tag_matches(tag: str, edital_words: set) -> bool:
    """A tag conta se pelo menos metade das palavras dela está no edital."""
    words = _plain_words(tag)
    return bool(words) and len(words & edital_words) * 2 >= len(words)


def evidence(professor: Professor, edital: dict) -> Tuple[List[str], str]:
    """(tags em comum, item do perfil com mais palavras em comum): o porquê da indicação."""
    edital_words = _plain_words(edital_text(edital))
    temas = [t for t in getattr(professor, "temas", []) if _tag_matches(t, edital_words)]
    best, best_overlap = "", 0
    for item in professor.itens:
        overlap = len(_plain_words(item) & edital_words)
        if overlap > best_overlap:
            best, best_overlap = item, overlap
    return temas, best


def reason(professor: Professor, edital: dict) -> str:
    """Formação + as tags em comum (ou o item do perfil mais próximo do edital)."""
    parts = []
    if professor.titulacao in ("doutorado", "mestrado") and professor.curso_titulacao:
        nivel = "Doutorado" if professor.titulacao == "doutorado" else "Mestrado"
        parts.append(f"{nivel} em {professor.curso_titulacao}")
    temas, best = evidence(professor, edital)
    if temas:
        parts.append("temas: " + ", ".join(temas[:3]))
    elif best:
        parts.append(f'"{best}"')
    text = "; ".join(parts)
    return text if len(text) <= REASON_MAX_CHARS else text[: REASON_MAX_CHARS - 1].rstrip() + "…"


def recommend(
    editais: Sequence[dict],
    professors: Sequence[Professor],
    vectors: Dict[str, List[float]],
    today: date,
    reference: Optional[Sequence[dict]] = None,
) -> List[Recomendacao]:
    """
    Uma Recomendacao por edital de `editais` com alguém acima do limite.

    As médias da centragem vêm de `reference` (todos os abertos, já recomendados
    ou não): numa semana com um só edital novo, centrar só nele zeraria tudo.
    """
    max_professors = int(_env_number(MAX_PROFESSORS_ENV, DEFAULT_MAX_PROFESSORS, int))
    floor = _env_number(MIN_THRESHOLD_ENV, DEFAULT_MIN_THRESHOLD)
    profs = [(p, vectors.get(text_key(p.texto))) for p in professors]
    profs = [(p, v) for p, v in profs if v is not None]
    candidates_editais = []
    for edital in editais:
        if not eligible_edital(edital, today):
            continue
        vector = vectors.get(text_key(edital_text(edital)))
        if vector is not None:
            candidates_editais.append((edital, vector))
    if not profs or not candidates_editais:
        return []

    ref_vectors = [ev for _, ev in candidates_editais]
    seen = {text_key(edital_text(e)) for e, _ in candidates_editais}
    for edital in reference or []:
        key = text_key(edital_text(edital))
        if key not in seen and key in vectors and eligible_edital(edital, today):
            ref_vectors.append(vectors[key])
            seen.add(key)
    ref_raw = [[cosine(ev, pv) for _, pv in profs] for ev in ref_vectors]
    prof_mean = [statistics.mean(row[j] for row in ref_raw) for j in range(len(profs))]
    grand = statistics.mean(statistics.mean(row) for row in ref_raw)

    raw = ref_raw[: len(candidates_editais)]
    edital_mean = [statistics.mean(row) for row in raw]

    result: List[Recomendacao] = []
    for i, (edital, _) in enumerate(candidates_editais):
        adjusted = [raw[i][j] - prof_mean[j] - edital_mean[i] + grand for j in range(len(profs))]
        limit = threshold(adjusted)
        minima = ((edital.get("elegibilidade") or {}).get("titulacao_minima") or "")
        chosen = []
        for j, (professor, _) in enumerate(profs):
            ok, aviso = degree_ok(professor, minima)
            # Sem evidência concreta (tag ou item em comum) não indica: na prévia
            # real, chamadas do Horizon em inglês traziam nomes sem motivo algum.
            if ok and raw[i][j] >= floor and adjusted[j] > 0 and adjusted[j] >= limit and any(
                evidence(professor, edital)
            ):
                chosen.append(Indicacao(professor, adjusted[j], reason(professor, edital), aviso))
        chosen.sort(key=lambda ind: (-round(ind.score, 4), not ind.professor.tem_lattes, ind.professor.nome))
        if chosen:
            result.append(Recomendacao(edital, chosen[:max_professors], limit))
    return result
