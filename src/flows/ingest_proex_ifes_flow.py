import logging
from typing import Optional

from dotenv import load_dotenv

from src.components.sources.proex_ifes_source import ProexIfesSource
from src.core.interfaces import ISink, ISource, ITransform
from src.domain.models import EditalDomain, RawEdital
from src.flows.common import FlowSpec, run_standard_pipeline
from src import rejection_store  # noqa: F401  (os testes trocam funções deste módulo)
from src.processed_store import DEFAULT_PATH

load_dotenv()

logger = logging.getLogger(__name__)

SPEC = FlowSpec("proex_ifes", "PROEX/IFES Editais")


def run_pipeline(
    source: Optional[ISource[RawEdital]] = None,
    transform: Optional[ITransform[RawEdital, EditalDomain]] = None,
    sink: Optional[ISink[EditalDomain]] = None,
    current_year: Optional[int] = None,
    processed_index_path: str = DEFAULT_PATH,
) -> None:
    """
    Editais PROEX/IFES, com deduplicação por URL.

    O roteiro (ler, transformar, estatísticas, recusas e erros, gravar, indexar)
    é o comum de `src/flows/common.py`; aqui fica só o que é desta fonte.
    """
    run_standard_pipeline(
        SPEC,
        lambda seen: ProexIfesSource(current_year=current_year, processed_urls=seen),
        source=source,
        transform=transform,
        sink=sink,
        processed_index_path=processed_index_path,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_pipeline()
