"""
CAPES e CNPq de volta no GitHub (RafaelDeps/retrieve_edital#7, spec 006).

O www.gov.br derruba toda conexão vinda dos servidores do GitHub Actions
(diagnóstico de 26/11/2026: `Connection reset by peer` no handshake). O fetcher
tenta direto e, se a conexão cair, busca pelo Jina Reader. Aqui o acesso direto
e o Jina são falsos: nada abre rede e nada espera de verdade.
"""

import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from pytest_bdd import given, parsers, scenarios, then, when

from scripts import run_all_flows
from src.components.sources.capes_source import CapesSource
from src.components.sources.cnpq_source import CNPQ_ABERTAS_URL, CnpqSource
from src.components.sources.govbr_fetcher import (
    JINA_READER_URL,
    GovBrFetcher,
    GovBrUnavailable,
    strip_jina_header,
)
from src.components.transforms.edital_normalizer import EditalNormalizer
from src.domain.models import RawEdital
from src.flow_health import parse_flow_stats
from tests.step_defs.test_cnpq_source import CHAMADA_DIR, CHAMADA_URL, LISTING_HTML, build_detail_html

scenarios("../../docs/features/govbr_desvio_jina.feature")

JINA_PDF_TEXT = (Path(__file__).resolve().parents[1] / "fixtures" / "govbr" / "pdf_jina.txt").read_text(
    encoding="utf-8"
)
CNPQ_PDF_URL = f"{CHAMADA_DIR}/Chamada252026.pdf"

CAPES_LISTING_URL = "https://www.gov.br/capes/pt-br/assuntos/editais-e-resultados-capes"
CAPES_DETAIL_URL = "https://www.gov.br/capes/pt-br/assuntos/edital-a"
CAPES_PDF_URL = "https://www.gov.br/capes/pt-br/centrais-de-conteudo/editais/edital-a.pdf"
CAPES_LISTING_HTML = f"""<html><body>
  <h2>Editais Abertos</h2>
  <div><ul><li><a href="{CAPES_DETAIL_URL}">Edital A 2026</a></li></ul></div>
</body></html>"""
CAPES_DETAIL_HTML = f"""<html><body>
  <h1>Edital CAPES 2026</h1>
  <p>Este edital promove a cooperação acadêmica internacional com apoio financeiro.</p>
  <a href="{CAPES_PDF_URL}">Edital CAPES 2026</a>
</body></html>"""

CNPQ_PAGES = {CNPQ_ABERTAS_URL: LISTING_HTML, CHAMADA_URL: build_detail_html()}
CAPES_PAGES = {CAPES_LISTING_URL: CAPES_LISTING_HTML, CAPES_DETAIL_URL: CAPES_DETAIL_HTML}
PDF_BYTES = b"%PDF-1.4 falso"
FAKE_KEY = "JINA-SEGREDO-123"


def connection_reset():
    return requests.ConnectionError(ConnectionResetError(104, "Connection reset by peer"))


class FakeResponse:
    def __init__(self, status_code=200, text="", content=b"", headers=None, url=""):
        self.status_code = status_code
        self.text = text
        self.content = content
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class DirectSession:
    """O www.gov.br visto do servidor: responde, ou derruba a conexão."""

    def __init__(self, pages, refuse=False, missing=()):
        self.pages, self.refuse, self.missing = pages, refuse, set(missing)
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        if self.refuse:
            raise connection_reset()
        if url in self.missing:
            return FakeResponse(404, url=url)
        if ".pdf" in url:
            return FakeResponse(content=PDF_BYTES, headers={"content-type": "application/pdf"}, url=url)
        return FakeResponse(text=self.pages.get(url, "<html></html>"), url=url)


class JinaSession:
    """O Jina Reader falso: HTML das páginas, texto dos PDFs, ou respostas programadas."""

    def __init__(self, pages, script=None):
        self.pages = pages
        self.script = list(script or [])
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        assert url.startswith(JINA_READER_URL)
        target = url[len(JINA_READER_URL):]
        self.calls.append({"url": target, "headers": dict(headers or {})})
        if self.script:
            return self.script.pop(0)
        if (headers or {}).get("X-Return-Format") == "html":
            return FakeResponse(text=self.pages.get(target, "<html></html>"))
        return FakeResponse(text=JINA_PDF_TEXT)


class FakeClock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []
        self._lock = threading.Lock()

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds


def fetcher(direct, jina, clock=None, name="teste"):
    clock = clock or FakeClock()
    return GovBrFetcher(direct_session=direct, jina_session=jina, clock=clock, sleep=clock.sleep,
                        source_name=name), clock


