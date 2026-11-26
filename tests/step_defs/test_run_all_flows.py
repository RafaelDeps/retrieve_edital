import os
import sys
import time

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from scripts import run_all_flows
from src.flow_health import FlowStats

scenarios("../../docs/features/run_all_flows.feature")

DAILY_FLOWS = ["FAPES", "FINEP", "CONIF", "PRPPG_IFES", "PROEX_IFES", "CAPES", "CNPQ"]


@pytest.fixture(autouse=True)
def isolated_runner_env(monkeypatch):
    """No CI essas variáveis existem de verdade: os testes não podem herdá-las."""
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("FLOW_TIMEOUT_SEC", raising=False)


@pytest.fixture
def runner_context(monkeypatch):
    """Estado compartilhado pelos steps BDD: fluxos simulados, chamadas e exit code."""
    context = {"failing": set(), "invoked": [], "exit_code": None}

    def fake_run_flow(name, command, workdir, timeout_sec=None):
        context["invoked"].append(name)
        if name in context["failing"]:
            return run_all_flows.RESULT_FAILURE
        return run_all_flows.RESULT_SUCCESS

    monkeypatch.setattr(run_all_flows, "run_flow", fake_run_flow)
    monkeypatch.setattr(run_all_flows, "refresh_edital_status", lambda workdir: None)
    return context


@given("every flow finishes successfully")
def every_flow_succeeds(runner_context):
    runner_context["failing"] = set()


@given(parsers.parse('the flow "{flow}" fails and every other flow succeeds'))
def one_flow_fails(runner_context, flow):
    runner_context["failing"] = {flow}


@when("the runner executes without a flow filter")
def runner_executes(runner_context):
    runner_context["exit_code"] = run_all_flows.main([])


@then(
    "it should invoke FAPES, FINEP, CONIF, PRPPG_IFES, PROEX_IFES, CAPES and CNPQ in this order"
)
def invoked_in_order(runner_context):
    assert runner_context["invoked"] == DAILY_FLOWS


@then("all seven daily flows should have been invoked")
def all_daily_invoked(runner_context):
    assert sorted(runner_context["invoked"]) == sorted(DAILY_FLOWS)


@given("the job summary file is available")
def job_summary_available(runner_context, monkeypatch, tmp_path):
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    runner_context["summary_path"] = summary_path


@then("the job summary should have one row per daily flow")
def summary_has_rows(runner_context):
    summary = runner_context["summary_path"].read_text(encoding="utf-8")
    assert "| Fonte | Resultado |" in summary
    for flow in DAILY_FLOWS:
        assert f"| {flow} |" in summary


@then(parsers.parse('the job summary should highlight "{flow}" as failed'))
def summary_highlights_failure(runner_context, flow):
    summary = runner_context["summary_path"].read_text(encoding="utf-8")
    assert f"| {flow} | {run_all_flows.RESULT_FAILURE} |" in summary
    assert f"**Falha em:** {flow}" in summary


@given("a flow that sleeps for 30 seconds without printing anything")
def silent_hanging_flow(runner_context):
    runner_context["command"] = [sys.executable, "-c", "import time; time.sleep(30)"]


@when("the runner executes it with a time limit of 1 second")
def run_with_short_limit(runner_context, tmp_path):
    started = time.monotonic()
    return_code, _ = run_all_flows.run_command_capturing_output(
        runner_context["command"], tmp_path, timeout_sec=1
    )
    runner_context["elapsed"] = time.monotonic() - started
    runner_context["return_code"] = return_code


@then("the flow should be killed with the timeout exit code")
def killed_with_timeout_code(runner_context):
    assert runner_context["return_code"] == run_all_flows.TIMEOUT_RETURN_CODE


@then(parsers.parse("it should take less than {seconds:d} seconds"))
def finished_quickly(runner_context, seconds):
    assert runner_context["elapsed"] < seconds


