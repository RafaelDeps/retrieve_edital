"""
Regras de cronograma: prazo, abertura, chamadas e próxima etapa.

Funções puras, sem I/O, usadas tanto pelo normalizador (ao processar um edital)
quanto pelo realinhamento diário (scripts/curate_output.py), para que as duas
pontas nunca calculem o prazo de jeitos diferentes. Toda regra que depende de
"hoje" recebe a data explicitamente (Constituição, Princípio VI).

Contrato dos campos gerados: docs/output_contract.md.
"""

import re
import unicodedata
from datetime import date
from typing import Dict, List, Optional, Tuple


def _extract_last_schedule_date(text: str) -> str:
    if not text:
        return ""

    iso_matches = re.findall(r"\d{4}-\d{2}-\d{2}", str(text))
    if iso_matches:
        return iso_matches[-1]

    br_matches = re.findall(r"(\d{2})/(\d{2})/(\d{4})", str(text))
    if br_matches:
        day, month, year = br_matches[-1]
        return f"{year}-{month}-{day}"

    return ""


def _match_any_token(event_name: str, tokens: tuple[str, ...]) -> bool:
    lowered = (event_name or "").lower()
    return any(token in lowered for token in tokens)


# Rótulos de etapa que indicam início e fim do período de submissão. A ordem de
# precedência importa: `abertura das inscrições` descreve o marco do edital,
# enquanto `publicação` costuma ser a data em que o documento foi divulgado. Para
# uma chamada de fluxo contínuo, o início útil é a abertura das inscrições.
START_EVENT_TOKENS = (
    "início do período do edital",
    "inicio do período do edital",
    "abertura das inscrições",
    "abertura das inscricoes",
    "abertura da inscrição",
    "abertura da inscricao",
    "inscrição",
    "inscricao",
    "manifestação de interesse",
    "manifestacao de interesse",
    "início",
    "inicio",
    "período",
    "periodo",
)
PUBLICATION_EVENT_TOKENS = ("publicação", "publicacao")
END_EVENT_TOKENS = (
    "fim do período do edital",
    "fim do periodo do edital",
    "prazo para envio da proposta",
    "prazo para envio de propostas",
    "prazo para envio",
    "encerramento",
    "término",
    "termino",
    "fim das inscrições",
    "fim das inscricoes",
    "fim do prazo",
    "data limite",
    "último dia",
    "ultimo dia",
)
# "Submissão até" / "Inscrições até o dia": "até" só conta quando fecha o rótulo.
# No meio do texto ("Fluxo contínuo ou até que se atinja o valor") não é prazo,
# e como palavra solta casaria em textos descritivos longos (PRODIF 09/2026).
END_EVENT_WORD_PATTERN = re.compile(r"(?<!\w)at[eé](\s+o\s+dia)?\s*:?\s*$", re.IGNORECASE)


def _is_iso_date(value) -> bool:
    return bool(re.match(r"\d{4}-\d{2}-\d{2}", str(value or "")))

ISO_DATE_PATTERN = re.compile(r"(\d{4})-(\d{2})-(\d{2})|(\d{2})/(\d{2})/(\d{4})")
# "24/02/2026 a 02/03/2026", "2026-10-14 até 2026-12-15": um período com fim explícito.
INTERVAL_SEPARATOR_PATTERN = re.compile(r"\s(?:a|até|ate)\s", re.IGNORECASE)


def _all_dates(text) -> List[str]:
    """Datas do texto, na ordem em que aparecem, em ISO (aceita ISO e dd/mm/aaaa)."""
    dates = []
    for match in ISO_DATE_PATTERN.finditer(str(text or "")):
        if match.group(1):
            dates.append(f"{match.group(1)}-{match.group(2)}-{match.group(3)}")
        else:
            dates.append(f"{match.group(6)}-{match.group(5)}-{match.group(4)}")
    return dates


def _is_interval(text) -> bool:
    return len(_all_dates(text)) >= 2 and bool(INTERVAL_SEPARATOR_PATTERN.search(str(text)))


