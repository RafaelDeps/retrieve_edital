import logging
import re
from datetime import datetime
from typing import Callable, Dict, List, Optional, Set
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.core.interfaces import ISource
from src.domain.models import RawEdital
from src.components.sources.document_selection import combine_texts, select_documents
from src.components.sources.govbr_fetcher import FetchedDocument, GovBrFetcher

logger = logging.getLogger(__name__)

CAPES_EDITAIS_URL = "https://www.gov.br/capes/pt-br/assuntos/editais-e-resultados-capes"
CAPES_BASE_URL = "https://www.gov.br"
PDF_URL_REGEX = re.compile(r"https://[^\s\"'<>]+\.pdf", re.IGNORECASE)
# A área de conteúdo das páginas do gov.br é o <main> (validado em 26/11/2026 nas
# páginas da Cátedra Jorge Amado, do Prêmio CAPES de Tese e do Aurora). Menu,
# rodapé e barra do governo ficam fora dele.
CONTENT_SELECTOR = "main"
# O <main> começa com "<título> Publicado em … Modificado em … Compartilhe :".
PAGE_HEADER_REGEX = re.compile(r"^.*?Compartilhe\s*:\s*", re.DOTALL)
DESCRIPTION_MAX_CHARS = 1500
READ_FAILURE_DOWNLOAD = "download falhou"
READ_FAILURE_NO_TEXT = "documento sem texto"


def _pdfplumber_text(pdf_bytes: bytes) -> str:
    from src.components.transforms.extraction_prompts import extract_text_with_pdfplumber

    return extract_text_with_pdfplumber(pdf_bytes)


def page_content_text(html: str) -> str:
    """Texto da área de conteúdo da página, sem o cabeçalho de publicação."""
    soup = BeautifulSoup(html or "", "html.parser")
    main = soup.select_one(CONTENT_SELECTOR)
    if main is None:
        return ""
    for element in main.find_all(["script", "style", "nav", "button"]):
        element.decompose()
    text = main.get_text(" ", strip=True)
    if "Compartilhe" in text[:400]:
        text = PAGE_HEADER_REGEX.sub("", text, count=1)
    return " ".join(text.split())