@given(
    parsers.parse(
        'the time limit option is "{cli}" and the environment variable is "{env}"'
    )
)
def time_limit_inputs(runner_context, cli, env):
    # "none" nos exemplos = valor não informado.
    runner_context["cli"] = None if cli == "none" else int(cli)
    runner_context["environ"] = (
        {} if env == "none" else {run_all_flows.FLOW_TIMEOUT_ENV: env}
    )


@when("the runner resolves the time limit")
def resolve_time_limit(runner_context):
    runner_context["timeout"] = run_all_flows.resolve_flow_timeout(
        runner_context["cli"], runner_context["environ"]
    )


@then(parsers.parse("the time limit should be {expected:d} seconds"))
def time_limit_is(runner_context, expected):
    assert runner_context["timeout"] == expected


@when("the flows are selected without a filter")
def select_without_filter(runner_context):
    runner_context["selected"] = [nome for nome, _ in run_all_flows.select_flows(None)]


@when(parsers.parse('the flows are selected with the filter "{only}"'))
def select_with_filter(runner_context, only):
    runner_context["selected"] = [nome for nome, _ in run_all_flows.select_flows(only)]


@then(parsers.parse('"{flow}" should not be selected'))
def flow_not_selected(runner_context, flow):
    assert flow not in runner_context["selected"]


@then(parsers.parse('the selected flows should be "{flows}"'))
def selected_flows_are(runner_context, flows):
    assert runner_context["selected"] == flows.split(",")


@then(parsers.parse("the runner should exit with code {code:d}"))
def runner_exit_code(runner_context, code):
    assert runner_context["exit_code"] == code


def test_flow_commands_include_capes_and_cnpq_in_expected_order():
    assert [name for name, _ in run_all_flows.FLOW_COMMANDS] == [
        "FAPES",
        "FINEP",
        "CONIF",
        "PRPPG_IFES",
        "PROEX_IFES",
        "CAPES",
        "CNPQ",
    ]


def test_registry_keys_include_new_integrated_flows():
    assert run_all_flows.REGISTRY_KEYS == {
        "FAPES": "fapes",
        "FINEP": "finep",
        "CONIF": "conif",
        "PRPPG_IFES": "prppg_ifes",
        "PROEX_IFES": "proex_ifes",
        "CAPES": "capes",
        "CNPQ": "cnpq",
        "HORIZON": "horizon",
    }


def test_horizon_e_opcional_e_roda_o_fluxo_do_horizon():
    assert run_all_flows.select_flows("HORIZON") == [
        ("HORIZON", [sys.executable, "-m", "src.flows.ingest_horizon_flow"])
    ]


def test_main_runs_all_flows_in_sequence(monkeypatch, tmp_path):
    executed = []

    def fake_run_flow(name, command, workdir, timeout_sec=None):
        executed.append((name, command[2]))
        return run_all_flows.RESULT_SUCCESS

    monkeypatch.setattr(run_all_flows, "run_flow", fake_run_flow)
    monkeypatch.setattr(run_all_flows, "refresh_edital_status", lambda workdir: None)

    exit_code = run_all_flows.main([])

    assert exit_code == 0
    assert executed == [
        ("FAPES", "src.flows.ingest_fapes_flow"),
        ("FINEP", "src.flows.ingest_finep_flow"),
        ("CONIF", "src.flows.ingest_conif_flow"),
        ("PRPPG_IFES", "src.flows.ingest_prppg_ifes_flow"),
        ("PROEX_IFES", "src.flows.ingest_proex_ifes_flow"),
        ("CAPES", "src.flows.ingest_capes_flow"),
        ("CNPQ", "src.flows.ingest_cnpq_flow"),
    ]


