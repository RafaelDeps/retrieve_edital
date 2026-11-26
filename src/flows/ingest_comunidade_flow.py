import logging
from typing import Optional

from dotenv import load_dotenv

from src.components.sources.comunidade_source import (
    REASON_INVALID_LINK,
    RESULT_ALREADY,
    RESULT_REFUSED,
    ComunidadeSource,
)
from src.components.sources.github_issues import IndicacoesClient
from src.core.interfaces import ISink, ISource, ITransform
from src.domain.models import EditalDomain, RawEdital
from src.flows.common import FlowSpec, PipelineResult, run_standard_pipeline, transform_error_ttl_days
from src import rejection_store  # noqa: F401  (os testes trocam funções deste módulo)
from src.processed_store import DEFAULT_PATH

load_dotenv()

logger = logging.getLogger(__name__)

SPEC = FlowSpec("comunidade", "Indicações da comunidade")
SIGNATURE = "\n\n— coletor do Portal de Editais (resposta automática)"


def run_pipeline(
    source: Optional[ISource[RawEdital]] = None,
    transform: Optional[ITransform[RawEdital, EditalDomain]] = None,
    sink: Optional[ISink[EditalDomain]] = None,
    client: Optional[IndicacoesClient] = None,
    processed_index_path: str = DEFAULT_PATH,
) -> PipelineResult:
    """
    Indicações de editais feitas pela comunidade no portal (spec 009).

    O roteiro comum lê, filtra, publica e registra; depois, cada indicação lida
    recebe a resposta na própria issue (só com PORTAL_ISSUES_TOKEN).
    """
    client = client or IndicacoesClient()
    built = {}

    def make_source(seen):
        built["source"] = ComunidadeSource(client=client, resolved_links=seen)
        return built["source"]

    result = run_standard_pipeline(
        SPEC,
        make_source,
        source=source,
        transform=transform,
        sink=sink,
        processed_index_path=processed_index_path,
    )
    reply_all(source or built.get("source"), result, client)
    return result


def build_replies(source, result: PipelineResult) -> list:
    """(número da issue, texto, fechar?) para cada indicação resolvida nesta execução."""
    replies = []
    errors = {error.url: error for error in result.errors}
    for link, number in (getattr(source, "issues_by_link", None) or {}).items():
        if link in result.persisted:
            text, close = f"✅ Publicado no portal: {result.persisted[link]}", True
        elif link in errors:
            text = (
                "❌ Não publicado por enquanto: erro ao ler o edital; nova tentativa em "
                f"{transform_error_ttl_days()} dias."
            )
            close = False
        elif link in result.rejections:
            text, close = f"❌ Não publicado: {result.rejections[link]}", True
        else:
            kind, reason = (getattr(source, "results", None) or {}).get(link, ("", ""))
            if kind == RESULT_ALREADY:
                text, close = "✅ Já está no portal.", True
            elif kind == RESULT_REFUSED:
                text, close = f"❌ Não publicado: {reason}", True
            else:
                continue  # adiado (IA indisponível) ou além do teto: volta na próxima execução
        replies.append((number, text + SIGNATURE, close))
    for number in getattr(source, "invalid_issues", None) or []:
        replies.append((number, f"❌ Não publicado: {REASON_INVALID_LINK}." + SIGNATURE, True))
    return replies


def reply_all(source, result: PipelineResult, client) -> int:
    if source is None:
        return 0
    replies = build_replies(source, result)
    if not replies:
        return 0
    if not getattr(client, "can_reply", False):
        logger.info("Respostas puladas (sem PORTAL_ISSUES_TOKEN): %s.", len(replies))
        return 0
    answered = 0
    for number, text, close in replies:
        try:
            client.comment(number, text)
            if close:
                client.close(number)
            answered += 1
        except Exception as exc:
            logger.error("Falha ao responder a indicação #%s: %s", number, exc)
    logger.info("Indicações respondidas: %s de %s.", answered, len(replies))
    return answered


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_pipeline()