def is_end_event(event: Dict[str, str]) -> bool:
    name = event.get("evento") or ""
    return _match_any_token(name, END_EVENT_TOKENS) or bool(END_EVENT_WORD_PATTERN.search(name))


def is_start_event(event: Dict[str, str]) -> bool:
    """Etapa de início que não é, ao mesmo tempo, de encerramento."""
    return _match_any_token(event.get("evento") or "", START_EVENT_TOKENS) and not is_end_event(event)


def normalize_call_name(name: str) -> str:
    """Chave de agrupamento: "2ª Chamada" e "2a chamada" são a mesma chamada."""
    text = (name or "").replace("ª", "a").replace("º", "o").lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip()


# Etapas que dizem respeito à inscrição do proponente. "Encerramento da avaliação
# de mérito" também tem "encerramento", mas não é prazo de quem se candidata.
SUBMISSION_EVENT_TOKENS = (
    "submissão",
    "submissao",
    "inscrição",
    "inscricao",
    "inscrições",
    "inscricoes",
    "proposta",
    "envio",
    "candidatura",
    "manifestação de interesse",
    "manifestacao de interesse",
)


def is_submission_event(event: Dict[str, str]) -> bool:
    return _match_any_token(event.get("evento") or "", SUBMISSION_EVENT_TOKENS)


def _prefer_submission(
    candidates: List[Tuple[str, Dict[str, str]]]
) -> List[Tuple[str, Dict[str, str]]]:
    """Se houver etapas de submissão entre os candidatos, só elas contam."""
    submission = [c for c in candidates if is_submission_event(c[1])]
    return submission or candidates


def end_event_candidates(events: List[Dict[str, str]]) -> List[Tuple[str, Dict[str, str]]]:
    """
    Prazos declarados: etapas com rótulo de encerramento e data (de intervalo, a última).

    Quando o cronograma tem encerramentos de submissão/inscrição, só eles são
    prazo: "Encerramento da avaliação de mérito" não é prazo do proponente.
    """
    candidates = []
    for event in events or []:
        dates = _all_dates(event.get("data"))
        if dates and is_end_event(event):
            candidates.append((dates[-1], event))
    return _prefer_submission(candidates)


def closing_candidates(events: List[Dict[str, str]]) -> List[Tuple[str, Dict[str, str]]]:
    """
    Datas que podem ser o prazo do edital, com a etapa de onde vieram.

    Primeiro as etapas de encerramento. Só na falta delas, etapas de início cujo
    valor é um intervalo ("24/02/2026 a 02/03/2026" → 02/03): o fim do intervalo
    é um prazo de verdade. Data solta de início nunca vira prazo — seria inventar
    um encerramento que o edital não declara (Constituição, Princípio II).
    """
    candidates = end_event_candidates(events)
    if candidates:
        return candidates
    return [
        (_all_dates(event.get("data"))[-1], event)
        for event in events or []
        if is_start_event(event) and _is_interval(event.get("data"))
    ]


def _choose_closing(
    events: List[Dict[str, str]], today: date
) -> Tuple[str, Optional[Dict[str, str]]]:
    """O próximo prazo a partir de hoje; se todos já passaram, o último."""
    candidates = closing_candidates(events)
    if not candidates:
        return "", None
    reference = today.isoformat()
    upcoming = [c for c in candidates if c[0] >= reference]
    chosen = min(upcoming, key=lambda c: c[0]) if upcoming else max(candidates, key=lambda c: c[0])
    return chosen


def derive_closing(events: List[Dict[str, str]], today: date) -> str:
    return _choose_closing(events, today)[0]


def _start_dates(events: List[Dict[str, str]]) -> List[Tuple[str, Dict[str, str]]]:
    """Inícios de etapa; com inícios de submissão no cronograma, só eles (ver end_event_candidates)."""
    starts = []
    for event in events or []:
        dates = _all_dates(event.get("data"))
        if dates and is_start_event(event):
            starts.append((dates[0], event))
    return _prefer_submission(starts)


