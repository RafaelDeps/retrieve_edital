"""
Sinais de saúde compartilhados entre os fluxos ETL e o runner unificado.

Motivação: o runner tratava como `Sucesso` duas situações muito diferentes:

- o source devolveu **zero itens brutos** — o scraper quebrou;
- o source devolveu **zero itens novos** — o portal simplesmente não publicou nada.

Como as duas apareciam no log como "Sucesso, delta 0", as quedas da FINEP e do
CNPq passaram meses despercebidas. Ver `docs/spec_finep_cnpq_horizon.md`.

Os fluxos informam a contagem bruta pelo stdout, num formato que o runner
consegue ler sem acoplar-se ao código de cada fluxo.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

FLOW_STATS_PREFIX = "[flow-stats]"

_FLOW_STATS_REGEX = re.compile(
    rf"{re.escape(FLOW_STATS_PREFIX)}\s+raw=(\d+)(?:\s+new=(\d+))?(?:\s+deferred=(\d+))?"
    r"(?:\s+created=(\d+)\s+updated=(\d+)\s+unchanged=(\d+)\s+closed=(\d+))?"
    r"(?:\s+closing_blocked=(1))?"
    r"(?:\s+network=(\w+))?(?:\s+jina_pages=(\d+))?(?:\s+network_reason=(\S+))?"
)


@dataclass(frozen=True)
class FlowStats:
    """Contagens que um fluxo reporta ao runner."""

    raw_count: int
    new_count: Optional[int] = None
    # Itens não lidos porque a IA ficou indisponível (cota diária, chave);
    # voltam na próxima execução. Maior que zero vira Atenção no runner.
    deferred_count: int = 0
    # Fontes que relêem tudo a cada execução (Horizon): o que mudou nos
    # arquivos e se o encerramento por ausência foi suspenso pela trava.
    created: Optional[int] = None
    updated: Optional[int] = None
    unchanged: Optional[int] = None
    closed: Optional[int] = None
    closing_blocked: bool = False
    # Acesso à origem quando ela recusa o GitHub (www.gov.br, spec 006): None é o
    # acesso direto; "jina" é o desvio (com o número de páginas); "failed" é a
    # falha de rede, com o motivo.
    network: Optional[str] = None
    jina_pages: int = 0
    network_reason: Optional[str] = None

    @property
    def source_returned_nothing(self) -> bool:
        """True quando o source não trouxe nenhum item bruto da origem."""
        return self.raw_count == 0


def emit_flow_stats(
    raw_count: int,
    new_count: Optional[int] = None,
    deferred_count: int = 0,
    created: Optional[int] = None,
    updated: int = 0,
    unchanged: int = 0,
    closed: int = 0,
    closing_blocked: bool = False,
    network: Optional[str] = None,
    jina_pages: int = 0,
    network_reason: Optional[str] = None,
) -> None:
    """
    Publica as contagens do fluxo no stdout para o runner unificado ler.

    `raw_count` é o total lido da origem (antes de deduplicação); `new_count`,
    quando informado, é quanto sobrou depois de descartar o que já estava no
    registry.
    """
    line = f"{FLOW_STATS_PREFIX} raw={raw_count}"
    if new_count is not None:
        line += f" new={new_count}"
    if deferred_count > 0:
        line += f" deferred={deferred_count}"
    if created is not None:
        line += f" created={created} updated={updated} unchanged={unchanged} closed={closed}"
    if closing_blocked:
        line += " closing_blocked=1"
    if network and network != "direct":
        line += f" network={network}"
        if network == "jina":
            line += f" jina_pages={jina_pages}"
        if network == "failed" and network_reason:
            line += f" network_reason={network_reason}"
    print(line, flush=True)


def network_stats(source) -> dict:
    """
    Estado de rede de uma fonte com `fetcher` (www.gov.br, spec 006), pronto para
    `emit_flow_stats(**...)`. Acesso direto não gera campo nenhum.
    """
    fetcher = getattr(source, "fetcher", None)
    mode = getattr(fetcher, "mode", "direct")
    if fetcher is None or mode == "direct":
        return {}
    stats = {"network": mode, "jina_pages": getattr(fetcher, "jina_pages", 0)}
    if mode == "failed":
        stats["network_reason"] = getattr(fetcher, "failure_reason", None)
    return stats


def parse_flow_stats(output: str) -> Optional[FlowStats]:
    """
    Extrai as contagens da saída de um fluxo.

    Retorna `None` quando o fluxo não publicou estatísticas — fluxos ainda não
    instrumentados continuam funcionando, apenas sem esse sinal.
    """
    if not output:
        return None
    match = None
    for match in _FLOW_STATS_REGEX.finditer(output):
        pass  # a última ocorrência é a que vale
    if match is None:
        return None
    raw_count = int(match.group(1))
    new_count = int(match.group(2)) if match.group(2) is not None else None
    deferred_count = int(match.group(3)) if match.group(3) is not None else 0
    changes = {}
    if match.group(4) is not None:
        changes = {
            "created": int(match.group(4)),
            "updated": int(match.group(5)),
            "unchanged": int(match.group(6)),
            "closed": int(match.group(7)),
        }
    return FlowStats(
        raw_count=raw_count,
        new_count=new_count,
        deferred_count=deferred_count,
        closing_blocked=match.group(8) is not None,
        network=match.group(9),
        jina_pages=int(match.group(10)) if match.group(10) is not None else 0,
        network_reason=match.group(11),
        **changes,
    )


def warn_on_redirect(requested_url: str, final_url: str) -> bool:
    """
    Avisa quando a URL de listagem redirecionou para outro domínio ou caminho.

    Foi exatamente esse o sintoma da queda da FINEP: a URL antiga passou a
    responder 301 para um portal novo e o parser seguiu rodando contra uma
    página sem editais. Retorna True quando houve redirecionamento relevante.
    """
    if not requested_url or not final_url:
        return False
    if _canonical(requested_url) == _canonical(final_url):
        return False
    logger.warning(
        "URL de listagem redirecionou: %s -> %s. "
        "Se o parser depender da estrutura antiga, ele pode estar extraindo zero itens.",
        requested_url,
        final_url,
    )
    return True


def _canonical(url: str) -> str:
    """Normaliza esquema, 'www.' e barra final para comparar apenas domínio + caminho."""
    without_scheme = re.sub(r"^https?://", "", url.strip(), flags=re.IGNORECASE)
    without_www = re.sub(r"^www\.", "", without_scheme, flags=re.IGNORECASE)
    return without_www.rstrip("/").lower()