def read_cnpq(direct, jina, clock=None):
    f, clock = fetcher(direct, jina, clock, "CNPq")
    source = CnpqSource(current_year=2026, fetcher=f)
    return source, source.read(), clock


def read_capes(direct, jina):
    f, _ = fetcher(direct, jina, name="CAPES")
    source = CapesSource(current_year=2026, fetcher=f, pdf_to_text=lambda pdf: pdf.decode("latin-1"))
    return source, source.read()


def comparable(raws):
    return [(r.title, r.url, r.raw_cronograma, r.raw_anexos) for r in raws]


@pytest.fixture
def ctx():
    return {}


# ---- Given -----------------------------------------------------------------


@given("www.gov.br answers the CNPq pages directly")
def cnpq_direct(ctx):
    ctx.update(source="cnpq", direct=DirectSession(CNPQ_PAGES), jina=JinaSession(CNPQ_PAGES))


@given("www.gov.br answers the CAPES pages directly")
def capes_direct(ctx):
    ctx.update(source="capes", direct=DirectSession(CAPES_PAGES), jina=JinaSession(CAPES_PAGES))


@given("www.gov.br refuses the connection to the CNPq pages")
def cnpq_refused(ctx):
    ctx.update(source="cnpq", direct=DirectSession(CNPQ_PAGES, refuse=True), jina=JinaSession(CNPQ_PAGES))


@given("www.gov.br answers the CNPq listing but the call page is missing")
def cnpq_missing_detail(ctx):
    ctx.update(source="cnpq", direct=DirectSession(CNPQ_PAGES, missing={CHAMADA_URL}),
               jina=JinaSession(CNPQ_PAGES))


@given("www.gov.br refuses the connection and Jina is rate limited")
def cnpq_jina_limited(ctx):
    ctx.update(source="cnpq", direct=DirectSession(CNPQ_PAGES, refuse=True),
               jina=JinaSession(CNPQ_PAGES, script=[FakeResponse(429)] * 10))


@given("www.gov.br answers an empty CNPq listing directly")
def cnpq_empty(ctx):
    ctx.update(source="cnpq", direct=DirectSession({CNPQ_ABERTAS_URL: "<html><body></body></html>"}),
               jina=JinaSession({}))


@given("www.gov.br refuses the connection and Jina asks to wait 5 seconds once")
def cnpq_jina_wait(ctx):
    ctx.update(source="cnpq", direct=DirectSession(CNPQ_PAGES, refuse=True),
               jina=JinaSession(CNPQ_PAGES, script=[FakeResponse(429, headers={"Retry-After": "5"})]))


@given("an edital whose PDF arrived as text")
def pdf_as_text(ctx):
    received = []
    service = SimpleNamespace(
        extract_from_text=lambda text, filename: received.append(text) or None,
        extract_from_pdf=lambda content, filename: pytest.fail("não devia ler bytes"),
        categorize_finep_by_description=lambda d: "",
    )
    ctx.update(service=service, received=received, raw=RawEdital(
        title="Edital CAPES 2026", url=CAPES_DETAIL_URL, raw_agency="CAPES",
        pdf_text=strip_jina_header(JINA_PDF_TEXT), document_type="edital",
    ))


# ---- When ------------------------------------------------------------------


def _read(ctx):
    if ctx["source"] == "cnpq":
        source, raws, clock = read_cnpq(ctx["direct"], ctx["jina"])
        ctx["clock"] = clock
    else:
        source, raws = read_capes(ctx["direct"], ctx["jina"])
    ctx["fetcher"] = source.fetcher
    return raws


@when("the CNPq source reads the calls")
@when("the CAPES source reads the calls")
def read_calls(ctx):
    ctx["first"] = _read(ctx)


@when("www.gov.br refuses the connection and the CNPq source reads the calls again")
@when("www.gov.br refuses the connection and the CAPES source reads the calls again")
def read_again_refused(ctx):
    ctx["direct"] = DirectSession(ctx["direct"].pages, refuse=True)
    ctx["second"] = _read(ctx)


@when(parsers.parse("the normalizer processes it on {today}"))
def normalize(ctx, today):
    from src.components.transforms.extraction_prompts import DocumentReadError

    try:
        EditalNormalizer(extraction_service=ctx["service"], today=date.fromisoformat(today)).process(ctx["raw"])
    except DocumentReadError:
        pass  # a IA falsa não devolve edital; aqui só importa o texto que ela recebeu


