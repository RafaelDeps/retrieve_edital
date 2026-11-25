"""
Editais com várias chamadas (RafaelDeps/retrieve_edital#5).

Caso de referência: Edital FAPES 10/2026 (Dr. Empreendedor Capixaba), com a 1ª
chamada de 04/05 a 07/07/2026 e a 2ª de 14/10 a 15/12/2026. O coletor gravava o
prazo da 1ª chamada e recusava o edital com a 2ª ainda aberta.
"""

from datetime import date

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from src.components.transforms import schedule_rules

scenarios("../../docs/features/multiplas_chamadas.feature")

REFERENCIA = [
    {"evento": "Início da submissão", "data": "2026-05-04", "chamada": "1ª chamada"},
    {"evento": "Encerramento da submissão", "data": "2026-07-07", "chamada": "1ª chamada"},
    {"evento": "Início da submissão", "data": "2026-10-14", "chamada": "2ª chamada"},
    {"evento": "Encerramento da submissão", "data": "2026-12-15", "chamada": "2ª chamada"},
]


@pytest.fixture
def ctx():
    return {}


@given("the reference schedule of Edital FAPES 10/2026")
def reference_schedule(ctx):
    ctx["events"] = [dict(e) for e in REFERENCIA]


@then(parsers.parse("it should have {stages:d} stages split into {calls:d} calls"))
def stages_and_calls(ctx, stages, calls):
    assert len(ctx["events"]) == stages
    assert len({e["chamada"] for e in ctx["events"]}) == calls


def _domain(data_encerramento):
    from src.domain.models import EditalDomain

    return EditalDomain(
        nome="EDITAL", descrição="d", orgão_fomento="FAPES", categoria="inovação",
        status="aberto", data_abertura="", data_encerramento=data_encerramento,
        link="https://exemplo", cronograma=[], tags=[],
    )


@when(parsers.parse("the schedule fields are derived on {today}"))
def derive_on(ctx, today):
    ctx["today"] = date.fromisoformat(today)
    ctx["fields"] = schedule_rules.derive_schedule_fields(ctx["events"], ctx["today"])


@then(parsers.parse("the closing date should be {closing}"))
def closing_is(ctx, closing):
    expected = "" if closing == "none" else closing
    assert ctx["fields"]["data_encerramento"] == expected


@then(parsers.parse("the opening date should be {opening}"))
def opening_is(ctx, opening):
    assert ctx["fields"]["data_abertura"] == opening


@then(parsers.parse("the status should be {status}"))
def status_is(ctx, status):
    from src.components.transforms.publication_rules import resolve_status

    edital = _domain(ctx["fields"]["data_encerramento"])
    assert resolve_status(edital, today=ctx["today"]) == status


UMA_CHAMADA = [
    {"evento": "Abertura das inscrições", "data": "2026-03-01"},
    {"evento": "Encerramento das inscrições", "data": "2026-04-30"},
]


class TestPrazoDaChamadaVigente:
    @pytest.mark.parametrize("today", ["2026-01-01", "2026-04-01", "2026-12-31"])
    def test_uma_chamada_so_nao_muda(self, today):
        campos = schedule_rules.derive_schedule_fields(UMA_CHAMADA, date.fromisoformat(today))
        assert campos["data_encerramento"] == "2026-04-30"
        assert campos["data_abertura"] == "2026-03-01"

    def test_chamadas_que_fecham_no_mesmo_dia(self):
        eventos = [
            {"evento": "Encerramento da submissão", "data": "2026-09-10", "chamada": "Ciclo 1"},
            {"evento": "Encerramento da submissão", "data": "2026-09-10", "chamada": "Ciclo 2"},
        ]
        assert schedule_rules.derive_closing(eventos, date(2026, 8, 1)) == "2026-09-10"

    def test_prazo_igual_a_hoje_ainda_vale(self):
        assert schedule_rules.derive_closing(REFERENCIA, date(2026, 7, 7)) == "2026-07-07"

    def test_sem_chamada_abertura_e_o_ultimo_inicio_antes_do_prazo(self):
        eventos = [{k: v for k, v in e.items() if k != "chamada"} for e in REFERENCIA]
        campos = schedule_rules.derive_schedule_fields(eventos, date(2026, 11, 25))
        assert campos["data_encerramento"] == "2026-12-15"
        assert campos["data_abertura"] == "2026-10-14"

    def test_data_relativa_nao_e_prazo(self):
        eventos = [
            {"evento": "Abertura das inscrições", "data": "2026-03-01"},
            {"evento": "Encerramento dos recursos", "data": "5 dias úteis após o resultado"},
        ]
        assert schedule_rules.derive_closing(eventos, date(2026, 1, 1)) == ""