class TestSelecaoDeFluxos:
    def test_sem_only_roda_todos(self):
        assert len(run_all_flows.select_flows(None)) == len(run_all_flows.FLOW_COMMANDS)

    def test_only_filtra_e_preserva_a_ordem(self):
        selecionados = [nome for nome, _ in run_all_flows.select_flows("CNPQ,FINEP")]
        # A ordem é a de FLOW_COMMANDS, não a do argumento.
        assert selecionados == ["FINEP", "CNPQ"]

    def test_nome_desconhecido_e_erro_explicito(self):
        """Silenciosamente não rodar nada seria pior que falhar."""
        with pytest.raises(SystemExit, match="Fluxo desconhecido"):
            run_all_flows.select_flows("FAPEMIG")

    def test_espacos_e_caixa_sao_tolerados(self):
        assert [nome for nome, _ in run_all_flows.select_flows(" finep , cnpq ")] == [
            "FINEP",
            "CNPQ",
        ]


class TestIsolamentoDeFalha:
    """
    Com oito fontes, uma indisponível zerava a coleta de todas as outras: o runner
    fazia `raise SystemExit` na primeira falha. Agora registra e segue, e o exit
    code final preserva a informação de que houve falha.
    """

    def test_falha_de_um_fluxo_nao_interrompe_os_demais(self, monkeypatch):
        executados = []

        def fake_run_flow(name, command, workdir, timeout_sec=None):
            executados.append(name)
            return (
                run_all_flows.RESULT_FAILURE
                if name == "FINEP"
                else run_all_flows.RESULT_SUCCESS
            )

        monkeypatch.setattr(run_all_flows, "run_flow", fake_run_flow)
        monkeypatch.setattr(run_all_flows, "refresh_edital_status", lambda workdir: None)

        exit_code = run_all_flows.main([])

        assert len(executados) == len(run_all_flows.FLOW_COMMANDS)
        assert exit_code == 1, "a falha precisa aparecer no exit code"

    def test_apenas_atencao_nao_falha_a_execucao(self, monkeypatch):
        monkeypatch.setattr(
            run_all_flows,
            "run_flow",
            lambda *a, **k: run_all_flows.RESULT_WARNING,
        )
        monkeypatch.setattr(run_all_flows, "refresh_edital_status", lambda workdir: None)
        assert run_all_flows.main([]) == 0

    def test_status_e_realinhado_mesmo_havendo_falha(self, monkeypatch):
        chamado = []
        monkeypatch.setattr(
            run_all_flows, "run_flow", lambda *a, **k: run_all_flows.RESULT_FAILURE
        )
        monkeypatch.setattr(
            run_all_flows, "refresh_edital_status", lambda workdir: chamado.append(True)
        )
        run_all_flows.main([])
        assert chamado, "o realinhamento de status não depende do sucesso dos fluxos"


class TestTetoDeDuracao:
    def test_fluxo_travado_e_encerrado_e_reportado_como_falha(self, tmp_path):
        import sys

        # Processo que escreve e depois dorme muito além do teto.
        codigo = "import sys,time\nprint('inicio', flush=True)\ntime.sleep(30)\nprint('fim', flush=True)"
        return_code, saida = run_all_flows.run_command_capturing_output(
            [sys.executable, "-c", codigo], tmp_path, timeout_sec=0
        )
        assert return_code == run_all_flows.TIMEOUT_RETURN_CODE
        assert "Teto de" in saida
        assert "fim" not in saida, "o processo foi encerrado antes de concluir"

    def test_fluxo_rapido_nao_e_afetado(self, tmp_path):
        import sys

        return_code, saida = run_all_flows.run_command_capturing_output(
            [sys.executable, "-c", "print('ok')"], tmp_path, timeout_sec=60
        )
        assert return_code == 0
        assert "ok" in saida


def write_log(workdir, rows):
    """Monta um flow_processing_log.md com as linhas dadas (mais recente primeiro)."""
    docs_dir = workdir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    header = (
        "# Flow Processing Log\n\n"
        "| Data/Hora | Fluxo | Resultado | Observações |\n"
        "| :-- | :-- | :-- | :-- |\n"
    )
    (docs_dir / "flow_processing_log.md").write_text(
        header + "".join(rows), encoding="utf-8"
    )