@when("the CNPq flow runs and the runner reads its stats")
def run_flow(ctx, capsys, tmp_path, monkeypatch):
    from src.flows import ingest_cnpq_flow

    monkeypatch.setattr(ingest_cnpq_flow.rejection_store, "get_active_keys", lambda s: set())
    monkeypatch.setattr(ingest_cnpq_flow.rejection_store, "record", lambda s, r: None)
    f, _ = fetcher(ctx["direct"], ctx["jina"], name="CNPq")
    capsys.readouterr()
    ingest_cnpq_flow.run_pipeline(
        source=CnpqSource(current_year=2026, fetcher=f),
        transform=SimpleNamespace(process=lambda raw: None, rejections={}, deferred=[]),
        sink=SimpleNamespace(write=lambda items: {}),
        processed_index_path=str(tmp_path / "processed.json"),
    )
    stats = parse_flow_stats(capsys.readouterr().out)
    ctx["result"], ctx["note"] = run_all_flows.build_observations(
        "CNPQ", {"cnpq": 0}, {"cnpq": 0}, 0, [], 0, stats=stats
    )


# ---- Then ------------------------------------------------------------------


@then("both reads give the same editais")
def same_editais(ctx):
    assert ctx["first"] and comparable(ctx["first"]) == comparable(ctx["second"])


@then("the read through Jina carries the PDF as text")
def pdf_as_text_from_jina(ctx):
    first, second = ctx["first"][0], ctx["second"][0]
    if first.raw_agency == "CAPES":
        # Spec 010: a CAPES manda sempre texto (edital + página); pelo Jina, o
        # PDF já chega como texto e entra no mesmo lugar.
        assert first.pdf_content is None and PDF_BYTES.decode("latin-1") in first.pdf_text
        assert "# COORDENAÇÃO" in second.pdf_text
    else:
        assert first.pdf_content == PDF_BYTES and first.pdf_text is None
        assert second.pdf_text.startswith("# COORDENAÇÃO")
    assert second.pdf_content is None
    assert "URL Source:" not in second.pdf_text


@then("the direct connection was tried only once")
def tried_once(ctx):
    assert len(ctx["direct"].calls) == 1
    assert ctx["fetcher"].mode == "jina"
    assert ctx["fetcher"].jina_pages == len(ctx["jina"].calls)


@then("the AI received that text")
def ai_received(ctx):
    assert ctx["received"] == [ctx["raw"].pdf_text]


@then("Jina was not called")
def jina_not_called(ctx):
    assert ctx["jina"].calls == []
    assert ctx["fetcher"].mode == "direct"


@then("no call was read")
def no_call(ctx):
    assert ctx["first"] == []


@then(parsers.parse('the result is "{result}"'))
def result_is(ctx, result):
    assert ctx["result"] == result


@then(parsers.parse('the note says "{text}"'))
def note_says(ctx, text):
    assert text in ctx["note"]


@then(parsers.parse('the note does not say "{text}"'))
def note_not(ctx, text):
    assert text not in ctx["note"]


@then("the CNPq call was read")
def cnpq_read(ctx):
    assert [r.url for r in ctx["first"]] == [CHAMADA_URL]


@then(parsers.parse("the fetcher waited {seconds:d} seconds"))
def waited(ctx, seconds):
    assert float(seconds) in ctx["clock"].sleeps


# ---- Unitários do fetcher ----------------------------------------------------