def _saved_edital(**overrides):
    edital = {
        "nome": "EDITAL FAPES Nº 10/2026 - DR. EMPREENDEDOR CAPIXABA",
        "descrição": "Apoio a doutores empreendedores.",
        "orgão_fomento": "FAPES",
        "categoria": "inovação",
        "status": "encerrado",
        "data_abertura": "2026-05-04",
        "data_encerramento": "2026-07-07",
        "link": "https://fapes.es.gov.br/media/edital_10_2026.pdf",
        "cronograma": [dict(e) for e in REFERENCIA],
        "tags": ["fapes"],
        "fonte_key": "fapes",
    }
    edital.update(overrides)
    return edital


def _run_refresh(workdir, edital, today):
    import json

    from scripts.curate_output import curate

    output_dir = workdir / "data" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "ref.json"
    path.write_text(json.dumps(edital, ensure_ascii=False), encoding="utf-8")
    curate(workdir=workdir, apply=True, refresh_status_only=True, today=today)
    return json.loads(path.read_text(encoding="utf-8"))


@given("a saved edital with the reference schedule and the old deadline 2026-07-07")
def saved_edital(ctx):
    ctx["saved"] = _saved_edital()


@when(parsers.parse("the daily refresh runs on {today}"))
def daily_refresh(ctx, tmp_path, today):
    ctx["result"] = _run_refresh(tmp_path, ctx["saved"], date.fromisoformat(today))


@then(parsers.parse("the saved closing date should be {closing}"))
def saved_closing(ctx, closing):
    assert ctx["result"]["data_encerramento"] == closing


@then(parsers.parse("the saved opening date should be {opening}"))
def saved_opening(ctx, opening):
    assert ctx["result"]["data_abertura"] == opening


@then(parsers.parse("the saved status should be {status}"))
def saved_status(ctx, status):
    assert ctx["result"]["status"] == status


class TestRealinhamentoDoCronograma:
    def test_sem_eventos_de_fim_o_prazo_e_mantido(self):
        from scripts.curate_output import expected_schedule_fields

        edital = _saved_edital(
            cronograma=[{"evento": "Publicação do edital", "data": "2026-03-01"}],
            data_encerramento="2026-09-30",
        )
        mudancas = expected_schedule_fields(edital, date(2026, 6, 1))
        assert "data_encerramento" not in mudancas

    def test_prazo_vindo_de_inicio_solto_e_limpo(self):
        from scripts.curate_output import expected_schedule_fields

        edital = _saved_edital(
            cronograma=[{"evento": "Início da vigência dos projetos", "data": "2027-05-01"}],
            data_encerramento="2027-05-01",
        )
        mudancas = expected_schedule_fields(edital, date(2026, 11, 25))
        assert mudancas["data_encerramento"] == ""

    def test_prazo_da_fonte_com_cronograma_vazio_e_mantido(self):
        from scripts.curate_output import expected_schedule_fields

        edital = _saved_edital(cronograma=[], data_encerramento="2026-12-01")
        assert "data_encerramento" not in expected_schedule_fields(edital, date(2026, 11, 25))


@given(parsers.parse('a schedule with the single stage "{evento}" on "{data}"'))
def single_stage(ctx, evento, data):
    ctx["events"] = [{"evento": evento, "data": data}]


@when(parsers.parse("the closing date is derived on {today}"))
def closing_derived(ctx, today):
    ctx["fields"] = {
        "data_encerramento": schedule_rules.derive_closing(
            ctx["events"], date.fromisoformat(today)
        )
    }