def log_row(flow, delta, result="Sucesso"):
    return (
        f"| 2026-07-27 03:00:00 -03:00 | `{flow}` | {result} | "
        f"Registry `x`: 10 -> 10 (delta {delta}); outras notas. |\n"
    )


class TestZeroDeltaStreak:
    def test_counts_consecutive_zero_delta_runs_for_the_flow(self, tmp_path):
        write_log(tmp_path, [log_row("FINEP", 0), log_row("FINEP", 0), log_row("FINEP", 3)])
        assert run_all_flows.count_zero_delta_streak(tmp_path, "FINEP") == 2

    def test_stops_at_the_first_run_that_collected_something(self, tmp_path):
        write_log(tmp_path, [log_row("FINEP", 2), log_row("FINEP", 0)])
        assert run_all_flows.count_zero_delta_streak(tmp_path, "FINEP") == 0

    def test_ignores_rows_of_other_flows(self, tmp_path):
        write_log(tmp_path, [log_row("CNPQ", 0), log_row("FINEP", 0), log_row("CNPQ", 0)])
        assert run_all_flows.count_zero_delta_streak(tmp_path, "FINEP") == 1

    def test_returns_zero_when_log_is_absent(self, tmp_path):
        assert run_all_flows.count_zero_delta_streak(tmp_path, "FINEP") == 0


class TestResolveResult:
    def test_failure_wins_over_every_other_signal(self):
        assert (
            run_all_flows.resolve_result("FINEP", 1, 5, 0, FlowStats(36, 5))
            == run_all_flows.RESULT_FAILURE
        )

    def test_source_returning_no_raw_item_is_a_warning_not_success(self):
        """Foi assim que a queda da FINEP passou meses reportada como Sucesso."""
        assert (
            run_all_flows.resolve_result("FINEP", 0, 0, 1, FlowStats(0, 0))
            == run_all_flows.RESULT_WARNING
        )

    def test_no_new_editais_but_healthy_source_is_success(self):
        assert (
            run_all_flows.resolve_result("FINEP", 0, 0, 1, FlowStats(36, 0))
            == run_all_flows.RESULT_SUCCESS
        )

    def test_proven_healthy_source_overrides_a_long_zero_delta_streak(self):
        """
        Em 2026-07-31, 6 dos 7 fluxos saíram como Atenção. FINEP e CNPq estavam
        provadamente saudáveis (36 e 9 itens brutos) e foram marcados só pela
        sequência de deltas zero. Alerta que dispara sempre não informa nada.
        """
        streak = run_all_flows.ZERO_DELTA_ALERT_THRESHOLD * 3
        assert (
            run_all_flows.resolve_result("FINEP", 0, 0, streak, FlowStats(36, 0))
            == run_all_flows.RESULT_SUCCESS
        )

    def test_empty_source_is_a_warning_even_on_the_first_occurrence(self):
        assert (
            run_all_flows.resolve_result("FINEP", 0, 0, 1, FlowStats(0, 0))
            == run_all_flows.RESULT_WARNING
        )

    def test_long_zero_delta_streak_without_stats_is_a_warning(self):
        streak = run_all_flows.ZERO_DELTA_ALERT_THRESHOLD
        assert (
            run_all_flows.resolve_result("CNPQ", 0, 0, streak, None)
            == run_all_flows.RESULT_WARNING
        )

    def test_short_zero_delta_streak_is_still_success(self):
        assert (
            run_all_flows.resolve_result("CNPQ", 0, 0, 2, None)
            == run_all_flows.RESULT_SUCCESS
        )

    def test_low_volume_flows_are_exempt_from_the_streak_rule(self, monkeypatch):
        monkeypatch.setattr(run_all_flows, "LOW_VOLUME_FLOWS", frozenset({"cnpq"}))
        streak = run_all_flows.ZERO_DELTA_ALERT_THRESHOLD + 5
        assert (
            run_all_flows.resolve_result("CNPQ", 0, 0, streak, None)
            == run_all_flows.RESULT_SUCCESS
        )


