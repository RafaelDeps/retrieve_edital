import logging
from typing import Optional

from dotenv import load_dotenv

from src.components.sources.finep_source import FinepSource
from src.config import get_reference_year
from src.core.interfaces import ISink, ISource, ITransform
from src.domain.models import EditalDomain, RawEdital
from src.flows.common import FlowSpec, run_standard_pipeline
from src import rejection_store  # noqa: F401  (os testes trocam funções deste módulo)
from src.processed_store import DEFAULT_PATH

load_dotenv()

logger = logging.getLogger(__name__)

SPEC = FlowSpec("finep", "FINEP Chamadas Públicas")


def run_pipeline(
    source: Optional[ISource[RawEdital]] = None,
    transform: Optional[ITransform[RawEdital, EditalDomain]] = None,
    sink: Optional[ISink[EditalDomain]] = None,
    reference_year: Optional[int] = None,
    max_pages: Optional[int] = None,
    processed_index_path: str = DEFAULT_PATH,
) -> None:
    """
    Chamadas públicas FINEP, com ano de referência do ambiente (REFERENCE_YEAR) ou
    o ano atual. Opcionalmente limita à primeira página com max_pages=1.

    O roteiro (ler, transformar, estatísticas, recusas e erros, gravar, indexar)
    é o comum de `src/flows/common.py`; aqui fica só o que é desta fonte.
    """
    run_standard_pipeline(
        SPEC,
        lambda seen: FinepSource(
            reference_year=reference_year or get_reference_year(),
            max_pages=max_pages,
            processed_urls=seen,
        ),
        source=source,
        transform=transform,
        sink=sink,
        processed_index_path=processed_index_path,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys

    # Por padrão roda só a primeira página (teste). Use --all para todas as páginas.
    run_pipeline(max_pages=1 if "--all" not in sys.argv else None)
