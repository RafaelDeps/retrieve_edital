"""
Prazos lidos do edital ou da página; sem prazo, fluxo contínuo (spec 010, issue #9).

Páginas reais da CAPES e anexos reais da FINEP (26/11/2026); downloads, IA e
índices falsos ou em tmp_path. Nada abre rede.
"""

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from src import continuous_flow_store
from src.components.sources.capes_source import CapesSource
from src.components.sources.finep_source import FinepSource
from src.components.sources.govbr_fetcher import FetchedDocument
from src.components.transforms.edital_normalizer import EditalNormalizer
from src.components.transforms.extraction_prompts import SupportingDocumentError, map_to_domain
from src.domain.models import RawEdital
from src.flows.common import record_continuous_flow, transform_all
from scripts.run_all_flows import select_continuous_flow_rereads

scenarios("../../docs/features/prazos_fluxo_continuo.feature")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "prazos"
CAPES_URL = "https://www.gov.br/capes/pt-br/assuntos/{name}"


class FakeAI:
    """Registra o texto recebido e responde o que o cenário definiu."""

    def __init__(self):
        self.texts = []
        self.response = {"nome": "Edital", "cronograma": []}
        self.categorized = 0

    def extract_from_text(self, text, filename, check_theme=False):
        self.texts.append(text)
        if self.response.get("tipo_documento") == "apoio":
            raise SupportingDocumentError()
        return map_to_domain(dict(self.response), text)

    def extract_from_pdf(self, content, filename, check_theme=False):
        pytest.fail("a spec 010 manda texto, não bytes")

    def categorize_finep_by_description(self, description):
        self.categorized += 1
        return "inovação"


class FakeFetcher:
    def __init__(self, html, documents):
        self.html = html
        self.documents = documents  # {trecho do link: texto ou None (falha)}

    def get_html(self, url, listing=False):
        return self.html

    def get_document(self, url):
        for key, text in self.documents.items():
            if key in url:
                return FetchedDocument(content=text.encode() if text is not None else None)
        return FetchedDocument(content=b"anexo")


@pytest.fixture
def ctx():
    return {"ai": FakeAI(), "documents": {}}


def _closing(date_iso):
    return [{"evento": "Encerramento das inscrições", "data": date_iso}]


# ---- Given -----------------------------------------------------------------