class TestDeferredItemsResult:
    """Itens adiados pela IA indisponível (spec 004) deixam o fluxo em Atenção."""

    def test_deferred_items_are_a_warning(self):
        assert (
            run_all_flows.resolve_result("CAPES", 0, 2, 0, FlowStats(10, 2, 3))
            == run_all_flows.RESULT_WARNING
        )

    def test_failure_still_wins_over_deferred(self):
        assert (
            run_all_flows.resolve_result("CAPES", 1, 0, 0, FlowStats(10, 0, 3))
            == run_all_flows.RESULT_FAILURE
        )

    def test_observation_counts_deferred_items(self):
        result, observations = run_all_flows.build_observations(
            "CAPES", {"capes": 5}, {"capes": 7}, 100, [], 0,
            stats=FlowStats(raw_count=10, new_count=2, deferred_count=3),
        )
        assert result == run_all_flows.RESULT_WARNING
        assert "3 itens adiados por limite da IA" in observations

    def test_without_deferred_nothing_changes(self):
        _, observations = run_all_flows.build_observations(
            "CAPES", {"capes": 5}, {"capes": 7}, 100, [], 0,
            stats=FlowStats(raw_count=10, new_count=2),
        )
        assert "adiados" not in observations


class TestBuildObservations:
    def test_reports_raw_count_and_flags_empty_source(self):
        result, observations = run_all_flows.build_observations(
            "FINEP",
            {"finep": 10},
            {"finep": 10},
            175,
            [],
            0,
            stats=FlowStats(raw_count=0, new_count=0),
            zero_delta_streak=1,
        )
        assert result == run_all_flows.RESULT_WARNING
        assert "Origem devolveu 0 itens brutos" in observations
        assert "verificar se o portal mudou" in observations

    def test_healthy_run_stays_successful(self):
        result, observations = run_all_flows.build_observations(
            "FINEP",
            {"finep": 10},
            {"finep": 36},
            201,
            [],
            0,
            stats=FlowStats(raw_count=36, new_count=26),
            zero_delta_streak=0,
        )
        assert result == run_all_flows.RESULT_SUCCESS
        assert "(delta 26)" in observations


@pytest.mark.parametrize(
    "line, expected",
    [
        ("| ts | `FINEP` | Sucesso | Registry (delta 0); |", ("FINEP", "Registry (delta 0);")),
        ("| :-- | :-- | :-- | :-- |", None),
        ("texto solto", None),
        ("| incompleto |", None),
    ],
)
def test_parse_log_row(line, expected):
    assert run_all_flows._parse_log_row(line) == expected


class TestResumoDoJob:
    def test_sem_variavel_nada_e_escrito(self, tmp_path):
        run_all_flows.write_step_summary(
            {"FINEP": run_all_flows.RESULT_FAILURE}, environ={}
        )
        assert list(tmp_path.iterdir()) == []

    def test_sem_falhas_nao_ha_destaque(self, tmp_path):
        summary_path = tmp_path / "summary.md"
        run_all_flows.write_step_summary(
            {"FINEP": run_all_flows.RESULT_SUCCESS},
            environ={"GITHUB_STEP_SUMMARY": str(summary_path)},
        )
        summary = summary_path.read_text(encoding="utf-8")
        assert "| FINEP | Sucesso |" in summary
        assert "Falha em" not in summary


