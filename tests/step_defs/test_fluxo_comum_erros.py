"""
Erros registrados e roteiro comum dos fluxos (RafaelDeps/retrieve_edital#10, spec 007).

Um erro inesperado na transformação ficava só numa linha de log: o item voltava
em toda execução e o fluxo aparecia como Sucesso. Agora ele vai para o índice de
recusados por 3 dias, entra no `[flow-stats]` e o runner marca Atenção. Os 7
fluxos comuns passaram a usar um roteiro só (`src/flows/common.py`).
"""

import functools
import pathlib
from datetime import date
from types import SimpleNamespace

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from scripts import run_all_flows
from src import rejection_store
from src.components.transforms.extraction_service import AIUnavailableError
from src.domain.models import EditalDomain, RawEdital
from src.flow_health import parse_flow_stats
from src.flows import common
from src.flows import ingest_conif_flow

scenarios("../../docs/features/fluxo_comum_erros.feature")

URL_A = "https://portal.conif.org.br/editais/edital-a"
URL_B = "https://portal.conif.org.br/editais/edital-b"


def raw(url, title):
    return RawEdital(title=title, url=url)


def domain(url):
    return EditalDomain(
        nome="EDITAL B", descrição="d", orgão_fomento="CONIF", categoria="pesquisa",
        status="aberto", data_abertura="", data_encerramento="2027-01-01", link=url,
        cronograma=[], tags=[],
    )


class FakeTransform:
    def __init__(self, behaviour):
        self.behaviour = behaviour  # url -> "error" | "deferred" | "valid"
        self.rejections = {}
        self.deferred = []

    def process(self, item):
        kind = self.behaviour[item.url]
        if kind == "error":
            raise ValueError("título vazio")
        if kind == "deferred":
            self.deferred.append(item.url)
            return None
        return domain(item.url)


class Env:
    def __init__(self, tmp_path, monkeypatch, capsys):
        self.tmp_path, self.monkeypatch, self.capsys = tmp_path, monkeypatch, capsys
        self.rejected_path = str(tmp_path / "rejected.json")
        self.processed_path = str(tmp_path / "processed.json")
        self.behaviour = {}
        self.written = {}
        self.seen_by_source = []
        self.output = ""
        self._record = rejection_store.record
        self._active = rejection_store.get_active_keys

    def use_day(self, day):
        self.monkeypatch.setattr(
            rejection_store, "record",
            functools.partial(self._record, path=self.rejected_path, today=day),
        )
        self.monkeypatch.setattr(
            rejection_store, "get_active_keys",
            functools.partial(self._active, path=self.rejected_path, today=day),
        )

    def run(self, day):
        self.use_day(day)
        items = [raw(url, f"Edital {url[-1].upper()}") for url in self.behaviour]

        def make_source(seen):
            self.seen_by_source.append(set(seen))
            return SimpleNamespace(read=lambda: list(items), last_listing_count=len(items))

        def write(valid):
            persisted = {d.link: d for d in valid}
            self.written.update(persisted)
            return persisted

        self.capsys.readouterr()
        # A configuração real do CONIF, com a fonte padrão trocada por uma falsa.
        common.run_standard_pipeline(
            ingest_conif_flow.SPEC,
            make_source,
            transform=FakeTransform(self.behaviour),
            sink=SimpleNamespace(write=write),
            processed_index_path=self.processed_path,
        )
        self.output = self.capsys.readouterr().out

    def rejected(self):
        return (rejection_store._load(self.rejected_path).get("conif") or {})

    def runner(self):
        stats = parse_flow_stats(self.output)
        return run_all_flows.build_observations("CONIF", {"conif": 0}, {"conif": 0}, 0, [], 0, stats=stats)


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    return Env(tmp_path, monkeypatch, capsys)


# ---- Given -----------------------------------------------------------------


@given("a CONIF batch where item A fails with ValueError and item B is valid")
def batch_error(env):
    env.behaviour = {URL_A: "error", URL_B: "valid"}


@given("a CONIF batch where item A is deferred and item B is valid")
def batch_deferred(env):
    env.behaviour = {URL_A: "deferred", URL_B: "valid"}


@given("a CONIF batch where items A and B fail with ValueError")
def batch_all_fail(env):
    env.behaviour = {URL_A: "error", URL_B: "error"}


@given(parsers.parse("the transform error validity is {days:d} days"))
def ttl(monkeypatch, days):
    monkeypatch.setenv(common.TRANSFORM_ERROR_TTL_ENV, str(days))


# ---- When ------------------------------------------------------------------


@when(parsers.parse("the CONIF flow runs on {day}"))
def run_once(env, day):
    env.run(date.fromisoformat(day))


@when(parsers.parse("the CONIF flow runs again on {day}"))
def run_again(env, day):
    env.run(date.fromisoformat(day))


# ---- Then ------------------------------------------------------------------


@then("item B is written")
def b_written(env):
    assert list(env.written) == [URL_B]


@then(parsers.parse('item A is in the rejected index as "{reason}" until {until}'))
def a_rejected(env, reason, until):
    entry = env.rejected()[URL_A]
    assert entry["motivo"] == reason
    assert entry["valida_ate"] == until


@then("item A is not in the rejected index")
def a_not_rejected(env):
    assert URL_A not in env.rejected()


