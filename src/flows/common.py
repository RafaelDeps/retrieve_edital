"""
Roteiro comum dos fluxos ETL e registro de erros de transformação (issue #10, spec 007).

Os fluxos FAPES, FINEP, CONIF, PRPPG/IFES, PROEX/IFES, CAPES e CNPq tinham o mesmo
corpo copiado: ler a fonte, transformar em paralelo, emitir estatísticas,
registrar recusas, gravar e indexar. Cada correção precisava ser repetida em
sete arquivos. Agora cada fluxo só declara o que é dele (`FlowSpec` e como montar
a fonte) e chama `run_standard_pipeline`. O Horizon, com roteiro próprio desde a
spec 005, usa só `transform_all`.

Um item cuja transformação lança erro deixava apenas uma linha no log da execução:
não entrava em índice nenhum, era baixado e lido de novo em toda execução e o
fluxo aparecia como Sucesso. Agora ele vai para o índice de recusados com
validade curta (padrão 3 dias), entra na contagem `errors=` e o runner marca
Atenção.
"""

import logging
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set

from src import rejection_store
from src.core.interfaces import ISink, ISource, ITransform
from src.domain.models import EditalDomain, RawEdital
from src.flow_health import emit_flow_stats, network_stats
from src.processed_store import DEFAULT_PATH, add_many, get_keys_set

logger = logging.getLogger(__name__)

TRANSFORM_MAX_WORKERS = 2  # conservador por causa dos limites do provedor de IA
TRANSFORM_ERROR_TTL_ENV = "TRANSFORM_ERROR_TTL_DAYS"
DEFAULT_TRANSFORM_ERROR_TTL_DAYS = 3
TRANSFORM_ERROR_REASON = "erro na transformação: {error_type}"


@dataclass(frozen=True)
class TransformError:
    url: str
    title: str
    error_type: str
    message: str


@dataclass
class TransformOutcome:
    valid: List[EditalDomain] = field(default_factory=list)
    errors: List[TransformError] = field(default_factory=list)


