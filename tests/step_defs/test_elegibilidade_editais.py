"""
Elegibilidade dos editais para recomendação (spec 011, issue #12).

IA falsa (extração completa e leitura curta) e saídas em tmp_path. Nada abre rede.
"""

import json
from datetime import date

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from scripts import run_all_flows
from src.components.transforms.edital_normalizer import EditalNormalizer
from src.components.transforms.extraction_prompts import map_to_domain, parse_eligibility_response
from src.components.transforms.extraction_service import AIUnavailableError
from src.domain.models import RawEdital
from src.flow_health import parse_flow_stats

scenarios("../../docs/features/elegibilidade_editais.feature")

HORIZON_URL = "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/opportunities/topic-details/HORIZON-X"
FINEP_URL = "https://www.finep.gov.br/e/chamada-publica/222684/1"


class FakeAI:
    def __init__(self):
        self.extractions = 0
        self.short_reads = 0
        self.categorized = 0
        self.response = {"nome": "Edital", "cronograma": [{"evento": "Encerramento", "data": "2026-12-20"}]}
        self.short = {}
        self.unavailable = False
        self.short_texts = []

    def extract_from_text(self, text, filename, check_theme=False):
        self.extractions += 1
        return map_to_domain(dict(self.response), text)

    def extract_from_pdf(self, content, filename, check_theme=False):
        pytest.fail("os cenários mandam texto")

    def read_eligibility(self, text, filename, finep=False):
        self.short_reads += 1
        self.short_texts.append(text)
        if self.unavailable:
            raise AIUnavailableError("cota diária")
        return parse_eligibility_response(dict(self.short), finep)

    def categorize_finep_by_description(self, description):
        self.categorized += 1
        return "extensão"


@pytest.fixture
def ctx():
    return {"ai": FakeAI()}


def _split(value):
    return [v.strip() for v in value.split(",") if v.strip()]


def _doc_raw():
    return RawEdital(title="Edital CNPq 25/2026", url="https://www.gov.br/cnpq/edital-25",
                     raw_agency="CNPq", pdf_text="Texto do edital.")


# ---- Given -----------------------------------------------------------------


@given(parsers.re(
    r'an edital text read by the AI with "(?P<degree>[^"]*)", areas "(?P<areas>[^"]*)" and audience "(?P<audience>[^"]*)"'
))
def edital_read(ctx, degree, areas, audience):
    ctx["ai"].response.update({
        "elegibilidade": {
            "titulacao_minima": degree,
            # "Linguística, Letras e Artes" tem vírgula; os cenários não a usam.
            "areas_conhecimento": _split(areas),
            "requisitos_resumo": "Coordenador doutor com vínculo em ICT.",
        },
        "publico_alvo": _split(audience),
    })
    ctx["raw"] = _doc_raw()


@given("an edital text read by the AI without eligibility fields")
def edital_without_fields(ctx):
    ctx["raw"] = _doc_raw()


@given("a PRPPG item without a document but with a deadline")
def prppg_item(ctx):
    ctx["raw"] = RawEdital(
        title="Edital PRPPG 05/2026", url="https://prppg.ifes.edu.br/edital-05", raw_agency="IFES",
        raw_description="Seleção de projetos de pesquisa.",
        raw_cronograma=[{"evento": "Encerramento das inscrições", "data": "2026-12-20"}],
    )


@given(parsers.re(
    r'a FINEP call with a deadline, the API audience "(?P<api>[^"]*)" and the short read audience "(?P<read>[^"]*)"'
))
def finep_call(ctx, api, read):
    ctx["ai"].short = {"publico_alvo": _split(read), "categoria": "inovação",
                       "elegibilidade": {"areas_conhecimento": ["Engenharias"]}}
    ctx["raw"] = RawEdital(
        title="FINEP Mais Inovação", url=FINEP_URL, raw_agency="FINEP",
        raw_description="Subvenção econômica à inovação para empresas.",
        source_category="chamada pública",
        raw_publico_alvo=(["ict", "empresa"] if api == "ict" else None),
        raw_cronograma=[{"evento": "Prazo para envio de propostas", "data": "2026-12-30"}],
    )


def _horizon_raw(known=None):
    return RawEdital(
        title="Advanced materials for batteries", url=HORIZON_URL, raw_agency="Horizon Europe",
        raw_description="Research and innovation action on battery materials.",
        raw_publico_alvo=["pesquisador"], raw_ambito_geografico="internacional",
        raw_cronograma=[{"evento": "Deadline", "data": "2027-02-10"}],
        known_elegibilidade=known,
    )


@given("a Horizon call without a saved eligibility")
def horizon_new(ctx):
    ctx["raw"] = _horizon_raw()


@given(parsers.parse('the short read answers areas "{areas}"'))
def short_answers(ctx, areas):
    ctx["ai"].short = {"elegibilidade": {"areas_conhecimento": _split(areas)}, "publico_alvo": ["pesquisador"]}


