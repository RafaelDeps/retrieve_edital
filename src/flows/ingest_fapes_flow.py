import logging
from typing import Optional

from dotenv import load_dotenv

from src.components.sources.fapes_source import FapesSource
from src.core.interfaces import ISink, ISource, ITransform
from src.domain.models import EditalDomain, RawEdital
from src.flows.common import FlowSpec, run_standard_pipeline
from src import rejection_store  # noqa: F401  (os testes trocam funções deste módulo)
from src.processed_store import DEFAULT_PATH

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)


def _processed_keys(editais) -> list:
    """
    Registra a URL do edital **e** as dos seus anexos.

    O FapesSource agrupa os documentos de um edital e elege um como principal
    conforme a classificação da IA, que varia entre execuções. Registrar só
    a URL do principal fazia a chave mudar de uma rodada para a outra: o mesmo
    edital voltava como novo, era reprocessado por OCR e sobrescrevia o arquivo
    anterior. Guardando todas as URLs do grupo, qualquer documento que reapareça
    identifica o edital já coletado.
    """
    keys: list = []
    for edital in editais:
        if edital.link:
            keys.append(edital.link)
        for anexo in edital.anexos or []:
            link = (anexo.get("link") or "").strip()
            if link:
                keys.append(link)
    return keys


SPEC = FlowSpec("fapes", "Retrieve Edital", processed_keys=_processed_keys)


def run_pipeline(
    source: Optional[ISource[RawEdital]] = None,
    transform: Optional[ITransform[RawEdital, EditalDomain]] = None,
    sink: Optional[ISink[EditalDomain]] = None,
    processed_index_path: str = DEFAULT_PATH,
) -> None:
    """
    Editais da FAPES. Deduplica por URL do documento, como as demais fontes, e
    registra no índice também os links dos anexos (`_processed_keys`).

    O roteiro (ler, transformar, estatísticas, recusas e erros, gravar, indexar)
    é o comum de `src/flows/common.py`; aqui fica só o que é desta fonte.
    """
    run_standard_pipeline(
        SPEC,
        lambda seen: FapesSource(processed_urls=seen),
        source=source,
        transform=transform,
        sink=sink,
        processed_index_path=processed_index_path,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_pipeline()
