"""
Horizon Europe sempre atualizado (RafaelDeps/retrieve_edital#8, spec 005).

O fluxo pulava toda chamada já vista, então prazo adiado, abertura e
cancelamento nunca chegavam aos arquivos. Agora toda chamada relevante é relida
a cada execução. Os testes rodam o fluxo de verdade contra uma lista falsa, com
saída e índice em `tmp_path` e data fixa.

Lista de referência (depois da coleta de A, B, C e D):

| Chamada | Na lista nova                  | Esperado          |
|---------|--------------------------------|-------------------|
| A       | prazo adiado                   | alterada          |
| B       | igual                          | sem mudança       |
| C       | ausente (estava aberta)        | encerrada         |
| D       | título novo, mesmo identifier  | alterada, 1 arquivo |
| E       | nova                           | nova              |
"""

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from src.components import horizon_sync
from src.components.sinks.json_sink import LocalJSONSink
from src.components.sources.horizon_source import (
    HORIZON_TOPIC_URL_TEMPLATE,
    HorizonSource,
    parse_json_objects,
)
from src.components.transforms.edital_normalizer import EditalNormalizer
from src.domain.models import EditalDomain
from src.flows import ingest_horizon_flow
from tests.step_defs.test_horizon_source import FakeSession, build_item, build_payload

scenarios("../../docs/features/horizon_atualizacao.feature")

TODAY = date(2026, 11, 26)
DIVISIONS = ["HORIZON.3.1"]


def millis(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)


def link_of(identifier: str) -> str:
    return HORIZON_TOPIC_URL_TEMPLATE.format(identifier=identifier)


def call(key: str, title=None, deadline="2026-12-17", status="Open", opening="2026-11-01"):
    return build_item(
        identifier=f"HORIZON-TEST-{key}",
        title=title or f"Call {key}",
        callTitle=title or f"Call {key}",
        status={"abbreviation": status},
        deadlineDatesLong=[millis(deadline)],
        plannedOpeningDateLong=millis(opening),
    )


LAST_WEEK = [call("A"), call("B"), call("C"), call("D")]
REFERENCE = [
    call("A", deadline="2027-01-20"),
    call("B"),
    call("D", title="Call D renamed"),
    call("E"),
]


class HorizonEnv:
    def __init__(self, tmp_path: Path, monkeypatch, capsys):
        self.workdir = tmp_path
        self.output = tmp_path / "data" / "output"
        self.index = str(tmp_path / "processed.json")
        self.capsys = capsys
        self.rejected = {}
        self.active_rejections = set()
        self.consulted_rejections = False
        monkeypatch.setattr(ingest_horizon_flow.rejection_store, "record", self._record)
        monkeypatch.setattr(
            ingest_horizon_flow.rejection_store, "get_active_keys", self._active_keys
        )
        self.last_output = ""

    def _record(self, source, rejections):
        self.rejected.update(rejections)

    def _active_keys(self, source):
        self.consulted_rejections = True
        return set(self.active_rejections)

    def run(self, items=None, payload=None):
        payload = payload if payload is not None else build_payload(items)
        source = HorizonSource(divisions=DIVISIONS, session=FakeSession(payload))
        self.capsys.readouterr()
        ingest_horizon_flow.run_pipeline(
            source=source,
            transform=EditalNormalizer(today=TODAY),
            sink=LocalJSONSink(str(self.output)),
            processed_index_path=self.index,
        )
        self.last_output = self.capsys.readouterr().out

    def files(self):
        return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in self.output.glob("*.json")}

    def files_for(self, key):
        link = link_of(f"HORIZON-TEST-{key}")
        return {name: data for name, data in self.files().items() if data["link"] == link}

    def only(self, key):
        found = self.files_for(key)
        assert len(found) == 1, found.keys()
        return next(iter(found.items()))


@pytest.fixture
def env(tmp_path, monkeypatch, capsys):
    return HorizonEnv(tmp_path, monkeypatch, capsys)


@pytest.fixture
def before():
    return {}


# ---- Given ---------------------------------------------------------------


@given("the Horizon calls A, B, C and D were collected last week")
def collected_last_week(env, before):
    env.run(LAST_WEEK)
    before["files"] = env.files()


@given("call F was collected while forthcoming")
def f_forthcoming(env, before):
    env.run(LAST_WEEK + [call("F", status="Forthcoming", opening="2027-01-10", deadline="2027-03-01")])
    before["files"] = env.files()


@given("call G is in the active rejections")
def g_rejected(env):
    env.active_rejections.add(link_of("HORIZON-TEST-G"))


# ---- When ----------------------------------------------------------------


@when(parsers.parse("the Horizon list now has call A with the deadline {deadline}"))
def list_a_postponed(env, deadline):
    env.run([call("A", deadline=deadline), call("B"), call("C"), call("D")])