class TestTetoPeloRelogio:
    def test_processo_neto_tambem_e_encerrado(self, tmp_path):
        """O Chromium do Playwright é neto do runner: não pode ficar órfão."""
        codigo = (
            "import subprocess, sys, time\n"
            "neto = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(neto.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        return_code, saida = run_all_flows.run_command_capturing_output(
            [sys.executable, "-c", codigo], tmp_path, timeout_sec=1
        )
        assert return_code == run_all_flows.TIMEOUT_RETURN_CODE
        pid_neto = int(saida.split()[0])
        limite = time.monotonic() + 5
        while time.monotonic() < limite:
            try:
                os.kill(pid_neto, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        pytest.fail(f"o processo neto {pid_neto} continua vivo após o teto")


class TestTetoInvalido:
    @pytest.mark.parametrize("valor", ["abc", "0", "-5"])
    def test_variavel_invalida_e_erro_de_uso(self, monkeypatch, valor):
        monkeypatch.setenv("FLOW_TIMEOUT_SEC", valor)
        monkeypatch.setattr(run_all_flows, "run_flow", lambda *a, **k: pytest.fail("rodou fluxo"))
        with pytest.raises(SystemExit) as erro:
            run_all_flows.main([])
        assert erro.value.code == 2

    def test_opcao_zero_e_erro_de_uso(self, monkeypatch):
        monkeypatch.setattr(run_all_flows, "run_flow", lambda *a, **k: pytest.fail("rodou fluxo"))
        with pytest.raises(SystemExit) as erro:
            run_all_flows.main(["--timeout", "0"])
        assert erro.value.code == 2


def _grava_edital(diretorio, nome, **campos):
    import json

    diretorio.mkdir(parents=True, exist_ok=True)
    (diretorio / nome).write_text(json.dumps(campos, ensure_ascii=False), encoding="utf-8")


class TestReextracao:
    def test_alvos_sao_abertos_de_fontes_com_ocr(self, tmp_path):
        saida = tmp_path / "out"
        _grava_edital(saida, "a.json", link="https://fapes/a", status="aberto", fonte_key="fapes")
        _grava_edital(saida, "b.json", link="https://fapes/b", status="encerrado", fonte_key="fapes")
        _grava_edital(saida, "c.json", link="https://finep/c", status="aberto", fonte_key="finep")
        _grava_edital(saida, "d.json", link="https://capes/d", status="aberto", fonte_key="capes")
        assert run_all_flows.select_reextraction_targets(saida) == {
            "capes": ["https://capes/d"],
            "fapes": ["https://fapes/a"],
        }

    def test_roda_so_os_fluxos_com_alvos_e_expoe_a_lista(self, monkeypatch, tmp_path):
        from src import processed_store

        saida = tmp_path / "out"
        _grava_edital(saida, "a.json", link="https://conif/a", status="aberto", fonte_key="conif")
        monkeypatch.setattr(run_all_flows, "OUTPUT_PATH", saida)
        monkeypatch.setattr(run_all_flows, "refresh_edital_status", lambda workdir: None)
        vistos = []

        def fake_run_flow(name, command, workdir, timeout_sec=None):
            caminho = os.environ.get(processed_store.REEXTRACT_KEYS_ENV)
            with open(caminho, encoding="utf-8") as lista:
                vistos.append((name, lista.read().split()))
            return run_all_flows.RESULT_SUCCESS

        monkeypatch.setattr(run_all_flows, "run_flow", fake_run_flow)
        assert run_all_flows.main(["--reextrair-abertos"]) == 0
        assert vistos == [("CONIF", ["https://conif/a"])]
        assert processed_store.REEXTRACT_KEYS_ENV not in os.environ

    def test_duplicatas_do_mesmo_link_ficam_so_a_mais_recente(self, tmp_path):
        saida = tmp_path / "out"
        _grava_edital(saida, "velho.json", link="https://x", status="aberto")
        os.utime(saida / "velho.json", (1, 1))
        _grava_edital(saida, "novo.json", link="https://x", status="aberto")
        _grava_edital(saida, "outro.json", link="https://y", status="aberto")
        assert run_all_flows.dedupe_by_link(saida, {"https://x"}) == 1
        assert sorted(p.name for p in saida.iterdir()) == ["novo.json", "outro.json"]