def transform_all(
    transform: ITransform, raw_items: List[RawEdital], max_workers: int = TRANSFORM_MAX_WORKERS
) -> TransformOutcome:
    """Transforma o lote em paralelo; devolve os válidos (na ordem do lote) e os erros."""
    from src.components.transforms.extraction_service import AIUnavailableError

    outcome = TransformOutcome()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [(executor.submit(transform.process, item), item) for item in raw_items]
        for future, raw_item in futures:
            try:
                domain = future.result()
            except AIUnavailableError as exc:
                # O normalizador converte isso em adiamento (spec 004); se chegar
                # aqui, não é erro do item.
                logger.warning("Item adiado (IA indisponível: %s): %s", exc.reason, raw_item.title)
                continue
            except Exception as exc:
                logger.error("Failed to transform item %s: %s", raw_item.title, exc)
                outcome.errors.append(
                    TransformError(
                        url=(raw_item.url or "").strip(),
                        title=raw_item.title or "",
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue
            if domain:
                outcome.valid.append(domain)
    return outcome


def transform_error_ttl_days() -> int:
    raw = os.getenv(TRANSFORM_ERROR_TTL_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_TRANSFORM_ERROR_TTL_DAYS
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        logger.warning(
            "%s inválido (%r); usando o padrão %s.",
            TRANSFORM_ERROR_TTL_ENV, raw, DEFAULT_TRANSFORM_ERROR_TTL_DAYS,
        )
        return DEFAULT_TRANSFORM_ERROR_TTL_DAYS
    return value


def record_transform_errors(fonte_key: str, errors: List[TransformError]) -> int:
    """Registra os erros com URL no índice de recusados, com validade curta."""
    entries = {
        error.url: TRANSFORM_ERROR_REASON.format(error_type=error.error_type)
        for error in errors
        if error.url
    }
    if not entries:
        return 0
    return rejection_store.record(fonte_key, entries, ttl_days=transform_error_ttl_days())


def error_stats(errors: List[TransformError]) -> dict:
    """Campos `errors`/`error_types` para `emit_flow_stats`; vazio sem erros."""
    if not errors:
        return {}
    counts = Counter(error.error_type for error in errors)
    ordered = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    return {
        "errors": len(errors),
        "error_types": ",".join(f"{name}:{count}" for name, count in ordered),
    }


def links_of(persisted: Iterable[EditalDomain]) -> List[str]:
    return [domain.link for domain in persisted]


@dataclass
class PipelineResult:
    """O que uma execução do roteiro comum produziu (usado por quem responde depois)."""

    persisted: Dict[str, str] = field(default_factory=dict)  # link -> nome
    rejections: Dict[str, str] = field(default_factory=dict)  # link -> motivo
    errors: List[TransformError] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)


def record_source_rejections(fonte_key: str, source) -> Dict[str, str]:
    """
    Recusas feitas pela própria fonte, antes da transformação (ex.: o filtro de
    tema das indicações, spec 009). `rejections_short` usa a validade curta dos
    erros (ex.: link inacessível, que pode voltar logo).
    """
    normal = dict(getattr(source, "rejections", None) or {})
    short = dict(getattr(source, "rejections_short", None) or {})
    if normal:
        rejection_store.record(fonte_key, normal)
    if short:
        rejection_store.record(fonte_key, short, ttl_days=transform_error_ttl_days())
    return {**normal, **short}


@dataclass(frozen=True)
class FlowSpec:
    """O que diferencia um fluxo comum: a chave da fonte, o rótulo e as chaves do índice."""

    fonte_key: str
    label: str
    processed_keys: Callable[[Iterable[EditalDomain]], List[str]] = links_of


def run_standard_pipeline(
    spec: FlowSpec,
    make_source: Callable[[Set[str]], ISource[RawEdital]],
    source: Optional[ISource[RawEdital]] = None,
    transform: Optional[ITransform[RawEdital, EditalDomain]] = None,
    sink: Optional[ISink[EditalDomain]] = None,
    processed_index_path: str = DEFAULT_PATH,
) -> PipelineResult:
    """Ler → transformar → estatísticas → recusas e erros → gravar → indexar."""
    from src.components.sinks.json_sink import LocalJSONSink
    from src.components.transforms.edital_normalizer import EditalNormalizer

    # Recusas (e erros) ainda válidas contam como já vistas: sem isso, um item
    # recusado volta como novo em toda execução, com PDF baixado e IA de novo.
    seen = get_keys_set(spec.fonte_key, path=processed_index_path)
    seen = seen | rejection_store.get_active_keys(spec.fonte_key)
    source = source or make_source(seen)
    transform = transform or EditalNormalizer()
    sink = sink or LocalJSONSink()

    logger.info("Starting %s Pipeline...", spec.label)
    logger.info("Phase 1: Extraction")
    raw_data_list = source.read()

    # Quantos itens a origem devolveu antes da deduplicação: é o que permite ao
    # runner separar "portal sem novidade" de "source quebrado".
    listing_count = getattr(source, "last_listing_count", len(raw_data_list))

    if not raw_data_list:
        emit_flow_stats(raw_count=listing_count, new_count=0, **network_stats(source))
        result = PipelineResult(rejections=record_source_rejections(spec.fonte_key, source))
        logger.warning("Extraction returned empty. Halting pipeline.")
        return result

    logger.info("Extracted %s raw records.", len(raw_data_list))
    logger.info("Phase 2: Transformation (Parallel)")
    outcome = transform_all(transform, raw_data_list)
    logger.info(
        "Successfully transformed %s out of %s records.", len(outcome.valid), len(raw_data_list)
    )

    logger.info("Phase 3: Load/Sink")
    # Emitido antes do sink, e fora do `if`: quando todos os itens são recusados,
    # o runner ainda precisa saber que a origem respondeu.
    emit_flow_stats(
        raw_count=listing_count,
        new_count=len(outcome.valid),
        deferred_count=len(getattr(transform, "deferred", [])),
        **error_stats(outcome.errors),
        **network_stats(source),
    )
    source_rejections = record_source_rejections(spec.fonte_key, source)
    transform_rejections = dict(getattr(transform, "rejections", None) or {})
    rejection_store.record(spec.fonte_key, transform_rejections)
    record_transform_errors(spec.fonte_key, outcome.errors)
    result = PipelineResult(
        rejections={**source_rejections, **transform_rejections},
        errors=list(outcome.errors),
        deferred=list(getattr(transform, "deferred", None) or []),
    )

    if outcome.valid:
        persisted = sink.write(outcome.valid)
        add_many(
            spec.fonte_key,
            spec.processed_keys(persisted.values()),
            path=processed_index_path,
        )
        result.persisted = {d.link: d.nome for d in persisted.values()}
        logger.info("Pipeline completed successfully.")
    else:
        logger.warning("No valid domains to sink. Pipeline finished with warnings.")
    return result
