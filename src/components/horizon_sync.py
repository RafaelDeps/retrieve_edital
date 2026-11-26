"""
Regras da atualização semanal do Horizon Europe (spec 005, issue #8).

Funções puras: o fluxo lê os fatos (o que estava salvo, o que a lista trouxe) e
o Sink grava. A leitura do Horizon não usa IA, então toda chamada relevante é
relida e reescrita a cada execução; estas regras dizem o que mudou e o que deve
ser encerrado por ter saído da lista.
"""

from dataclasses import asdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

from src.domain.models import EditalDomain

CHANGE_NEW = "nova"
CHANGE_UPDATED = "alterada"
CHANGE_UNCHANGED = "sem_mudanca"

OPEN_STATUS = "aberto"
# Abaixo desta fração das chamadas abertas salvas, a lista é suspeita (download
# parcial, mudança de formato) e ninguém é encerrado por ausência.
CLOSING_GUARD_RATIO = 0.5
REASON_INCOMPLETE = "dataset incompleto"

SavedByLink = Dict[str, Tuple[str, dict]]


def classify_changes(
    domains: Iterable[EditalDomain], saved_by_link: SavedByLink, processed_links: Set[str]
) -> Dict[str, str]:
    """Para cada chamada gravada: nova (fora do índice), sem mudança ou alterada."""
    changes: Dict[str, str] = {}
    for domain in domains:
        link = (domain.link or "").strip()
        if link not in processed_links:
            changes[link] = CHANGE_NEW
        elif link in saved_by_link and saved_by_link[link][1] == asdict(domain):
            changes[link] = CHANGE_UNCHANGED
        else:
            changes[link] = CHANGE_UPDATED
    return changes


def count_changes(changes: Dict[str, str]) -> Dict[str, int]:
    values = list(changes.values())
    return {kind: values.count(kind) for kind in (CHANGE_NEW, CHANGE_UPDATED, CHANGE_UNCHANGED)}


def open_saved_links(saved_by_link: SavedByLink) -> List[str]:
    return [link for link, (_, data) in saved_by_link.items() if data.get("status") == OPEN_STATUS]


def closing_guard(read_complete: bool, relevant_count: int, open_saved_count: int) -> Optional[str]:
    """Motivo para não encerrar ninguém por ausência, ou None quando é seguro."""
    if not read_complete:
        return REASON_INCOMPLETE
    if relevant_count < CLOSING_GUARD_RATIO * open_saved_count:
        return (
            f"{relevant_count} relevantes, menos da metade das "
            f"{open_saved_count} abertas salvas"
        )
    return None


def links_to_close(saved_by_link: SavedByLink, relevant_links: Set[str]) -> List[str]:
    """Chamadas salvas como abertas que não estão mais entre as relevantes."""
    return [link for link in open_saved_links(saved_by_link) if link not in relevant_links]