def short_description(text: str, max_chars: int = DESCRIPTION_MAX_CHARS) -> str:
    """Começo do texto, cortado no fim de uma frase quando passa do limite."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = head.rfind(". ")
    return head[: cut + 1] if cut > max_chars // 3 else head.rstrip() + "…"


def _normalize_capes_url(href: str) -> str:
    if not href:
        return CAPES_EDITAIS_URL
    if href.startswith("http"):
        return href
    return urljoin(CAPES_BASE_URL, href)


def _is_pdf_link(url: str) -> bool:
    """
    True para links de documento, não de página.

    Só olhar a terminação `.pdf` não basta: o gov.br serve o mesmo arquivo em
    `…/edital.pdf/@@display-file/file`, e esses escapavam do filtro. Quando a
    fonte usava navegador, ele tentava abrir esses links como página e falhava
    com `Page.goto: Download is starting`, derrubando o item.
    """
    lowered = url.lower()
    return ".pdf" in lowered or lowered.endswith((".doc", ".docx", ".odt"))


class CapesSource(ISource[RawEdital]):
    """
    Extractor for CAPES editais.
    Reads the "Editais Abertos" section, visits each detail page and collects
    PDF attachments, preferring edital/chamada PDFs as OCR input.
    """

    def __init__(
        self,
        start_url: str = CAPES_EDITAIS_URL,
        processed_urls: Optional[Set[str]] = None,
        current_year: Optional[int] = None,
        fetcher: Optional[GovBrFetcher] = None,
        pdf_to_text: Optional[Callable[[bytes], str]] = None,
    ) -> None:
        self.start_url = start_url
        # Texto dos PDFs baixados direto (pdfplumber por padrão; injetável nos testes).
        self.pdf_to_text = pdf_to_text or _pdfplumber_text
        # A página é HTML estático: não precisa de navegador. Todo acesso ao
        # www.gov.br passa pelo fetcher, que desvia pelo Jina quando a conexão
        # direta é recusada (GitHub Actions, issue #7).
        self.fetcher = fetcher or GovBrFetcher(source_name="CAPES")
        self.processed_urls = processed_urls or set()
        self.current_year = current_year if current_year is not None else datetime.now().year
        # Quantos itens a listagem da origem devolveu, antes da deduplicação e
        # dos filtros. É o que permite ao runner distinguir "portal sem
        # novidade" de "source quebrado". Ver src/flow_health.py.
        self.last_listing_count = 0

    def _infer_year_from_text(self, text: str) -> Optional[int]:
        years = [int(value) for value in re.findall(r"\b(20\d{2})\b", text or "")]
        return max(years) if years else None

    def _extract_listing_entries(self, html: str) -> List[Dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        section_header = None

        for candidate in soup.find_all(["h2", "h3"]):
            text = candidate.get_text(" ", strip=True)
            if "Editais Abertos" in text:
                section_header = candidate
                break

        if section_header is None:
            return []

        entries: List[Dict[str, str]] = []
        seen_urls = set()
        container = section_header.find_next(["div", "ul"])
        if container is None:
            return []

        for anchor in container.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            title = anchor.get_text(" ", strip=True)
            if not href or not title:
                continue

            normalized_url = _normalize_capes_url(href)
            if _is_pdf_link(normalized_url):
                continue
            # `seen_urls` é deduplicação interna da listagem. A comparação com o
            # registry saiu daqui para o read(), para que a contagem bruta
            # signifique "a origem respondeu" e não "não havia nada novo".
            if normalized_url in seen_urls:
                continue
            if "gov.br/capes" not in normalized_url:
                continue
            inferred_year = self._infer_year_from_text(f"{title} {normalized_url}")
            if inferred_year is not None and inferred_year < self.current_year:
                continue

            entries.append({"title": title, "url": normalized_url})
            seen_urls.add(normalized_url)

        return entries

    def _extract_pdf_links(self, html: str) -> List[Dict[str, str]]:
        soup = BeautifulSoup(html, "html.parser")
        links_by_url: Dict[str, Dict[str, str]] = {}

        for anchor in soup.select("a[href]"):
            href = (anchor.get("href") or "").strip()
            normalized_url = _normalize_capes_url(href)
            if not _is_pdf_link(normalized_url):
                continue
            label = anchor.get_text(" ", strip=True) or normalized_url.rsplit("/", 1)[-1]
            links_by_url[normalized_url] = {
                "titulo": label,
                "link": normalized_url,
                "tipo": "anexo",
            }

        for pdf_url in PDF_URL_REGEX.findall(html):
            normalized_url = _normalize_capes_url(pdf_url)
            links_by_url.setdefault(
                normalized_url,
                {
                    "titulo": normalized_url.rsplit("/", 1)[-1],
                    "link": normalized_url,
                    "tipo": "anexo",
                },
            )

        return list(links_by_url.values())

    def _download_main_document(self, url: str) -> FetchedDocument:
        """O documento principal: bytes no acesso direto, texto pelo Jina."""
        try:
            return self.fetcher.get_document(url)
        except requests.RequestException as exc:
            logger.error("Error downloading CAPES file %s: %s", url, exc)
            return FetchedDocument()

    def _document_text(self, url: str) -> Optional[str]:
        """Texto de um documento: pdfplumber nos bytes, ou o texto pronto do Jina."""
        document = self._download_main_document(url)
        if document.text is not None:
            return document.text
        if not document.content:
            return None
        try:
            return self.pdf_to_text(document.content)
        except Exception as exc:  # PDF corrompido ou que não é PDF
            logger.warning("CAPES: texto ilegível em %s (%s).", url, type(exc).__name__)
            return ""

    def _extract_detail_page(
        self,
        detail_url: str,
        fallback_title: str,
    ) -> Optional[RawEdital]:
        html = self.fetcher.get_html(detail_url)
        soup = BeautifulSoup(html, "html.parser")
        title_el = soup.find(["h1", "h2"])
        title = title_el.get_text(" ", strip=True) if title_el else fallback_title

        page_text = page_content_text(html)
        anexos = self._extract_pdf_links(html)
        original, alteracoes = select_documents(anexos, newest_first=True)
        inferred_year = self._infer_year_from_text(
            f"{title} {detail_url} {original['link'] if original else ''} "
            f"{' '.join(item['titulo'] for item in anexos)}"
        )
        if inferred_year is not None and inferred_year < self.current_year:
            return None

        # Spec 010: o original, as alterações (da mais antiga para a mais nova) e
        # a página vão juntos para a IA, numa chamada só. A página entra mesmo
        # com edital: avisos como "ficam encerrados os ciclos" (Aurora) só estão nela.
        read_failure = ""
        origin = "documento"
        text = ""
        if original:
            original_text = self._document_text(original["link"])
            if original_text is None:
                read_failure = READ_FAILURE_DOWNLOAD
            elif not original_text.strip():
                read_failure = READ_FAILURE_NO_TEXT
            else:
                amendments = []
                for alteracao in alteracoes:
                    amendment_text = self._document_text(alteracao["link"])
                    if amendment_text and amendment_text.strip():
                        amendments.append((alteracao["titulo"], amendment_text))
                text, used = combine_texts((original["titulo"], original_text), amendments, page_text)
                logger.info(
                    "CAPES %s: edital original + %s alteração(ões) + página.", detail_url, used
                )
        elif page_text:
            origin = "página"
            text = page_text
        else:
            read_failure = READ_FAILURE_NO_TEXT

        return RawEdital(
            title=title or fallback_title,
            url=detail_url,
            raw_agency="CAPES",
            raw_description=short_description(page_text),
            pdf_text=text or None,
            raw_tags=["capes"],
            raw_anexos=anexos,
            read_failure=read_failure,
            document_origin=origin,
        )

    def read(self) -> List[RawEdital]:
        try:
            listing_html = self.fetcher.get_html(self.start_url, listing=True)
        except requests.RequestException as exc:
            logger.error("Error during CAPES extraction: %s", exc)
            self.last_listing_count = 0
            return []

        listing_entries = self._extract_listing_entries(listing_html)
        self.last_listing_count = len(listing_entries)
        raw_editais: List[RawEdital] = []

        for item in listing_entries:
            if item["url"] in self.processed_urls:
                continue
            try:
                raw = self._extract_detail_page(item["url"], item["title"])
                if raw:
                    raw_editais.append(raw)
            except Exception as exc:
                logger.error("Error extracting CAPES detail %s: %s", item["url"], exc)

        logger.info("CapesSource extracted %s editais.", len(raw_editais))
        return raw_editais
