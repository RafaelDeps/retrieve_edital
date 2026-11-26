"""
Indicações de editais pela comunidade (spec 009; portal: RafaelDeps/portal_edital#10, #15).

O formulário "Contribua" do portal abre issues "[Indicação] …". O coletor as lê,
recusa o que não é fomento (a primeira indicação real foi um concurso público do
Banestes), publica o resto e responde na issue. Tudo falso aqui: issues, links e
IA; índices e saída em tmp_path.
"""

import functools
import json
from datetime import date
from pathlib import Path

import pytest
import requests
from pytest_bdd import given, parsers, scenarios, then, when

from src import rejection_store
from src.components.sinks.json_sink import LocalJSONSink
from src.components.sources.comunidade_source import (
    ComunidadeSource,
    is_indicacao,
    parse_indicacao,
)
from src.components.sources.github_issues import IndicacoesClient
from src.components.transforms import theme_filter
from src.components.transforms.edital_normalizer import EditalNormalizer
from src.components.transforms.extraction_prompts import (
    OffTopicError,
    build_extraction_prompt,
    map_to_domain,
    theme_verdict,
)
from src.domain.models import EditalDomain, RawEdital
from src.flows import ingest_comunidade_flow

scenarios("../../docs/features/indicacoes_comunidade.feature")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "comunidade"
TODAY = date(2026, 11, 26)


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def issue(number, link, orgao="FAPES", prazo="_No response_", title=None, labels=()):
    body = (
        f"### Link oficial do edital\n{link}\n\n### Órgão de fomento / Instituição\n{orgao}\n\n"
        f"### Prazo de encerramento\n{prazo}\n\n### Observações\n_No response_"
    )
    return {"number": number, "title": title or f"[Indicação] {orgao}", "body": body,
            "labels": [{"name": n} for n in labels], "state": "OPEN"}


def edital_data(nome="Edital FAPES de Pesquisa 2026", orgao="FAPES", deadline="2026-12-15"):
    cronograma = [{"evento": "Encerramento da submissão", "data": deadline}] if deadline else []
    return {"nome": nome, "descrição": "Apoio a projetos de pesquisa científica.",
            "orgão_fomento": orgao, "categoria": "pesquisa", "status": "aberto",
            "data_abertura": "", "data_encerramento": deadline or "", "cronograma": cronograma,
            "tags": ["pesquisa", "fapes", "edital"]}


class FakeIssues:
    def __init__(self, issues, can_reply=True, fail=False):
        self.issues, self.can_reply, self.fail = issues, can_reply, fail
        self.comments, self.closed = [], []

    def list_open(self):
        return list(self.issues)

    def comment(self, number, text):
        if self.fail:
            raise requests.HTTPError("HTTP 403")
        self.comments.append((number, text))

    def close(self, number):
        self.closed.append(number)


class FakeAI:
    def __init__(self, data=None, off_topic=None):
        self.data, self.off_topic, self.calls = data, off_topic, []

    def _answer(self, check_theme):
        self.calls.append(check_theme)
        if self.off_topic:
            raise OffTopicError(self.off_topic)
        return map_to_domain(self.data)

    def extract_from_pdf(self, pdf_bytes, filename, check_theme=False):
        return self._answer(check_theme)

    def extract_from_text(self, text, filename, check_theme=False):
        return self._answer(check_theme)

    def categorize_finep_by_description(self, description):
        return ""

    def classify_document_titles(self, titles):
        return {}


class Env:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.output = tmp_path / "data" / "output"
        self.index = str(tmp_path / "processed.json")
        self.issues = []
        self.pages = {}
        self.down = set()
        self.known = set()
        self.ai = FakeAI(edital_data())
        self.fetched = []
        self.fail_reply = False
        rejected = str(tmp_path / "rejected.json")
        monkeypatch.setattr(rejection_store, "record",
                            functools.partial(rejection_store.record, path=rejected, today=TODAY))
        monkeypatch.setattr(rejection_store, "get_active_keys",
                            functools.partial(rejection_store.get_active_keys, path=rejected, today=TODAY))

    def fetch(self, url):
        self.fetched.append(url)
        if url in self.down:
            raise requests.ConnectionError("down")
        if url in self.pages:
            html = self.pages[url]
            title = html.split("<title>")[1].split("</title>")[0] if "<title>" in html else ""
            return None, html, title.replace("&uacute;", "ú")
        return b"%PDF-1.4 falso", None, ""

    def run(self, reply_token):
        self.client = FakeIssues(self.issues, can_reply=reply_token, fail=self.fail_reply)
        source = ComunidadeSource(client=self.client, fetch=self.fetch, known_links=lambda: set(self.known))
        self.result = ingest_comunidade_flow.run_pipeline(
            source=source,
            transform=EditalNormalizer(extraction_service=self.ai, today=TODAY),
            sink=LocalJSONSink(str(self.output)),
            client=self.client,
            processed_index_path=self.index,
        )

    def published(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in self.output.glob("*.json")] if self.output.is_dir() else []


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