@when(parsers.parse("the Horizon list now has call F open since {opening}"))
def list_f_open(env, opening):
    env.run(LAST_WEEK + [call("F", status="Open", opening=opening, deadline="2027-03-01")])


@when("the Horizon list is the reference list")
def list_reference(env):
    env.run(REFERENCE)


@when("the Horizon list has A, B, C and D unchanged")
def list_unchanged(env):
    env.run(LAST_WEEK)


@when("the Horizon list is the reference list cut in the middle")
def list_truncated(env):
    env.run(payload=build_payload(REFERENCE)[:-40])


@when("the Horizon list has only call A")
def list_only_a(env):
    env.run([call("A")])


@when(parsers.parse("the Horizon list has call G with the deadline {deadline}"))
def list_g(env, deadline):
    env.run(LAST_WEEK + [call("G", deadline=deadline)])


@when(parsers.parse("the daily refresh runs on {today}"))
def daily_refresh(env, today):
    from scripts.curate_output import curate

    curate(env.workdir, apply=True, refresh_status_only=True, today=date.fromisoformat(today))


# ---- Then ----------------------------------------------------------------


@then(parsers.parse("call A has the closing date {closing} in the same file"))
def a_updated(env, before, closing):
    name, data = env.only("A")
    assert data["data_encerramento"] == closing
    assert name in before["files"]


@then(parsers.parse("call F has the opening date {opening}"))
def f_opened(env, opening):
    assert env.only("F")[1]["data_abertura"] == opening


@then("call B has the same content as before")
def b_unchanged(env, before):
    name, data = env.only("B")
    assert before["files"][name] == data


@then("call E is saved and indexed")
def e_saved(env):
    from src.processed_store import get_keys_set

    env.only("E")
    assert link_of("HORIZON-TEST-E") in get_keys_set("horizon", path=env.index)


@then("there is exactly one file for call D, with the new title")
def d_single(env):
    assert env.only("D")[1]["nome"] == "CALL D RENAMED"


@then("the set of file names is the same as before")
def same_names(env, before):
    assert set(env.files()) == set(before["files"])


@then("call C is closed and its file still exists")
def c_closed(env, before):
    name, data = env.only("C")
    assert data["status"] == "encerrado"
    assert {k: v for k, v in data.items() if k != "status"} == {
        k: v for k, v in before["files"][name].items() if k != "status"
    }


@then("call C is still closed")
def c_still_closed(env):
    assert env.only("C")[1]["status"] == "encerrado"


@then("call C is still open")
def c_open(env):
    assert env.only("C")[1]["status"] == "aberto"


@then("the flow reports that closing was blocked")
def closing_blocked(env):
    assert "closing_blocked=1" in env.last_output


@then("call G is saved")
def g_saved(env):
    env.only("G")


@then("the active rejections were not consulted")
def rejections_not_consulted(env):
    assert env.consulted_rejections is False


@then(parsers.parse("the flow stats report {counts}"))
def stats_report(env, counts):
    assert counts in env.last_output
    assert "new=1" in env.last_output


# ---- Regras puras ----------------------------------------------------------


def domain(link, nome="X", status="aberto"):
    return EditalDomain(
        nome=nome, descrição="d", orgão_fomento="Horizon Europe", categoria="pesquisa",
        status=status, data_abertura="", data_encerramento="", link=link,
        cronograma=[], tags=[],
    )


class TestHorizonSyncRules:
    def test_classify_changes(self):
        from dataclasses import asdict

        same, changed, new = domain("l1"), domain("l2", nome="novo"), domain("l3")
        saved = {"l1": ("a.json", asdict(same)), "l2": ("b.json", asdict(domain("l2")))}
        assert horizon_sync.classify_changes([same, changed, new], saved, {"l1", "l2"}) == {
            "l1": "sem_mudanca", "l2": "alterada", "l3": "nova",
        }

    def test_indexed_call_without_saved_file_counts_as_updated(self):
        assert horizon_sync.classify_changes([domain("l1")], {}, {"l1"}) == {"l1": "alterada"}

    def test_closing_guard(self):
        assert horizon_sync.closing_guard(False, 300, 300) == horizon_sync.REASON_INCOMPLETE
        assert "menos da metade" in horizon_sync.closing_guard(True, 149, 300)
        assert horizon_sync.closing_guard(True, 150, 300) is None
        assert horizon_sync.closing_guard(True, 0, 0) is None

    def test_links_to_close_only_open_and_missing(self):
        saved = {
            "aberta-fora": ("1.json", {"status": "aberto"}),
            "aberta-dentro": ("2.json", {"status": "aberto"}),
            "encerrada-fora": ("3.json", {"status": "encerrado"}),
        }
        assert horizon_sync.links_to_close(saved, {"aberta-dentro"}) == ["aberta-fora"]