@given(parsers.parse('a Horizon call with a saved eligibility for areas "{areas}"'))
def horizon_saved(ctx, areas):
    ctx["raw"] = _horizon_raw({"titulacao_minima": "", "areas_conhecimento": [areas], "requisitos_resumo": ""})


@given("the AI has no quota left")
def no_quota(ctx):
    ctx["ai"].unavailable = True


@given(parsers.parse("a flow reported {n:d} items pending eligibility"))
def flow_reported(ctx, n):
    ctx["stats"] = parse_flow_stats(f"[flow-stats] raw=10 new=2 eligibility_pending={n}")


@given("the output has 3 open editais without eligibility, 1 with it and 1 Horizon without it")
def output_dir(ctx, tmp_path):
    rows = [
        ("a", "capes", "2026-12-30", None),
        ("b", "cnpq", "2026-12-05", None),
        ("c", "finep", "", None),
        ("d", "cnpq", "2026-12-01", {"titulacao_minima": "", "areas_conhecimento": [], "requisitos_resumo": ""}),
        ("e", "horizon", "2026-12-02", None),
    ]
    for name, fonte, closing, eleg in rows:
        (tmp_path / f"{name}.json").write_text(json.dumps({
            "link": f"https://x/{name}", "fonte_key": fonte, "status": "aberto",
            "data_encerramento": closing, "elegibilidade": eleg,
        }), encoding="utf-8")
    ctx["output"] = tmp_path


# ---- When ------------------------------------------------------------------


@when(parsers.parse("it is normalized on {today}"))
def normalized(ctx, today):
    ctx["normalizer"] = EditalNormalizer(extraction_service=ctx["ai"], today=date.fromisoformat(today))
    ctx["result"] = ctx["normalizer"].process(ctx["raw"])


@when("the runner reads the stats")
def runner_reads(ctx):
    ctx["observation"] = run_all_flows.build_observations(
        "HORIZON", {"horizon": 0}, {"horizon": 0}, 0, [], 0, stats=ctx["stats"]
    )


@when(parsers.parse("the runner selects the eligibility rereads with a cap of {cap:d}"))
def select(ctx, cap):
    ctx["selected"] = run_all_flows.select_eligibility_rereads(ctx["output"], cap)


# ---- Then ------------------------------------------------------------------


@then("the AI was called once")
def called_once(ctx):
    assert ctx["ai"].extractions == 1 and ctx["ai"].short_reads == 0


@then("the AI was not called")
def not_called(ctx):
    assert ctx["ai"].extractions == 0 and ctx["ai"].short_reads == 0


@then("the short read was called once")
def short_once(ctx):
    assert ctx["ai"].short_reads == 1 and ctx["ai"].extractions == 0
    # O título vai junto: no Horizon a descrição é só metadado (validação real).
    assert ctx["ai"].short_texts[0].startswith("Título: ")


@then("the short read was not called")
def short_not(ctx):
    assert ctx["ai"].short_reads == 0


@then("the FINEP category classifier was not called")
def no_categorize(ctx):
    assert ctx["ai"].categorized == 0


@then(parsers.re(r'the eligibility has degree "(?P<degree>[^"]*)" and areas "(?P<areas>[^"]*)"'))
def eligibility_has(ctx, degree, areas):
    eleg = ctx["result"].elegibilidade
    assert eleg["titulacao_minima"] == degree
    assert eleg["areas_conhecimento"] == _split(areas)


@then(parsers.parse("the eligibility has {n:d} areas"))
def nine_areas(ctx, n):
    assert len(ctx["result"].elegibilidade["areas_conhecimento"]) == n


@then("the eligibility is present and empty")
def present_empty(ctx):
    assert ctx["result"].elegibilidade == {"titulacao_minima": "", "areas_conhecimento": [], "requisitos_resumo": ""}


@then("the eligibility is null")
def is_null(ctx):
    assert ctx["result"].elegibilidade is None


@then(parsers.parse('the audience is "{audience}"'))
def audience(ctx, audience):
    assert ctx["result"].publico_alvo == _split(audience)


@then(parsers.parse('the requirements summary is "{summary}"'))
def summary(ctx, summary):
    assert ctx["result"].elegibilidade["requisitos_resumo"] == summary


@then(parsers.parse('the category is "{category}"'))
def category(ctx, category):
    assert ctx["result"].categoria == category


@then("the edital is published with a null eligibility")
def published_null(ctx):
    assert ctx["result"] is not None and ctx["result"].elegibilidade is None


@then("the item is pending eligibility")
def pending(ctx):
    assert ctx["normalizer"].eligibility_pending == [HORIZON_URL]


@then(parsers.parse('the result is Atenção with "{fragment}"'))
def warning(ctx, fragment):
    result, note = ctx["observation"]
    assert result == run_all_flows.RESULT_WARNING and fragment in note


@then("the 2 with the nearest deadline are selected and Horizon is not")
def nearest(ctx):
    assert ctx["selected"] == {"capes": ["https://x/a"], "cnpq": ["https://x/b"]}