@given(parsers.parse('the real CAPES page "{name}"'))
def capes_page(ctx, name):
    ctx["name"] = name
    ctx["html"] = (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@given(parsers.parse(
    'the real CAPES page "{name}" with the edital text "{edital}" and the amendment text "{alteracao}"'
))
def capes_page_with_texts(ctx, name, edital, alteracao):
    capes_page(ctx, name)
    # Cátedra: Edital_11_2026 (original) e edital_n__11_2026 (alteração).
    # Aurora: Edital_16_2026 (original) e edital_2853575 / Edital_2829258 (alterações).
    ctx["documents"] = {
        "SEI_2806040_Edital_11_2026": edital,
        "sei_2843522_edital_n__11_2026": alteracao,
        "Edital_16_2026__1_": edital,
        "edital_2853575_edital": alteracao,
        "Edital_2829258_Edital": alteracao,
    }


@given(parsers.parse('the real CAPES page "{name}" whose edital download fails'))
def capes_page_download_fails(ctx, name):
    capes_page(ctx, name)
    ctx["documents"] = {"SEI_2806040_Edital_11_2026": None}


@given(parsers.parse('the page text says "{sentence}"'))
def page_says(ctx, sentence):
    ctx["html"] = ctx["html"].replace("</main>", f"<p>{sentence}</p></main>")


@given(parsers.parse("the AI answers the closing date {closing}"))
def ai_closing(ctx, closing):
    ctx["ai"].response = {"nome": "Edital", "cronograma": _closing(closing), "fluxo_continuo": False}


@given("the AI answers without a closing date")
@given("the AI answers without a closing date and without the declaration")
def ai_no_closing(ctx):
    ctx["ai"].response = {"nome": "Edital", "cronograma": [], "fluxo_continuo": False}


@given(parsers.parse('a FAPES document "{title}"'))
def fapes_document(ctx, title):
    ctx["raw"] = RawEdital(
        title=title, url="https://fapes.es.gov.br/diretrizes.pdf", raw_agency="FAPES",
        pdf_text="Diretrizes para participação de pesquisadores capixabas no Horizonte Europa.",
    )


@given("the AI classifies it as a supporting document")
def ai_support(ctx):
    ctx["ai"].response = {"nome": "Diretrizes", "tipo_documento": "apoio"}


def _finep_item(deadline=None):
    data = json.loads((FIXTURES / "finep_anexos_fip_nordeste.json").read_text(encoding="utf-8"))
    item = {
        "id": 777, "titulo": data["titulo"], "descricaoRawText": "Seleção de gestor de fundo de capital semente.",
        "situacao": {"name": "Aberta"}, "prazoProposto": deadline,
    }
    return item, data["anexos"]


@given(parsers.parse('the FINEP call "{title}" without a deadline and with its real attachments'))
def finep_no_deadline(ctx, title):
    ctx["finep"] = _finep_item()


@given(parsers.parse('the FINEP call "{title}" with the API deadline {deadline} and with its real attachments'))
def finep_with_deadline(ctx, title, deadline):
    ctx["finep"] = _finep_item(f"{deadline}T23:59:00Z")


@given("the portal output has open continuous flows read 8 days ago, 3 days ago, declared, and never recorded")
def output_with_flows(ctx, tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    links = {"oito": "sem-prazo:2026-11-18", "tres": "sem-prazo:2026-11-23", "declarado": "declarada:2026-11-01", "nunca": None}
    index = {}
    for name, entry in links.items():
        link = f"https://www.gov.br/capes/{name}"
        (out / f"{name}.json").write_text(json.dumps({
            "link": link, "fonte_key": "capes", "status": "aberto", "modalidade": "fluxo-contínuo",
        }), encoding="utf-8")
        if entry:
            origem, lido_em = entry.split(":")
            index[link] = {"origem": origem, "lido_em": lido_em}
    # Aberto sem prazo e sem modalidade (o estado da issue #9) também é relido;
    # com prazo, não.
    for name, closing in (("sem_nada", ""), ("com_prazo", "2026-12-20")):
        (out / f"{name}.json").write_text(json.dumps({
            "link": f"https://www.gov.br/capes/{name}", "fonte_key": "capes", "status": "aberto",
            "modalidade": "", "data_encerramento": closing,
        }), encoding="utf-8")
    (tmp_path / "fluxo_continuo.json").write_text(json.dumps(index), encoding="utf-8")
    ctx.update(output=out, index_path=str(tmp_path / "fluxo_continuo.json"))


@given(parsers.parse('the index has "{link}" as "{origem}"'))
def index_has(ctx, tmp_path, link, origem):
    ctx["processed"] = str(tmp_path / "processed.json")
    continuous_flow_store.record({link: origem}, path=continuous_flow_store.path_next_to(ctx["processed"]))
    ctx["link"] = link


# ---- When ------------------------------------------------------------------


def _capes_raw(ctx):
    texts = ctx["documents"]
    fetcher = FakeFetcher(ctx["html"], texts)
    source = CapesSource(current_year=2026, fetcher=fetcher, pdf_to_text=lambda pdf: pdf.decode())
    return source._extract_detail_page(CAPES_URL.format(name=ctx["name"]), "Fallback")


@when(parsers.parse("the CAPES item is read and normalized on {today}"))
def capes_normalized(ctx, today):
    ctx["raw"] = _capes_raw(ctx)
    ctx["normalizer"] = EditalNormalizer(extraction_service=ctx["ai"], today=date.fromisoformat(today))
    ctx["result"] = ctx["normalizer"].process(ctx["raw"])


@when(parsers.parse("the CAPES item is transformed on {today}"))
def capes_transformed(ctx, today):
    ctx["raw"] = _capes_raw(ctx)
    normalizer = EditalNormalizer(extraction_service=ctx["ai"], today=date.fromisoformat(today))
    ctx["outcome"] = transform_all(normalizer, [ctx["raw"]])
    ctx["result"] = ctx["outcome"].valid[0] if ctx["outcome"].valid else None


@when(parsers.parse("it is normalized on {today}"))
def normalized(ctx, today):
    ctx["normalizer"] = EditalNormalizer(extraction_service=ctx["ai"], today=date.fromisoformat(today))
    ctx["result"] = ctx["normalizer"].process(ctx["raw"])


@when(parsers.parse("the FINEP call is read and normalized on {today}"))
def finep_normalized(ctx, today):
    item, anexos = ctx["finep"]
    api = SimpleNamespace(download=lambda url: b"Texto do edital FIP Nordeste.")
    source = FinepSource(reference_year=2026, api_client=api, pdf_to_text=lambda pdf: pdf.decode())
    source._fetch_anexos = lambda chamada_id: anexos
    ctx["raw"] = source._build_raw_edital(item)
    ctx["normalizer"] = EditalNormalizer(extraction_service=ctx["ai"], today=date.fromisoformat(today))
    ctx["result"] = ctx["normalizer"].process(ctx["raw"])


@when(parsers.parse("the daily runner selects the rereads on {today}"))
def select_rereads(ctx, today):
    ctx["selected"] = select_continuous_flow_rereads(
        ctx["output"], ctx["index_path"], date.fromisoformat(today), 7
    )


@when("the item is persisted again with a deadline")
def persisted_again(ctx):
    transform = SimpleNamespace(continuous_flow={})
    record_continuous_flow(transform, [SimpleNamespace(link=ctx["link"])], ctx["processed"])


# ---- Then ------------------------------------------------------------------


@then("the AI was called once")
def called_once(ctx):
    assert len(ctx["ai"].texts) == 1


@then("the AI was not called")
def not_called(ctx):
    assert ctx["ai"].texts == []


@then("the FINEP category classifier was not called")
def no_categorize(ctx):
    assert ctx["ai"].categorized == 0


@then("the AI received the original edital before the amendment and then the page")
def original_first(ctx):
    text = ctx["ai"].texts[0]
    assert text.startswith("=== EDITAL ORIGINAL: Edital nº")
    assert text.index("=== EDITAL ORIGINAL") < text.index("=== ALTERAÇÃO 1") < text.index("=== PÁGINA DO EDITAL")


@then("the AI received only the page text")
def only_page(ctx):
    text = ctx["ai"].texts[0]
    assert ctx["raw"].document_origin == "página"
    assert "===" not in text and text.startswith("O Prêmio CAPES de Tese reconhece")


@then(parsers.parse('the AI text contains "{fragment}"'))
def ai_text_contains(ctx, fragment):
    assert fragment in ctx["ai"].texts[0]


@then(parsers.parse('the description starts with "{start}"'))
def description_starts(ctx, start):
    assert ctx["raw"].raw_description.startswith(start)
    assert "Ir para o conteúdo" not in ctx["result"].descrição


@then(parsers.parse("the edital is published with the closing date {closing} and no modality"))
def published_with_closing(ctx, closing):
    assert ctx["result"].data_encerramento == closing
    assert ctx["result"].modalidade == ""
    assert ctx["raw"].url not in ctx["normalizer"].continuous_flow


@then(parsers.parse('the edital is published as "{modalidade}" with origin "{origem}"'))
def published_as(ctx, modalidade, origem):
    assert ctx["result"].modalidade == modalidade
    assert ctx["normalizer"].continuous_flow[ctx["raw"].url] == origem


@then("the published name is the API name")
def api_name(ctx):
    assert ctx["result"].nome == "EDITAL FIP NORDESTE CAPITAL SEMENTE"


@then("nothing is published")
def nothing_published(ctx):
    assert ctx["result"] is None


@then(parsers.parse('the item error is "{error_type}"'))
def item_error(ctx, error_type):
    assert [e.error_type for e in ctx["outcome"].errors] == [error_type]


@then(parsers.parse('the refusal reason is "{reason}"'))
def refusal_reason(ctx, reason):
    assert ctx["normalizer"].rejections[ctx["raw"].url] == reason


@then("only the ones read 8 days ago, never recorded, or open without deadline and modality are selected")
def selected(ctx):
    assert ctx["selected"] == {
        "capes": [
            "https://www.gov.br/capes/nunca",
            "https://www.gov.br/capes/oito",
            "https://www.gov.br/capes/sem_nada",
        ]
    }


@then(parsers.parse('the index no longer has "{link}"'))
def index_cleared(ctx, link):
    assert link not in continuous_flow_store.load(continuous_flow_store.path_next_to(ctx["processed"]))
