"""
Arquivo de editais encerrados (RafaelDeps/retrieve_edital#11, spec 008).

Encerrados nunca saíam de data/output/: 192 de 567 em 26/11/2026, e uma limpeza
manual de 183 arquivos em novembro. Agora o realinhamento diário move para
data/arquivo/ os encerrados com prazo há mais de 30 dias, e o sink apaga a cópia
arquivada quando o edital é gravado de novo. Pastas em tmp_path, data fixa.
"""

import json
import pathlib
from datetime import date, timedelta

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from scripts import curate_output
from src.components.sinks.json_sink import LocalJSONSink
from src.domain.models import EditalDomain

scenarios("../../docs/features/arquivo_encerrados.feature")

TODAY = date(2026, 11, 26)


def iso(days_ago):
    return (TODAY - timedelta(days=days_ago)).isoformat()


def saved(name, status, deadline, link=None):
    cronograma = [{"evento": "Encerramento da submissão", "data": deadline}] if deadline else []
    return {
        "nome": name.upper(), "descrição": "Edital de teste com descrição real.",
        "orgão_fomento": "Horizon Europe", "categoria": "pesquisa", "status": status,
        "data_abertura": "", "data_encerramento": deadline, "link": link or f"https://exemplo/{name}",
        "cronograma": cronograma, "tags": [], "fonte_key": "horizon",
    }


class Folders:
    def __init__(self, tmp_path):
        self.workdir = tmp_path
        self.output = tmp_path / "data" / "output"
        self.archive = tmp_path / "data" / "arquivo"
        self.output.mkdir(parents=True)
        self.report = {}

    def put(self, folder, name, data):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")

    def names(self, folder):
        return sorted(p.stem for p in folder.glob("*.json")) if folder.is_dir() else []

    def links(self, folder):
        return {json.loads(p.read_text(encoding="utf-8"))["link"] for p in folder.glob("*.json")} if folder.is_dir() else set()

    def load(self, folder, name):
        return json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def ctx(tmp_path):
    return {"folders": Folders(tmp_path)}


# ---- Given -----------------------------------------------------------------


@given("an output folder with an edital closed 60 days ago, one closed 10 days ago, one closed without a deadline and one open")
def mixed_output(ctx):
    f = ctx["folders"]
    ctx["before"] = {
        "antigo": saved("antigo", "encerrado", iso(60)),
        "recente": saved("recente", "encerrado", iso(10)),
        "sem_prazo": saved("sem_prazo", "encerrado", ""),
        "aberto": saved("aberto", "aberto", (TODAY + timedelta(days=20)).isoformat()),
    }
    for name, data in ctx["before"].items():
        f.put(f.output, name, data)


@given("an edital still open whose deadline was 45 days ago")
def stale_open(ctx):
    ctx["folders"].put(ctx["folders"].output, "vencido", saved("vencido", "aberto", iso(45)))


@given(parsers.parse("the archive grace is {days:d} days"))
def grace(monkeypatch, days):
    monkeypatch.setenv(curate_output.ARCHIVE_GRACE_ENV, str(days))


@given("the archive already holds an edital the refresh would change")
def frozen(ctx):
    f = ctx["folders"]
    # Status "aberto" com prazo vencido: no output, o realinhamento o encerraria.
    f.put(f.archive, "congelado", saved("congelado", "aberto", iso(400)))
    ctx["frozen_bytes"] = (f.archive / "congelado.json").read_bytes()


@given(parsers.parse('the archive holds an edital with link "{link}"'))
def archived_link(ctx, link):
    f = ctx["folders"]
    f.put(f.archive, "arquivado", saved("arquivado", "encerrado", iso(90), link=link))


# ---- When ------------------------------------------------------------------


@when(parsers.parse("the daily refresh runs on {day}"))
def refresh(ctx, day):
    f = ctx["folders"]
    ctx["count_before"] = len(f.names(f.output)) + len(f.names(f.archive))
    f.report = curate_output.curate(
        workdir=f.workdir, apply=True, refresh_status_only=True, today=date.fromisoformat(day)
    )


@when(parsers.parse('the sink writes an edital with link "{link}"'))
def sink_writes(ctx, link):
    f = ctx["folders"]
    LocalJSONSink(str(f.output)).write([EditalDomain(
        nome="EDITAL REABERTO", descrição="d", orgão_fomento="Horizon Europe", categoria="pesquisa",
        status="aberto", data_abertura="", data_encerramento="2027-03-01", link=link,
        cronograma=[], tags=[],
    )])


# ---- Then ------------------------------------------------------------------