class TestGovBrFetcher:
    def test_direct_ok_does_not_touch_jina(self):
        f, _ = fetcher(DirectSession(CNPQ_PAGES), JinaSession(CNPQ_PAGES))
        assert "Chamada CNPq" in f.get_html(CNPQ_ABERTAS_URL)
        assert f.mode == "direct" and f.jina_pages == 0

    def test_http_error_is_not_a_block(self):
        f, _ = fetcher(DirectSession(CNPQ_PAGES, missing={CHAMADA_URL}), JinaSession(CNPQ_PAGES))
        with pytest.raises(requests.HTTPError):
            f.get_html(CHAMADA_URL)
        assert f.mode == "direct"

    def test_reset_switches_to_jina_and_asks_for_html(self):
        jina = JinaSession(CNPQ_PAGES)
        f, _ = fetcher(DirectSession(CNPQ_PAGES, refuse=True), jina)
        assert "Chamada CNPq" in f.get_html(CNPQ_ABERTAS_URL, listing=True)
        assert f.mode == "jina" and f.jina_pages == 1
        assert jina.calls[0]["headers"]["X-Return-Format"] == "html"

    def test_interval_between_jina_calls(self):
        f, clock = fetcher(DirectSession(CNPQ_PAGES, refuse=True), JinaSession(CNPQ_PAGES))
        f.get_html(CNPQ_ABERTAS_URL)
        f.get_html(CHAMADA_URL)
        assert clock.sleeps == [4.0]

    def test_persistent_429_fails_the_listing_with_a_reason(self):
        f, clock = fetcher(DirectSession({}, refuse=True), JinaSession({}, script=[FakeResponse(429)] * 10))
        with pytest.raises(GovBrUnavailable) as raised:
            f.get_html(CNPQ_ABERTAS_URL, listing=True)
        assert raised.value.reason == "jina_429"
        assert f.mode == "failed" and f.failure_reason == "jina_429"
        assert sum(s for s in clock.sleeps if s != 4.0) <= f.max_total_wait

    def test_persistent_429_on_a_detail_keeps_the_detour(self):
        f, _ = fetcher(DirectSession({}, refuse=True), JinaSession({}, script=[FakeResponse(429)] * 10))
        with pytest.raises(GovBrUnavailable):
            f.get_html(CHAMADA_URL)
        assert f.mode == "jina"

    def test_page_without_html_is_invalid(self):
        f, _ = fetcher(DirectSession({}, refuse=True), JinaSession({}, script=[FakeResponse(text="erro")]))
        with pytest.raises(GovBrUnavailable) as raised:
            f.get_html(CNPQ_ABERTAS_URL)
        assert raised.value.reason == "jina_vazio"

    def test_document_comes_as_text_without_the_jina_header(self):
        f, _ = fetcher(DirectSession({}, refuse=True), JinaSession({}))
        document = f.get_document(CNPQ_PDF_URL)
        assert document.content is None
        assert document.text.startswith("# COORDENAÇÃO")

    def test_scanned_document_is_empty_text_not_a_network_failure(self):
        header_only = JINA_PDF_TEXT.split("Markdown Content:")[0] + "Markdown Content:\n"
        f, _ = fetcher(DirectSession({}, refuse=True), JinaSession({}, script=[FakeResponse(text=header_only)]))
        document = f.get_document(CNPQ_PDF_URL)
        assert document.text == "" and document.content is None
        assert f.mode == "jina" and f.failure_reason is None

    def test_client_error_from_jina_is_not_retried(self):
        jina = JinaSession({}, script=[FakeResponse(422)])
        f, _ = fetcher(DirectSession({}, refuse=True), jina)
        with pytest.raises(GovBrUnavailable) as raised:
            f.get_document(CNPQ_PDF_URL)
        assert raised.value.reason == "jina_http_422"
        assert len(jina.calls) == 1

    def test_optional_key_goes_in_the_header_and_never_in_logs(self, monkeypatch, caplog):
        monkeypatch.setenv("JINA_API_KEY", FAKE_KEY)
        jina = JinaSession({}, script=[FakeResponse(429), FakeResponse(text="<html>ok</html>")])
        f, _ = fetcher(DirectSession({}, refuse=True), jina)
        f.get_html(CNPQ_ABERTAS_URL)
        assert jina.calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
        assert caplog.text and FAKE_KEY not in caplog.text

    def test_without_key_there_is_no_authorization_header(self, monkeypatch):
        monkeypatch.delenv("JINA_API_KEY", raising=False)
        jina = JinaSession(CNPQ_PAGES)
        f, _ = fetcher(DirectSession({}, refuse=True), jina)
        f.get_html(CNPQ_ABERTAS_URL)
        assert "Authorization" not in jina.calls[0]["headers"]


class TestNetworkStats:
    def test_emit_and_parse_network_fields(self, capsys):
        from src.flow_health import emit_flow_stats

        emit_flow_stats(raw_count=8, new_count=1, network="jina", jina_pages=12)
        emit_flow_stats(raw_count=0, new_count=0, network="failed", network_reason="jina_429")
        emit_flow_stats(raw_count=8, new_count=1, network="direct")
        jina, failed, direct = capsys.readouterr().out.strip().splitlines()
        assert jina.endswith("network=jina jina_pages=12")
        assert failed.endswith("network=failed network_reason=jina_429")
        assert "network" not in direct
        stats = parse_flow_stats(failed)
        assert (stats.network, stats.network_reason) == ("failed", "jina_429")
        assert parse_flow_stats(jina).jina_pages == 12

    def test_failure_still_wins_over_network(self):
        from src.flow_health import FlowStats

        stats = FlowStats(0, 0, network="failed", network_reason="jina_429")
        assert run_all_flows.resolve_result("CNPQ", 1, 0, 0, stats) == run_all_flows.RESULT_FAILURE