@then(parsers.parse('the flow stats report "{text}"'))
def stats_report(env, text):
    assert text in env.output


@then("the flow stats do not report errors")
def no_errors(env):
    assert "errors=" not in env.output


@then(parsers.parse('the runner result is "{result}"'))
def runner_result(env, result):
    assert env.runner()[0] == result


@then(parsers.parse('the runner note says "{text}"'))
def runner_note(env, text):
    assert text in env.runner()[1]


@then(parsers.parse('the runner note does not say "{text}"'))
def runner_note_not(env, text):
    assert text not in env.runner()[1]


@then("the source of the second run already knew item A")
def knew_a(env):
    assert URL_A in env.seen_by_source[1]


@then("the source of the second run did not know item A")
def did_not_know_a(env):
    assert URL_A not in env.seen_by_source[1]


# ---- Unitários ---------------------------------------------------------------


class TestTransformAll:
    def test_keeps_batch_order_and_ignores_none(self):
        items = [raw(f"u{i}", f"t{i}") for i in range(6)]
        transform = SimpleNamespace(process=lambda item: None if item.url == "u3" else domain(item.url))
        outcome = common.transform_all(transform, items)
        assert [d.link for d in outcome.valid] == ["u0", "u1", "u2", "u4", "u5"]
        assert outcome.errors == []

    def test_exception_becomes_typed_error(self, caplog):
        def process(item):
            raise KeyError("x")

        outcome = common.transform_all(SimpleNamespace(process=process), [raw("u1", "Título")])
        assert outcome.errors == [common.TransformError("u1", "Título", "KeyError", "'x'")]
        assert "Failed to transform item Título" in caplog.text

    def test_ai_unavailable_is_not_an_error(self):
        def process(item):
            raise AIUnavailableError("cota diária esgotada")

        outcome = common.transform_all(SimpleNamespace(process=process), [raw("u1", "t")])
        assert outcome.errors == [] and outcome.valid == []

    def test_error_without_url_is_counted_but_not_recorded(self, monkeypatch):
        calls = []
        monkeypatch.setattr(rejection_store, "record", lambda *a, **k: calls.append(a) or 1)
        errors = [common.TransformError("", "t", "ValueError", "m")]
        assert common.record_transform_errors("conif", errors) == 0
        assert calls == []
        assert common.error_stats(errors) == {"errors": 1, "error_types": "ValueError:1"}

    def test_error_types_are_ordered_by_count_then_name(self):
        errors = [common.TransformError(f"u{i}", "t", kind, "m")
                  for i, kind in enumerate(["ValueError", "KeyError", "ValueError", "AttributeError"])]
        assert common.error_stats(errors)["error_types"] == "ValueError:2,AttributeError:1,KeyError:1"

    @pytest.mark.parametrize("value, expected", [(None, 3), ("5", 5), ("0", 0), ("abc", 3), ("-2", 3)])
    def test_ttl(self, monkeypatch, value, expected):
        if value is None:
            monkeypatch.delenv(common.TRANSFORM_ERROR_TTL_ENV, raising=False)
        else:
            monkeypatch.setenv(common.TRANSFORM_ERROR_TTL_ENV, value)
        assert common.transform_error_ttl_days() == expected


class TestNoDuplicatedFlowCode:
    COMMON_FLOWS = ("fapes", "finep", "conif", "prppg_ifes", "proex_ifes", "capes", "cnpq")

    @pytest.mark.parametrize("name", COMMON_FLOWS)
    def test_common_flows_use_the_shared_pipeline(self, name):
        code = pathlib.Path(f"src/flows/ingest_{name}_flow.py").read_text(encoding="utf-8")
        assert "run_standard_pipeline(" in code
        assert "ThreadPoolExecutor" not in code
        assert "emit_flow_stats(" not in code

    def test_horizon_uses_the_shared_transform_step(self):
        code = pathlib.Path("src/flows/ingest_horizon_flow.py").read_text(encoding="utf-8")
        assert "transform_all(" in code
        assert "ThreadPoolExecutor" not in code


class TestErrorStatsLine:
    def test_emit_and_parse_errors_with_every_field(self, capsys):
        from src.flow_health import FlowStats, emit_flow_stats

        emit_flow_stats(raw_count=10, new_count=2, deferred_count=1, errors=3,
                        error_types="ValueError:2,KeyError:1", created=2, updated=1,
                        unchanged=0, closed=0, closing_blocked=True, network="jina", jina_pages=4)
        line = capsys.readouterr().out.strip()
        assert "deferred=1 errors=3 error_types=ValueError:2,KeyError:1 created=2" in line
        stats = parse_flow_stats(line)
        assert stats == FlowStats(raw_count=10, new_count=2, deferred_count=1, errors=3,
                                  error_types="ValueError:2,KeyError:1", created=2, updated=1,
                                  unchanged=0, closed=0, closing_blocked=True, network="jina",
                                  jina_pages=4)

    def test_failure_still_wins_over_errors(self):
        from src.flow_health import FlowStats

        stats = FlowStats(10, 2, errors=1, error_types="ValueError:1")
        assert run_all_flows.resolve_result("CONIF", 1, 0, 0, stats) == run_all_flows.RESULT_FAILURE