# ---- Given -----------------------------------------------------------------


@given(parsers.parse('the portal has the suggestion "{name}" pointing to a funding edital'))
def funding(env, name):
    env.issues = [fixture(name)]


@given(parsers.parse('the portal has the suggestion "{name}" pointing to an edital without agency or deadline'))
def no_agency(env, name):
    env.issues = [fixture(name)]
    env.ai = FakeAI(edital_data(orgao="", deadline=None))


@given(parsers.parse('the portal has the suggestion "{name}" pointing to an expired funding edital'))
def expired(env, name):
    env.issues = [fixture(name)]
    env.ai = FakeAI(edital_data(deadline="2026-10-18"))


@given(parsers.parse('the portal has the real suggestion "{name}" pointing to the Banestes page'))
def banestes(env, name):
    env.issues = [fixture(name)]
    link = parse_indicacao(env.issues[0]["body"])["link"]
    env.pages[link] = (FIXTURES / "pagina_banestes.html").read_text(encoding="utf-8")


@given(parsers.parse('the portal has the suggestion "{name}" pointing to an edital the AI finds off topic'))
def off_topic(env, name):
    env.issues = [fixture(name)]
    env.ai = FakeAI(off_topic="premiação cultural sem relação com ciência")


@given(parsers.parse('the portal has the suggestion "{name}" pointing to an edital already in the portal'))
def already(env, name):
    env.issues = [fixture(name)]
    env.known.add(parse_indicacao(env.issues[0]["body"])["link"])


@given(parsers.parse("the portal has {count:d} funding suggestions"))
def many(env, count):
    env.issues = [issue(200 + i, f"https://exemplo.org/edital-{i}.pdf") for i in range(count)]


@given("the portal has a suggestion without a link and one with a link that is down")
def invalid_and_down(env):
    env.issues = [issue(103, "não sei o link"), issue(104, "https://fora-do-ar.org/edital.pdf")]
    env.down.add("https://fora-do-ar.org/edital.pdf")


@given("the portal has only a regular issue")
def regular(env):
    env.issues = [issue(14, "https://exemplo.org/x.pdf", title="Editais encerrados passam para data/arquivo/")]


@given("answering on the portal fails")
def failing_reply(env):
    env.fail_reply = True


# ---- When ------------------------------------------------------------------


@when(parsers.parse("the community flow runs on {day} with the reply token"))
def run_with_token(env, day):
    env.run(reply_token=True)


@when(parsers.parse("the community flow runs on {day} without the reply token"))
def run_without_token(env, day):
    env.run(reply_token=False)


# ---- Then ------------------------------------------------------------------


@then('the edital is published with source "comunidade" and the community tag')
def published(env):
    [edital] = env.published()
    assert edital["fonte_key"] == "comunidade"
    assert "indicação da comunidade" in edital["tags"]


@then(parsers.parse('suggestion {number:d} is answered with "{text}" and closed'))
def answered(env, number, text):
    bodies = [body for n, body in env.client.comments if n == number]
    assert bodies and bodies[0].startswith(text), env.client.comments
    assert bodies[0].endswith("(resposta automática)")
    assert number in env.client.closed


@then(parsers.parse('the published edital has the agency "{agency}" and the deadline {deadline} from the form'))
def form_fallback(env, agency, deadline):
    [edital] = env.published()
    assert edital["orgão_fomento"] == agency
    assert edital["data_encerramento"] == deadline
    assert any("prazo informado na indicação" in e["evento"] for e in edital["cronograma"])


@then("nothing is published")
def nothing(env):
    assert env.published() == []


@then("the AI was not called")
def ai_not_called(env):
    assert env.ai.calls == []


@then("the AI was called once")
def ai_called_once(env):
    assert env.ai.calls == [True]