@then("the edital closed 60 days ago is in the archive with the same name and content")
def old_archived(ctx):
    f = ctx["folders"]
    assert "antigo" in f.names(f.archive)
    archived = f.load(f.archive, "antigo")
    assert archived["link"] == ctx["before"]["antigo"]["link"]
    assert archived["status"] == "encerrado"
    assert archived["data_encerramento"] == ctx["before"]["antigo"]["data_encerramento"]


@then("the other three editais are still in the output folder")
def others_stay(ctx):
    f = ctx["folders"]
    assert f.names(f.output) == ["aberto", "recente", "sem_prazo"]


@then(parsers.parse("the refresh reports {count:d} archived edital"))
def report(ctx, count):
    assert ctx["folders"].report["archived"] == count


@then(parsers.parse('that edital is in the archive as "{status}"'))
def stale_archived(ctx, status):
    f = ctx["folders"]
    assert f.load(f.archive, "vencido")["status"] == status
    assert "vencido" not in f.names(f.output)


@then("the edital closed 10 days ago is also in the archive")
def recent_archived(ctx):
    f = ctx["folders"]
    assert {"antigo", "recente"} <= set(f.names(f.archive))


@then("the two folders together hold the same number of editais as before")
def nothing_lost(ctx):
    f = ctx["folders"]
    assert len(f.names(f.output)) + len(f.names(f.archive)) == ctx["count_before"]
    assert not f.links(f.output) & f.links(f.archive)


@then("that archived edital is byte for byte the same")
def still_frozen(ctx):
    assert (ctx["folders"].archive / "congelado.json").read_bytes() == ctx["frozen_bytes"]


@then(parsers.parse('the link "{link}" is only in the output folder'))
def only_output(ctx, link):
    f = ctx["folders"]
    assert link in f.links(f.output)
    assert link not in f.links(f.archive)


@then(parsers.parse('the archive still holds the link "{link}"'))
def archive_kept(ctx, link):
    f = ctx["folders"]
    assert link in f.links(f.archive)


# ---- Unitários ---------------------------------------------------------------


class TestArchiveRules:
    @pytest.mark.parametrize(
        "status, deadline, due",
        [
            ("encerrado", iso(60), True),
            ("encerrado", iso(31), True),
            ("encerrado", iso(30), False),
            ("encerrado", iso(10), False),
            ("encerrado", "", False),
            ("encerrado", "15/10/2026", False),
            ("aberto", iso(60), False),
        ],
    )
    def test_archive_due(self, status, deadline, due):
        assert curate_output.archive_due({"status": status, "data_encerramento": deadline}, TODAY, 30) is due

    @pytest.mark.parametrize("value, expected", [(None, 30), ("5", 5), ("0", 0), ("x", 30), ("-1", 30)])
    def test_grace(self, monkeypatch, value, expected):
        if value is None:
            monkeypatch.delenv(curate_output.ARCHIVE_GRACE_ENV, raising=False)
        else:
            monkeypatch.setenv(curate_output.ARCHIVE_GRACE_ENV, value)
        assert curate_output.archive_grace_days() == expected

    def _file(self, folder, name, link):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.json"
        path.write_text(json.dumps({"link": link}), encoding="utf-8")
        return path

    def test_archive_file_keeps_the_name(self, tmp_path):
        source = self._file(tmp_path / "out", "edital", "l1")
        target = curate_output.archive_file(source, {"link": "l1"}, tmp_path / "arq")
        assert target.name == "edital.json" and not source.exists()

    def test_name_taken_by_another_edital_gets_a_suffix(self, tmp_path):
        self._file(tmp_path / "arq", "edital", "outro")
        source = self._file(tmp_path / "out", "edital", "l1")
        target = curate_output.archive_file(source, {"link": "l1"}, tmp_path / "arq")
        assert target.name.startswith("edital_") and target.name != "edital.json"
        assert json.loads((tmp_path / "arq" / "edital.json").read_text())["link"] == "outro"

    def test_same_edital_replaces_the_archived_copy(self, tmp_path):
        self._file(tmp_path / "arq", "edital", "l1")
        source = self._file(tmp_path / "out", "edital", "l1")
        target = curate_output.archive_file(source, {"link": "l1"}, tmp_path / "arq")
        assert target.name == "edital.json"
        assert len(list((tmp_path / "arq").glob("*.json"))) == 1


class TestWorkflowsCommitTheArchive:
    @pytest.mark.parametrize("workflow", ["run_scraper.yml", "run_horizon_weekly.yml"])
    def test_git_add_includes_the_archive(self, workflow):
        text = pathlib.Path(".github/workflows", workflow).read_text(encoding="utf-8")
        add_lines = [line for line in text.splitlines() if line.strip().startswith("git add")]
        assert add_lines and all("data/arquivo" in line for line in add_lines)

    def test_archive_folder_exists_in_the_repository(self):
        assert pathlib.Path("data/arquivo/.gitkeep").exists()