class TestSourceReadFacts:
    def test_complete_read_lists_relevant_links(self):
        source = HorizonSource(divisions=DIVISIONS, session=FakeSession(build_payload(REFERENCE)))
        source.read()
        assert source.last_read_complete is True
        assert link_of("HORIZON-TEST-E") in source.last_relevant_links
        assert len(source.last_relevant_links) == 4

    def test_truncated_payload_is_incomplete(self):
        objects, complete = parse_json_objects(build_payload(REFERENCE)[:-40])
        assert complete is False
        assert len(objects) < len(REFERENCE)

    def test_network_error_is_incomplete(self):
        import requests

        source = HorizonSource(
            divisions=DIVISIONS, session=FakeSession(raise_error=requests.RequestException("x"))
        )
        assert source.read() == []
        assert source.last_read_complete is False
        assert source.last_relevant_links == set()


class TestSinkMaintenance:
    def write(self, folder, name, data):
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(json.dumps(data), encoding="utf-8")

    def test_load_saved_filters_by_source_and_skips_bad_files(self, tmp_path, caplog):
        self.write(tmp_path, "a.json", {"fonte_key": "horizon", "link": "l1"})
        self.write(tmp_path, "b.json", {"fonte_key": "fapes", "link": "l2"})
        self.write(tmp_path, "c.json", {"fonte_key": "horizon"})
        (tmp_path / "d.json").write_text("{quebrado", encoding="utf-8")
        saved = LocalJSONSink(str(tmp_path)).load_saved("horizon")
        assert list(saved) == ["l1"]
        assert "sem link" in caplog.text and "ilegível" in caplog.text

    def test_mark_closed_changes_only_the_status(self, tmp_path):
        self.write(tmp_path, "a.json", {"status": "aberto", "nome": "Chamada Ç", "link": "l1"})
        assert LocalJSONSink(str(tmp_path)).mark_closed(str(tmp_path / "a.json")) is True
        data = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
        assert data == {"status": "encerrado", "nome": "Chamada Ç", "link": "l1"}
        assert "Ç" in (tmp_path / "a.json").read_text(encoding="utf-8")

    def test_mark_closed_reports_failure(self, tmp_path):
        assert LocalJSONSink(str(tmp_path)).mark_closed(str(tmp_path / "nao_existe.json")) is False

    def test_remove_older_duplicates_keeps_the_newest(self, tmp_path):
        import os

        self.write(tmp_path, "velho.json", {"link": "l1"})
        self.write(tmp_path, "novo.json", {"link": "l1"})
        self.write(tmp_path, "outro.json", {"link": "l2"})
        os.utime(tmp_path / "velho.json", (1, 1))
        removed = LocalJSONSink(str(tmp_path)).remove_older_duplicates({"l1"})
        assert removed == 1
        assert sorted(p.name for p in tmp_path.glob("*.json")) == ["novo.json", "outro.json"]


class TestEmptyRead:
    def test_failed_download_blocks_closing_when_calls_are_open(self, env):
        import requests

        env.run(LAST_WEEK)
        source = HorizonSource(
            divisions=DIVISIONS, session=FakeSession(raise_error=requests.RequestException("x"))
        )
        env.capsys.readouterr()
        ingest_horizon_flow.run_pipeline(
            source=source, transform=EditalNormalizer(today=TODAY),
            sink=LocalJSONSink(str(env.output)), processed_index_path=env.index,
        )
        assert "closing_blocked=1" in env.capsys.readouterr().out
        assert env.only("C")[1]["status"] == "aberto"

    def test_past_deadline_call_is_rejected_again(self, env):
        env.run([call("H", deadline="2026-10-01")])
        assert env.files_for("H") == {}
        assert link_of("HORIZON-TEST-H") in env.rejected


class TestDailyRefreshStatus:
    """O realinhamento diário não reabre chamada que a lista do Horizon encerrou."""

    def test_horizon_closed_call_stays_closed_with_future_deadline(self):
        from scripts.curate_output import expected_status

        edital = {"fonte_key": "horizon", "status": "encerrado", "data_encerramento": "2027-04-21"}
        assert expected_status(edital, TODAY) == "encerrado"

    def test_other_sources_still_reopen_an_extended_deadline(self):
        from scripts.curate_output import expected_status

        edital = {"fonte_key": "fapes", "status": "encerrado", "data_encerramento": "2027-04-21"}
        assert expected_status(edital, TODAY) == "aberto"

    def test_horizon_call_past_its_deadline_is_closed(self):
        from scripts.curate_output import expected_status

        edital = {"fonte_key": "horizon", "status": "aberto", "data_encerramento": "2026-10-01"}
        assert expected_status(edital, TODAY) == "encerrado"