def _first_date_matching(events: List[Dict[str, str]], tokens: tuple) -> str:
    for item in events or []:
        date_value = item.get("data") or ""
        if _is_iso_date(date_value) and _match_any_token(item.get("evento") or "", tokens):
            return date_value
    return ""


def _opening_by_precedence(events: List[Dict[str, str]]) -> str:
    """
    Abertura sem prazo de referência: abertura de inscrições, depois publicação,
    depois a primeira data disponível. Nunca inventa data.
    """
    for tokens in (START_EVENT_TOKENS, PUBLICATION_EVENT_TOKENS):
        found = _first_date_matching(events, tokens)
        if found:
            return found
    for item in events or []:
        if _is_iso_date(item.get("data")):
            return item["data"]
    return ""


def derive_opening(
    events: List[Dict[str, str]], closing: str, closing_event: Optional[Dict[str, str]]
) -> str:
    """
    Abertura da mesma chamada do prazo escolhido.

    Com o campo `chamada`, a menor data de início daquela chamada. Sem ele, a
    primeira data de início dentro da janela da chamada: depois do prazo da
    chamada anterior e até o prazo escolhido. "A última data de início antes do
    prazo" pegava etapas como "Período de julgamento". Sem prazo, a precedência
    de sempre.
    """
    if not closing:
        return _opening_by_precedence(events)
    starts = _start_dates(events)
    call = normalize_call_name((closing_event or {}).get("chamada") or "")
    if call:
        same_call = [d for d, e in starts if normalize_call_name(e.get("chamada") or "") == call]
        if same_call:
            return min(same_call)
    earlier_closings = [d for d, _ in closing_candidates(events) if d < closing]
    window_start = max(earlier_closings) if earlier_closings else ""
    in_window = [d for d, _ in starts if window_start < d <= closing]
    if in_window:
        return min(in_window)
    return _opening_by_precedence(events)


def derive_chamadas(events: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Uma entrada por chamada, na ordem em que aparecem no cronograma.

    Agrupa as etapas pelo nome normalizado da chamada; o nome exibido é a
    primeira grafia encontrada. Sem nenhuma etapa com chamada, lista vazia.
    """
    groups: Dict[str, Dict[str, object]] = {}
    for event in events or []:
        name = (event.get("chamada") or "").strip()
        if not name:
            continue
        key = normalize_call_name(name)
        groups.setdefault(key, {"nome": name, "events": []})["events"].append(event)
    chamadas = []
    for group in groups.values():
        group_events = group["events"]
        starts = [d for d, _ in _start_dates(group_events)]
        closings = [d for d, _ in closing_candidates(group_events)]
        chamadas.append(
            {
                "nome": group["nome"],
                "abertura": min(starts) if starts else "",
                "encerramento": max(closings) if closings else "",
            }
        )
    return chamadas


def derive_next_stage(events: List[Dict[str, str]], today: date) -> Optional[Dict[str, str]]:
    """A etapa de qualquer tipo com a menor data a partir de hoje; None se não houver."""
    reference = today.isoformat()
    upcoming = []
    for position, event in enumerate(events or []):
        for event_date in _all_dates(event.get("data")):
            if event_date >= reference:
                upcoming.append((event_date, position, event))
    if not upcoming:
        return None
    event_date, _, event = min(upcoming, key=lambda item: (item[0], item[1]))
    return {"evento": event.get("evento") or "", "data": event_date}


def derive_schedule_fields(events: List[Dict[str, str]], today: date) -> Dict[str, object]:
    """Campos derivados do cronograma, todos calculados para a mesma data de referência."""
    closing, closing_event = _choose_closing(events, today)
    return {
        "data_encerramento": closing,
        "data_abertura": derive_opening(events, closing, closing_event),
        "chamadas": derive_chamadas(events),
        "proxima_etapa": derive_next_stage(events, today),
    }