def test_ate_sem_data_nao_e_prazo():
    eventos = [{"evento": "Estatuto social válido até a homologação", "data": "a definir"}]
    assert schedule_rules.closing_candidates(eventos) == []


def test_ate_so_casa_como_palavra_inteira():
    assert not schedule_rules.is_end_event({"evento": "Comprovante de matrícula atestado"})


def test_realinhamento_grava_mudanca_so_de_prazo(tmp_path):
    """Regressão: mudança só no cronograma (sem mudar status) também é gravada."""
    edital = _saved_edital(
        status="aberto",
        cronograma=[{"evento": "Início da vigência dos projetos", "data": "2027-05-01"}],
        data_encerramento="2027-05-01",
    )
    resultado = _run_refresh(tmp_path, edital, date(2026, 11, 25))
    assert resultado["data_encerramento"] == ""


def test_abertura_nao_usa_periodo_de_julgamento():
    """Regressão (CNPq 37/2026): abertura é a das inscrições, não a do julgamento."""
    eventos = [
        {"evento": "Abertura das inscrições", "data": "2026-11-11"},
        {"evento": "Data limite para submissão das propostas", "data": "2027-03-12"},
        {"evento": "Período de Julgamento", "data": "2027-03-01"},
        {"evento": "Início da implementação das bolsas", "data": "2027-08"},
    ]
    campos = schedule_rules.derive_schedule_fields(eventos, date(2026, 11, 25))
    assert campos["data_encerramento"] == "2027-03-12"
    assert campos["data_abertura"] == "2026-11-11"


class TestChamadaPreservadaNaExtracao:
    def test_mapeamento_da_ia_preserva_chamada(self):
        from src.components.transforms.mistral_client import MistralExtractionService

        servico = MistralExtractionService(api_key="x")
        dominio = servico._map_to_domain({"nome": "edital", "cronograma": REFERENCIA})
        assert [e.get("chamada") for e in dominio.cronograma] == [e["chamada"] for e in REFERENCIA]

    def test_mapeamento_da_ia_aceita_formato_antigo(self):
        from src.components.transforms.mistral_client import MistralExtractionService

        servico = MistralExtractionService(api_key="x")
        dominio = servico._map_to_domain(
            {"nome": "edital", "cronograma": [{"etapa": "Submissão", "data": "2026-05-04"}]}
        )
        assert dominio.cronograma == [{"evento": "Submissão", "data": "2026-05-04"}]

    def test_uniao_mantem_mesma_etapa_de_chamadas_diferentes(self):
        from src.components.transforms.edital_normalizer import merge_cronograma

        unido = merge_cronograma([], REFERENCIA)
        assert unido == REFERENCIA

    def test_uniao_preserva_chamada_da_fonte(self):
        from src.components.transforms.edital_normalizer import merge_cronograma

        fonte = [{"evento": "Encerramento da submissão", "data": "2026-12-15", "chamada": "2ª chamada"}]
        unido = merge_cronograma(fonte, [])
        assert unido[0]["chamada"] == "2ª chamada"

    def test_normalizacao_preserva_chamada_e_nao_inventa(self):
        from src.components.transforms.date_utils import normalize_schedule_dates

        normalizado = normalize_schedule_dates(
            [
                {"evento": "Início da submissão", "data": "04/05/2026", "chamada": "1ª chamada"},
                {"evento": "Resultado final", "data": "2027-01-30"},
            ]
        )
        assert normalizado[0] == {
            "evento": "Início da submissão", "data": "2026-05-04", "chamada": "1ª chamada"
        }
        assert "chamada" not in normalizado[1]

    def test_prompt_divide_intervalos_e_pede_chamada(self):
        from src.components.transforms.mistral_client import MistralExtractionService

        prompt = MistralExtractionService(api_key="x")._get_extraction_prompt("TEXTO")
        assert "Encerramento de" in prompt
        assert '"chamada"' in prompt
        assert "use apenas a primeira data" not in prompt


