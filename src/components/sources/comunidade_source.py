"""
Indicações de editais pela comunidade (spec 009).

O portal tem um formulário público ("Contribua") que abre uma issue em
RafaelDeps/portal_edital com título "[Indicação] …" e seções fixas (link oficial,
órgão, prazo, observações). Esta fonte lê essas issues e devolve, como
`RawEdital`, só as que valem a leitura pela IA:

- link inválido, já resolvido, já no acervo ou além do teto: não segue;
- link que não abre: recusa de validade curta;
- título ou texto com termo claro de outro assunto (concurso público, licitação…):
  recusa sem IA (`theme_filter`).

O que segue carrega `community` (os campos do formulário, só como reserva) e
`raw_fonte_key="comunidade"`. A verificação de tema pela IA e as regras de
publicação ficam no Transform; a resposta na issue, no fluxo.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from src.core.interfaces import ISource
from src.domain.models import RawEdital
from src.components.sources.github_issues import IndicacoesClient
from src.components.transforms.theme_filter import off_topic_term
from src.processed_store import DEFAULT_PATH as PROCESSED_PATH

logger = logging.getLogger(__name__)

FONTE_KEY = "comunidade"
SUGGESTION_LABEL = "indicacao-edital"
SUGGESTION_TITLE_PREFIX = "[Indicação]"
COMMUNITY_TAG = "indicação da comunidade"
MAX_PER_RUN_ENV = "COMUNIDADE_MAX_POR_EXECUCAO"
DEFAULT_MAX_PER_RUN = 10
NO_RESPONSE = "_No response_"
FETCH_TIMEOUT_SEC = 60
OUTPUT_DIRS = (Path("data/output"), Path("data/arquivo"))

FIELD_HEADINGS = {
    "link oficial do edital": "link",
    "órgão de fomento / instituição": "orgao",
    "prazo de encerramento": "prazo",
    "observações": "observacoes",
}

RESULT_ALREADY = "ja_no_portal"
RESULT_REFUSED = "recusado"

REASON_INVALID_LINK = "link inválido"
REASON_UNREACHABLE = "link inacessível"


def parse_indicacao(body: str) -> Dict[str, str]:
    """Campos do formulário a partir das seções `### …` do corpo da issue."""
    fields = {name: "" for name in FIELD_HEADINGS.values()}
    current = None
    lines: Dict[str, List[str]] = {}
    for line in (body or "").splitlines():
        heading = re.match(r"^###\s+(.+?)\s*$", line)
        if heading:
            current = FIELD_HEADINGS.get(heading.group(1).strip().lower())
            if current:
                lines[current] = []
            continue
        if current:
            lines[current].append(line)
    for name, content in lines.items():
        value = "\n".join(content).strip()
        fields[name] = "" if value == NO_RESPONSE else value
    return fields


def is_indicacao(issue: dict) -> bool:
    labels = {(label.get("name") or "") for label in issue.get("labels") or []}
    title = (issue.get("title") or "").strip()
    return SUGGESTION_LABEL in labels or title.startswith(SUGGESTION_TITLE_PREFIX)


def is_valid_url(value: str) -> bool:
    parsed = urlparse((value or "").strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc) and " " not in value.strip()


def max_per_run() -> int:
    raw = os.getenv(MAX_PER_RUN_ENV)
    try:
        value = int(raw) if raw and raw.strip() else DEFAULT_MAX_PER_RUN
    except ValueError:
        value = DEFAULT_MAX_PER_RUN
    return value if value > 0 else DEFAULT_MAX_PER_RUN


def load_known_links(
    output_dirs: Iterable[Path] = OUTPUT_DIRS, processed_path: str = PROCESSED_PATH
) -> Set[str]:
    """Links do acervo: JSONs publicados e arquivados e todos os índices de processados."""
    links: Set[str] = set()
    for folder in output_dirs:
        for path in Path(folder).glob("*.json") if Path(folder).is_dir() else []:
            try:
                link = (json.loads(path.read_text(encoding="utf-8")).get("link") or "").strip()
            except (OSError, ValueError, AttributeError):
                continue
            if link:
                links.add(link)
    try:
        data = json.loads(Path(processed_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for source, keys in (data or {}).items():
        if isinstance(keys, list):
            links.update(str(k).strip() for k in keys if k)
    return links


def default_fetch(url: str, govbr_fetcher=None) -> Tuple[Optional[bytes], Optional[str], str]:
    """
    Baixa o link indicado: (bytes do PDF, texto da página, título da página).

    O www.gov.br recusa o GitHub; ele passa pelo desvio da spec 006.
    """
    host = urlparse(url).netloc.lower()
    if host == "www.gov.br" and govbr_fetcher is not None:
        document = govbr_fetcher.get_document(url)
        if document.text is not None:
            return None, document.text, ""
        content, content_type = document.content, document.content_type
    else:
        response = requests.get(url, timeout=FETCH_TIMEOUT_SEC, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        content, content_type = response.content, (response.headers.get("content-type") or "").lower()
    if "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
        return content, None, ""
    soup = BeautifulSoup(content or b"", "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    text = soup.get_text(" ", strip=True)
    return None, text, title


class ComunidadeSource(ISource[RawEdital]):
    """Lê as indicações abertas do portal e devolve as que vão para a IA."""

    def __init__(
        self,
        client: Optional[IndicacoesClient] = None,
        fetch: Optional[Callable[[str], Tuple[Optional[bytes], Optional[str], str]]] = None,
        known_links: Optional[Callable[[], Set[str]]] = None,
        resolved_links: Optional[Set[str]] = None,
        max_per_run_value: Optional[int] = None,
        govbr_fetcher=None,
    ) -> None:
        self.client = client or IndicacoesClient()
        if fetch is None:
            if govbr_fetcher is None:
                from src.components.sources.govbr_fetcher import GovBrFetcher

                govbr_fetcher = GovBrFetcher(source_name="Comunidade")
            self.fetcher = govbr_fetcher
            fetch = lambda url: default_fetch(url, govbr_fetcher)  # noqa: E731
        self.fetch = fetch
        self.known_links = known_links or load_known_links
        self.resolved_links = set(resolved_links or ())
        self.max_per_run = max_per_run_value or max_per_run()
        self.last_listing_count = 0
        self.rejections: Dict[str, str] = {}
        self.rejections_short: Dict[str, str] = {}
        self.results: Dict[str, Tuple[str, str]] = {}
        self.issues_by_link: Dict[str, int] = {}
        self.invalid_issues: List[int] = []

    def read(self) -> List[RawEdital]:
        try:
            issues = [issue for issue in self.client.list_open() if is_indicacao(issue)]
        except requests.RequestException as exc:
            logger.error("Erro ao ler as indicações do portal: %s", exc)
            self.last_listing_count = 0
            return []
        self.last_listing_count = len(issues)
        logger.info("Indicações abertas no portal: %s.", len(issues))

        known = self.known_links()
        raw_items: List[RawEdital] = []
        to_read = 0
        for issue in issues:
            number = issue.get("number")
            fields = parse_indicacao(issue.get("body") or "")
            link = fields["link"].strip()
            if not is_valid_url(link):
                logger.info("Indicação #%s sem link válido.", number)
                self.invalid_issues.append(number)
                continue
            if link in self.resolved_links:
                continue  # já respondida (ou já tentada) antes
            if link in self.issues_by_link:
                continue  # o mesmo link em outra indicação desta execução
            self.issues_by_link[link] = number
            if link in known:
                self.results[link] = (RESULT_ALREADY, "")
                continue
            if to_read >= self.max_per_run:
                del self.issues_by_link[link]  # fica para a próxima execução
                continue
            to_read += 1
            raw = self._read_one(link, fields, number)
            if raw is not None:
                raw_items.append(raw)
        logger.info("Indicações enviadas para leitura: %s.", len(raw_items))
        return raw_items

    def _read_one(self, link: str, fields: Dict[str, str], number: int) -> Optional[RawEdital]:
        try:
            pdf_bytes, page_text, page_title = self.fetch(link)
        except Exception as exc:
            logger.warning("Indicação #%s: link inacessível (%s).", number, type(exc).__name__)
            self.rejections_short[link] = REASON_UNREACHABLE
            self.results[link] = (RESULT_REFUSED, REASON_UNREACHABLE)
            return None

        sample = page_text or ""
        if pdf_bytes and not sample:
            from src.components.transforms.extraction_prompts import extract_text_with_pdfplumber

            try:
                sample = extract_text_with_pdfplumber(pdf_bytes)
            except Exception:
                sample = ""
        term = off_topic_term(page_title, sample)
        if term:
            reason = f"fora do tema: {term}"
            logger.info("Indicação #%s recusada sem IA (%s).", number, reason)
            self.rejections[link] = reason
            self.results[link] = (RESULT_REFUSED, reason)
            return None

        return RawEdital(
            title=page_title or fields.get("orgao") or link,
            url=link,
            raw_agency=None,
            raw_description=None,
            pdf_content=pdf_bytes,
            pdf_text=None if pdf_bytes else (page_text or ""),
            document_type="edital",
            raw_tags=[COMMUNITY_TAG],
            community={"orgao": fields.get("orgao", ""), "prazo": fields.get("prazo", ""), "issue": str(number)},
            raw_fonte_key=FONTE_KEY,
        )