@then("the link was not fetched")
def not_fetched(env):
    assert env.fetched == []


@then(parsers.parse("{count:d} links were fetched"))
def fetched_count(env, count):
    assert len(env.fetched) == count


@then("no suggestion was answered")
def none_answered(env):
    assert env.client.comments == [] and env.client.closed == []


# ---- Unitários ---------------------------------------------------------------


class TestThemeFilter:
    def test_banestes_title(self):
        assert theme_filter.off_topic_term("Concurso Público Banestes 2026") == "concurso público"

    def test_fellowship_selection_is_not_blocked(self):
        assert theme_filter.off_topic_term("Seleção de bolsistas de iniciação científica — processo seletivo") is None

    def test_pregao(self):
        assert theme_filter.off_topic_term("Pregão Eletrônico nº 5/2026") == "pregão eletrônico"

    def test_term_after_the_limit_is_ignored(self):
        assert theme_filter.off_topic_term("x" * 2100 + " concurso público") is None


class TestParsing:
    def test_real_banestes_body(self):
        fields = parse_indicacao(fixture("issue_banestes")["body"])
        assert fields["link"] == "https://www.banestes.com.br/publicacoes_legais/concurso2026.htm"
        assert fields["orgao"] == "banesntes" and fields["prazo"] == "18/10/2026"

    def test_no_response_becomes_empty(self):
        fields = parse_indicacao(fixture("issue_sem_resposta")["body"])
        assert fields["prazo"] == "" and fields["observacoes"] == ""

    def test_label_or_title_marks_a_suggestion(self):
        assert is_indicacao({"title": "[Indicação] x", "labels": []})
        assert is_indicacao({"title": "qualquer", "labels": [{"name": "indicacao-edital"}]})
        assert not is_indicacao({"title": "Editais encerrados", "labels": []})


class TestThemeExtraction:
    def test_prompt_without_flag_is_unchanged(self):
        assert build_extraction_prompt("T") == build_extraction_prompt("T", check_theme=False)
        assert "e_fomento" not in build_extraction_prompt("T")

    def test_prompt_with_flag_asks_the_theme(self):
        assert "e_fomento" in build_extraction_prompt("T", check_theme=True)

    def test_theme_verdict(self):
        assert theme_verdict({"tema": {"e_fomento": False, "motivo": "concurso"}}) == (False, "concurso")
        assert theme_verdict({}) == (True, "")
        assert theme_verdict({"tema": "?"}) == (True, "")

    def test_gemini_raises_off_topic(self):
        from types import SimpleNamespace

        from src.components.transforms.gemini_client import GeminiExtractionService

        answer = json.dumps({**edital_data(), "tema": {"e_fomento": False, "motivo": "concurso"}})
        client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **k: SimpleNamespace(text=answer)))
        service = GeminiExtractionService(client=client, sleep=lambda s: None)
        with pytest.raises(OffTopicError) as raised:
            service.extract_from_text("texto do edital", "x.pdf", check_theme=True)
        assert raised.value.motivo == "concurso"
        assert service.extract_from_text("texto do edital", "x.pdf") is not None


class TestFonteKey:
    def test_declared_fonte_key_wins_over_agency_profile(self):
        from src.components.transforms import publication_rules

        edital = map_to_domain(edital_data())
        raw = RawEdital(title="t", url="u", raw_fonte_key="comunidade")
        assert publication_rules.resolve_fonte_key(edital, raw) == "comunidade"
        assert publication_rules.resolve_fonte_key(edital, RawEdital(title="t", url="u")) == "fapes"


class TestClientNeverLogsTheToken:
    def test_reply_failure_does_not_leak_the_token(self, monkeypatch, caplog):
        from types import SimpleNamespace

        monkeypatch.setenv("PORTAL_ISSUES_TOKEN", "SEGREDO-PORTAL")
        session = SimpleNamespace(post=lambda *a, **k: SimpleNamespace(status_code=403))
        client = IndicacoesClient(repo="x/y", session=session)
        source = SimpleNamespace(issues_by_link={"u": 7}, results={"u": ("ja_no_portal", "")}, invalid_issues=[])
        ingest_comunidade_flow.reply_all(source, ingest_comunidade_flow.PipelineResult(), client)
        assert "HTTP 403" in caplog.text
        assert "SEGREDO-PORTAL" not in caplog.text