@then(parsers.parse('the calls should be "{calls}"'))
def calls_are(ctx, calls):
    esperado = []
    for parte in calls.split("; "):
        nome, periodo = parte.rsplit(" ", 1)
        abertura, encerramento = periodo.split("..")
        esperado.append({"nome": nome, "abertura": abertura, "encerramento": encerramento})
    assert ctx["fields"]["chamadas"] == esperado


@then(parsers.parse('the next stage should be "{evento}" on {data}'))
def next_stage_is(ctx, evento, data):
    assert ctx["fields"]["proxima_etapa"] == {"evento": evento, "data": data}


class TestChamadasEProximaEtapa:
    def test_sem_chamada_lista_vazia(self):
        assert schedule_rules.derive_schedule_fields(UMA_CHAMADA, date(2026, 1, 1))["chamadas"] == []

    def test_sem_etapa_futura_proxima_etapa_nula(self):
        campos = schedule_rules.derive_schedule_fields(REFERENCIA, date(2027, 6, 1))
        assert campos["proxima_etapa"] is None

    def test_grafias_diferentes_da_mesma_chamada(self):
        eventos = [
            {"evento": "Início da submissão", "data": "2026-10-14", "chamada": "2ª Chamada"},
            {"evento": "Encerramento da submissão", "data": "2026-12-15", "chamada": "2a chamada"},
        ]
        assert schedule_rules.derive_chamadas(eventos) == [
            {"nome": "2ª Chamada", "abertura": "2026-10-14", "encerramento": "2026-12-15"}
        ]

    def test_json_antigo_sem_campos_novos_e_realinhado(self, tmp_path):
        antigo = _saved_edital(status="aberto", data_encerramento="2026-12-15")
        resultado = _run_refresh(tmp_path, antigo, date(2026, 11, 25))
        assert resultado["chamadas"][1]["nome"] == "2ª chamada"
        assert resultado["proxima_etapa"]["data"] == "2026-12-15"

    def test_normalizador_publica_os_campos(self):
        from unittest.mock import MagicMock

        from src.components.transforms.edital_normalizer import EditalNormalizer
        from src.domain.models import RawEdital

        servico = MagicMock()
        servico.extract_from_pdf.return_value = None
        normalizador = EditalNormalizer(extraction_service=servico, today=date(2026, 11, 25))
        edital = normalizador.process(
            RawEdital(
                title="Edital FAPES 10/2026",
                url="https://fapes.es.gov.br/edital-10-2026.pdf",
                raw_agency="FAPES",
                raw_description="Apoio a doutores empreendedores.",
                raw_cronograma=[dict(e) for e in REFERENCIA],
            )
        )
        assert edital.data_encerramento == "2026-12-15"
        assert len(edital.chamadas) == 2
        assert edital.proxima_etapa == {"evento": "Encerramento da submissão", "data": "2026-12-15"}


class TestDescontoDaReextracao:
    def _indice(self, tmp_path):
        import json

        caminho = tmp_path / "processed.json"
        caminho.write_text(json.dumps({"fapes": ["https://a", "https://b"]}), encoding="utf-8")
        return str(caminho)

    def test_chaves_do_arquivo_sao_descontadas(self, tmp_path, monkeypatch):
        from src import processed_store

        lista = tmp_path / "reextrair.txt"
        lista.write_text("https://a\n\n", encoding="utf-8")
        monkeypatch.setenv(processed_store.REEXTRACT_KEYS_ENV, str(lista))
        assert processed_store.get_keys_set("fapes", path=self._indice(tmp_path)) == {"https://b"}

    def test_sem_variavel_nada_muda(self, tmp_path, monkeypatch):
        from src import processed_store

        monkeypatch.delenv(processed_store.REEXTRACT_KEYS_ENV, raising=False)
        assert processed_store.get_keys_set("fapes", path=self._indice(tmp_path)) == {
            "https://a",
            "https://b",
        }


def test_realinhamento_grava_proxima_etapa_nula(tmp_path):
    """O contrato garante a chave: sem etapa futura, `proxima_etapa` é null, não ausente."""
    antigo = _saved_edital(status="encerrado")
    resultado = _run_refresh(tmp_path, antigo, date(2027, 6, 1))
    assert "proxima_etapa" in resultado and resultado["proxima_etapa"] is None
