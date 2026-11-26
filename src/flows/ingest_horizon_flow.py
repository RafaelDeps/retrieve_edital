import logging
from typing import Optional, Sequence

from dotenv import load_dotenv

from src.components import horizon_sync
from src.flows.common import error_stats, record_transform_errors, transform_all
from src.components.sinks.json_sink import LocalJSONSink
from src.components.sources.horizon_source import HorizonSource
from src.components.transforms.edital_normalizer import EditalNormalizer
from src.core.interfaces import ISink, ISource, ITransform
from src.domain.models import EditalDomain, RawEdital
from src.flow_health import emit_flow_stats
from src import rejection_store
from src.processed_store import DEFAULT_PATH, add_many, get_keys_set

load_dotenv()

logger = logging.getLogger(__name__)

FONTE_KEY = "horizon"


def run_pipeline(
    source: Optional[ISource[RawEdital]] = None,
    transform: Optional[ITransform[RawEdital, EditalDomain]] = None,
    sink: Optional[ISink[EditalDomain]] = None,
    divisions: Optional[Sequence[str]] = None,
    processed_index_path: str = DEFAULT_PATH,
) -> None:
    """
    Orquestra o ETL das chamadas do Horizon Europe (extract → transform → sink).

    Cadência semanal: o dataset de origem tem ~126 MB e muda pouco de um dia para
    o outro. Ver `.github/workflows/run_horizon_weekly.yml`.

    Toda chamada relevante é relida e reescrita a cada execução (issue #8): a
    leitura não usa IA, então não há custo em reler, e só assim prazo adiado,
    abertura e cancelamento chegam aos arquivos já gravados. Os índices de
    processados e de recusados não filtram a leitura; o de processados só diz o
    que é novo.
    """
    processed_links = get_keys_set(FONTE_KEY, path=processed_index_path)
    source = source or HorizonSource(divisions=divisions)
    transform = transform or EditalNormalizer()
    sink = sink or LocalJSONSink()

    # Retrato do que estava gravado, antes de o sink sobrescrever: base para
    # dizer o que mudou e para encerrar o que saiu da lista.
    load_saved = getattr(sink, "load_saved", None)
    saved = load_saved(FONTE_KEY) if load_saved else {}
    open_saved = horizon_sync.open_saved_links(saved)

    logger.info("Starting Horizon Europe Pipeline...")

    logger.info("Phase 1: Extraction")
    raw_data_list = source.read()

    # Quantas chamadas relevantes a origem devolveu antes da deduplicação: é o
    # que permite ao runner distinguir "nada novo" de "source quebrado".
    listing_count = getattr(source, "last_listing_count", len(raw_data_list))

    if not raw_data_list:
        # Sem leitura não há como saber o que saiu da lista: nada é encerrado, e
        # com chamadas abertas gravadas isso é sinalizado como trava.
        emit_flow_stats(
            raw_count=listing_count,
            new_count=0,
            closing_blocked=bool(open_saved),
        )
        logger.warning("Extraction returned empty. Halting pipeline.")
        return

    logger.info("Extracted %s raw records.", len(raw_data_list))
    logger.info("Phase 2: Transformation (Parallel)")
    outcome = transform_all(transform, raw_data_list)
    valid_domains = outcome.valid

    logger.info(
        "Successfully transformed %s out of %s records.",
        len(valid_domains),
        len(raw_data_list),
    )

    logger.info("Phase 3: Load/Sink")
    rejection_store.record(FONTE_KEY, getattr(transform, "rejections", {}))
    record_transform_errors(FONTE_KEY, outcome.errors)

    changes = {}
    if valid_domains:
        persisted = sink.write(valid_domains)
        written = list(persisted.values())
        changes = horizon_sync.classify_changes(written, saved, processed_links)
        add_many(FONTE_KEY, [d.link for d in written], path=processed_index_path)
        remove_duplicates = getattr(sink, "remove_older_duplicates", None)
        if remove_duplicates:
            removed = remove_duplicates({d.link for d in written})
            if removed:
                logger.info("Horizon: %s arquivo(s) duplicado(s) removido(s).", removed)
    else:
        logger.warning("No valid domains to sink.")

    closed, closing_blocked = _close_missing(source, sink, saved, open_saved)
    counts = horizon_sync.count_changes(changes)
    logger.info(
        "Horizon: %s novas, %s alteradas, %s sem mudança, %s encerradas.",
        counts[horizon_sync.CHANGE_NEW],
        counts[horizon_sync.CHANGE_UPDATED],
        counts[horizon_sync.CHANGE_UNCHANGED],
        closed,
    )
    # Emitido fora do `if`: quando todos os itens são recusados, o runner ainda
    # precisa saber que a origem respondeu.
    emit_flow_stats(
        raw_count=listing_count,
        new_count=counts[horizon_sync.CHANGE_NEW],
        created=counts[horizon_sync.CHANGE_NEW],
        updated=counts[horizon_sync.CHANGE_UPDATED],
        unchanged=counts[horizon_sync.CHANGE_UNCHANGED],
        closed=closed,
        closing_blocked=closing_blocked,
        **error_stats(outcome.errors),
    )
    logger.info("Pipeline completed.")


def _close_missing(source, sink, saved, open_saved) -> tuple[int, bool]:
    """
    Encerra as chamadas abertas gravadas que saíram da lista, salvo se a trava
    disparar (leitura incompleta ou queda brusca de relevantes).

    Devolve (quantas foram encerradas, se a trava disparou).
    """
    relevant_links = getattr(source, "last_relevant_links", set())
    reason = horizon_sync.closing_guard(
        getattr(source, "last_read_complete", False), len(relevant_links), len(open_saved)
    )
    if reason is not None:
        if open_saved:
            logger.warning("Encerramento por ausência suspenso: %s.", reason)
            return 0, True
        return 0, False

    mark_closed = getattr(sink, "mark_closed", None)
    if mark_closed is None:
        return 0, False
    closed = 0
    for link in horizon_sync.links_to_close(saved, relevant_links):
        path = saved[link][0]
        if mark_closed(path):
            logger.info("Chamada fora da lista do Horizon marcada como encerrada: %s", link)
            closed += 1
    return closed, False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_pipeline()
