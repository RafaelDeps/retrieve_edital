"""
Extração por IA com o Gemini (spec 004).

A Mistral passou a recusar todas as chamadas (402, 25/11/2026). O Gemini, no
nível gratuito, lê o texto extraído localmente do PDF. Os testes usam um cliente
falso, com relógio e espera falsos: nada abre rede e nada espera de verdade. O
único teste com a chave real é o `integration` do FAPES 10/2026.
"""

import json
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors
from pytest_bdd import given, parsers, scenarios, then, when

from src.components.transforms import extraction_prompts, gemini_client
from src.components.transforms.edital_normalizer import EditalNormalizer
from src.components.transforms.extraction_service import (
    AIUnavailableError,
    UnavailableExtractionService,
    get_extraction_service,
)
from src.components.transforms.gemini_client import (
    GeminiExtractionService,
    classify_api_error,
    retry_delay_seconds,
)
from src.domain.models import RawEdital

scenarios("../../docs/features/extracao_gemini.feature")

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fapes_10_2026_cronograma_gemini.json"
PDF_TEXT = "TEXTO DO EDITAL FAPES Nº 10/2026 - DR. EMPREENDEDOR CAPIXABA"
FAKE_KEY = "SEGREDO-FAKE-KEY-123"
FAPES_10_PDF_URL = (
    "https://fapes.es.gov.br/media/Portif%C3%B3lios/"
    "edital_fapes_no_10-2026_-_dr-_empreendedor_capixaba_-_1a_alteracao_.pdf"
)


def _quota_error(quota_id: str, delay: str = "37s") -> errors.ClientError:
    return errors.ClientError(
        429,
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "You exceeded your current quota.",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota_id}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
                ],
            }
        },
    )


def daily_quota_error():
    return _quota_error("GenerateRequestsPerDayPerProjectPerModel-FreeTier")


def per_minute_error():
    return _quota_error("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")


def invalid_key_error():
    return errors.ClientError(
        400,
        {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "API key not valid.",
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}
                ],
            }
        },
    )


def overloaded_error():
    return errors.ServerError(
        503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "overloaded"}}
    )


def edital_json(nome: str = "Edital FAPES 10/2026") -> str:
    return json.dumps(
        {
            "nome": nome,
            "descrição": "Apoio a médicos empreendedores.",
            "orgão_fomento": "FAPES",
            "categoria": "inovação",
            "status": "aberto",
            "data_abertura": "2026-05-04",
            "data_encerramento": "2026-07-07",
            "cronograma": json.loads(FIXTURE.read_text(encoding="utf-8")),
            "tags": ["empreendedorismo", "saúde", "inovação"],
        },
        ensure_ascii=False,
    )


class FakeClock:
    """Relógio que só anda quando alguém espera."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self._lock = threading.Lock()

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        with self._lock:
            self.sleeps.append(seconds)
            self.now += seconds


class FakeModels:
    """`client.models` falso: cada chamada pede a resposta a `responder(n)`."""

    def __init__(self, responder, clock=None):
        self.responder = responder
        self.clock = clock
        self.calls = []
        self._lock = threading.Lock()

    def generate_content(self, model, contents, config=None):
        with self._lock:
            self.calls.append(
                {"model": model, "contents": contents, "config": config,
                 "at": self.clock() if self.clock else None}
            )
            n = len(self.calls)
        answer = self.responder(n)
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(text=answer)


class FakeGenAIClient:
    def __init__(self, responder, clock=None):
        self.models = FakeModels(responder, clock)


def make_service(responder, clock=None, **kwargs):
    clock = clock or FakeClock()
    client = FakeGenAIClient(responder, clock)
    service = GeminiExtractionService(client=client, clock=clock, sleep=clock.sleep, **kwargs)
    return service, client, clock


def pdf_edital(url="https://fapes.example/edital-10-2026.pdf", agency="FAPES"):
    return RawEdital(
        title="Edital FAPES 10/2026",
        url=url,
        raw_agency=agency,
        pdf_content=b"%PDF-1.4 falso",
        document_type="edital",
    )


@pytest.fixture
def ctx():
    return {}


@pytest.fixture
def pdf_text(monkeypatch):
    def set_text(text):
        monkeypatch.setattr(extraction_prompts, "extract_text_with_pdfplumber", lambda _: text)

    return set_text


# ---- Given ---------------------------------------------------------------


@given("the PDF text of an edital")
def given_pdf_text(pdf_text):
    pdf_text(PDF_TEXT)


@given("a PDF without extractable text")
def given_pdf_without_text(pdf_text):
    pdf_text("   ")


@given("a Gemini client that answers with the FAPES 10/2026 schedule")
def given_fapes_client(ctx):
    ctx["service"], ctx["client"], ctx["clock"] = make_service(lambda n: edital_json())


@given("a Gemini client whose daily quota is exhausted")
def given_daily_quota_client(ctx):
    ctx["service"], ctx["client"], ctx["clock"] = make_service(lambda n: daily_quota_error())


@given(parsers.parse('the AI is unavailable because "{reason}"'))
def given_unavailable(ctx, reason):
    ctx["service"] = UnavailableExtractionService(reason)


@given(parsers.parse("a CAPES batch of {count:d} PDF editais"))
def given_capes_batch(ctx, count):
    items = [pdf_edital(url=f"https://capes.example/edital-{i}.pdf", agency="CAPES") for i in range(count)]
    ctx["source"] = SimpleNamespace(read=lambda: list(items), last_listing_count=count)


@given(parsers.parse("a Gemini client whose daily quota runs out on call {limit:d}"))
def given_quota_runs_out(ctx, pdf_text, limit):
    pdf_text(PDF_TEXT)
    ctx["service"], ctx["client"], ctx["clock"] = make_service(
        lambda n: edital_json(f"Edital CAPES {n}") if n < limit else daily_quota_error()
    )


@given("no AI configuration")
def given_no_configuration():
    pass  # o conftest já limpa LLM_PROVIDER, GEMINI_API_KEY e MISTRAL_API_KEY


# ---- When ----------------------------------------------------------------


@when("the Gemini service extracts the edital")
def when_extract(ctx):
    ctx["result"] = ctx["service"].extract_from_pdf(b"%PDF-1.4 falso", "edital.pdf")


@when("the Gemini service extracts the edital twice")
def when_extract_twice(ctx):
    ctx["errors"] = []
    for _ in range(2):
        with pytest.raises(AIUnavailableError) as raised:
            ctx["service"].extract_from_pdf(b"%PDF-1.4 falso", "edital.pdf")
        ctx["errors"].append(raised.value)


@when(parsers.parse("the normalizer processes the edital on {today}"))
@when(parsers.parse("the normalizer processes a PDF edital on {today}"))
def when_normalizer_processes(ctx, today):
    ctx["normalizer"] = EditalNormalizer(
        extraction_service=ctx["service"], today=date.fromisoformat(today)
    )
    ctx["raw"] = pdf_edital()
    ctx["result"] = ctx["normalizer"].process(ctx["raw"])


@when(parsers.parse("the CAPES pipeline runs on {today}"))
def when_capes_pipeline(ctx, today, tmp_path, monkeypatch, capsys):
    from src.flows import ingest_capes_flow
    from src.processed_store import get_keys_set

    recorded = {}
    monkeypatch.setattr(ingest_capes_flow.rejection_store, "get_active_keys", lambda source: set())
    monkeypatch.setattr(
        ingest_capes_flow.rejection_store, "record", lambda source, rejections: recorded.update(rejections)
    )
    written = {}

    def write(items):
        persisted = {item.link: item for item in items}
        written.update(persisted)
        return persisted

    index_path = str(tmp_path / "processed.json")
    transform = EditalNormalizer(extraction_service=ctx["service"], today=date.fromisoformat(today))
    ingest_capes_flow.run_pipeline(
        source=ctx["source"],
        transform=transform,
        sink=SimpleNamespace(write=write),
        processed_index_path=index_path,
    )
    ctx.update(
        transform=transform,
        written=written,
        rejected=recorded,
        processed=get_keys_set("capes", path=index_path),
        output=capsys.readouterr().out,
    )


@when("the extraction service is requested")
def when_factory(ctx):
    ctx["service"] = get_extraction_service()


# ---- Then ----------------------------------------------------------------


@then("the edital keeps the schedule with the call of each stage")
def then_schedule_kept(ctx):
    cronograma = ctx["result"].cronograma
    assert len(cronograma) == len(json.loads(FIXTURE.read_text(encoding="utf-8")))
    assert all(stage.get("chamada") for stage in cronograma)


@then("the request used the model gemini-3.5-flash-lite as JSON with temperature 0")
def then_request_config(ctx):
    call = ctx["client"].models.calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    assert call["config"].response_mime_type == "application/json"
    assert call["config"].temperature == 0


@then("the request carried the PDF text and the shared extraction instructions")
def then_request_content(ctx):
    call = ctx["client"].models.calls[0]
    assert PDF_TEXT in call["contents"]
    assert "Encerramento de <etapa>" in call["contents"]
    assert call["config"].system_instruction == extraction_prompts.SYSTEM_EXTRACTION


@then("no edital is returned")
def then_none(ctx):
    assert ctx["result"] is None


@then(parsers.parse("the AI was called {count:d} times"))
def then_call_count(ctx, count):
    assert len(ctx["client"].models.calls) == count


@then(parsers.parse("the closing date is {closing} and the opening date is {opening}"))
def then_dates(ctx, closing, opening):
    assert ctx["result"].data_encerramento == closing
    assert ctx["result"].data_abertura == opening


@then(parsers.parse("the edital has {count:d} calls"))
def then_calls(ctx, count):
    assert len(ctx["result"].chamadas) == count


@then(parsers.parse('both extractions report the AI as unavailable because "{reason}"'))
def then_both_unavailable(ctx, reason):
    assert [e.reason for e in ctx["errors"]] == [reason, reason]


@then("the edital URL is deferred")
def then_deferred(ctx):
    assert ctx["normalizer"].deferred == [ctx["raw"].url]


@then("nothing is recorded as rejected")
def then_not_rejected(ctx):
    assert ctx["normalizer"].rejections == {}


@then(parsers.parse("{count:d} editais are written"))
def then_written(ctx, count):
    assert len(ctx["written"]) == count


@then(parsers.parse("{count:d} editais are deferred and kept out of both registries"))
def then_deferred_out_of_registries(ctx, count):
    deferred = set(ctx["transform"].deferred)
    assert len(deferred) == count
    assert not deferred & ctx["processed"]
    assert ctx["processed"] == set(ctx["written"])
    assert ctx["rejected"] == {}


@then(parsers.parse("the flow stats report {count:d} deferred items"))
def then_stats(ctx, count):
    assert f"deferred={count}" in ctx["output"]


@then(parsers.parse('the service is unavailable because "{reason}"'))
def then_factory_unavailable(ctx, reason):
    assert isinstance(ctx["service"], UnavailableExtractionService)
    assert reason in ctx["service"].reason


# ---- Limites (D4) --------------------------------------------------------


class TestLimits:
    def test_per_minute_429_is_retried_after_the_indicated_delay(self, pdf_text):
        pdf_text(PDF_TEXT)
        service, client, clock = make_service(
            lambda n: per_minute_error() if n == 1 else edital_json()
        )
        assert service.extract_from_pdf(b"%PDF", "a.pdf") is not None
        assert 37 in clock.sleeps
        assert len(client.models.calls) == 2

    def test_persistent_per_minute_429_becomes_daily_quota(self, pdf_text, monkeypatch):
        pdf_text(PDF_TEXT)
        monkeypatch.setenv("GEMINI_MAX_RETRIES", "2")
        service, client, _ = make_service(lambda n: per_minute_error())
        with pytest.raises(AIUnavailableError) as raised:
            service.extract_from_pdf(b"%PDF", "a.pdf")
        assert raised.value.reason == gemini_client.REASON_PERSISTENT_RATE_LIMIT
        assert len(client.models.calls) == 3

    def test_total_wait_never_exceeds_the_cap(self, pdf_text, monkeypatch):
        pdf_text(PDF_TEXT)
        monkeypatch.setenv("GEMINI_MAX_TOTAL_WAIT_SEC", "60")
        monkeypatch.setenv("GEMINI_MIN_INTERVAL_SEC", "0")
        service, _, clock = make_service(lambda n: per_minute_error())
        with pytest.raises(AIUnavailableError):
            service.extract_from_pdf(b"%PDF", "a.pdf")
        assert sum(clock.sleeps) <= 60

    def test_invalid_key_makes_the_ai_unavailable(self, pdf_text):
        pdf_text(PDF_TEXT)
        service, _, _ = make_service(lambda n: invalid_key_error())
        with pytest.raises(AIUnavailableError) as raised:
            service.extract_from_pdf(b"%PDF", "a.pdf")
        assert raised.value.reason == gemini_client.REASON_INVALID_KEY

    def test_unavailability_is_logged_once(self, pdf_text, caplog):
        pdf_text(PDF_TEXT)
        service, _, _ = make_service(lambda n: daily_quota_error())
        for _ in range(3):
            with pytest.raises(AIUnavailableError):
                service.extract_from_pdf(b"%PDF", "a.pdf")
        assert caplog.text.count("IA indisponível no resto desta execução") == 1

    def test_transient_503_is_retried_then_fails_only_the_item(self, pdf_text, monkeypatch):
        pdf_text(PDF_TEXT)
        monkeypatch.setenv("GEMINI_MAX_RETRIES", "1")
        service, client, _ = make_service(lambda n: overloaded_error())
        assert service.extract_from_pdf(b"%PDF", "a.pdf") is None
        assert service.unavailable_reason is None
        assert len(client.models.calls) == 2

    def test_calls_keep_the_minimum_interval(self, pdf_text):
        pdf_text(PDF_TEXT)
        service, _, clock = make_service(lambda n: edital_json())
        service.extract_from_pdf(b"%PDF", "a.pdf")
        service.extract_from_pdf(b"%PDF", "b.pdf")
        assert clock.sleeps == [gemini_client.DEFAULT_MIN_INTERVAL_SEC]

    def test_minimum_interval_holds_across_threads(self, pdf_text):
        pdf_text(PDF_TEXT)
        service, client, _ = make_service(lambda n: edital_json())
        threads = [
            threading.Thread(target=service.extract_from_pdf, args=(b"%PDF", f"{i}.pdf"))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        instants = sorted(call["at"] for call in client.models.calls)
        assert len(instants) == 4
        assert all(
            b - a >= gemini_client.DEFAULT_MIN_INTERVAL_SEC for a, b in zip(instants, instants[1:])
        )

    def test_the_key_never_appears_in_logs(self, pdf_text, caplog, monkeypatch):
        pdf_text(PDF_TEXT)
        clock = FakeClock()
        service = GeminiExtractionService(api_key=FAKE_KEY, clock=clock, sleep=clock.sleep)
        errors_in_order = iter([overloaded_error(), invalid_key_error()])
        service.client = FakeGenAIClient(lambda n: next(errors_in_order, daily_quota_error()))
        monkeypatch.setenv("GEMINI_MAX_RETRIES", "0")
        service.max_retries = 0
        assert service.extract_from_pdf(b"%PDF", "a.pdf") is None
        with pytest.raises(AIUnavailableError):
            service.extract_from_pdf(b"%PDF", "b.pdf")
        service.classify_document_titles(["Edital 1"])
        service.categorize_finep_by_description("descrição")
        assert caplog.text
        assert FAKE_KEY not in caplog.text


class TestApiErrorParsing:
    @pytest.mark.parametrize(
        "error, kind",
        [
            (daily_quota_error(), "daily"),
            (per_minute_error(), "per_minute"),
            (errors.ClientError(429, {"error": {"code": 429, "message": "busy"}}), "per_minute"),
            (invalid_key_error(), "auth"),
            (errors.ClientError(403, {"error": {"code": 403, "status": "PERMISSION_DENIED"}}), "auth"),
            (overloaded_error(), "transient"),
            (TimeoutError("lento"), "transient"),
            (errors.ClientError(400, {"error": {"code": 400, "message": "bad"}}), "item"),
            (ValueError("x"), "item"),
        ],
    )
    def test_classify_api_error(self, error, kind):
        assert classify_api_error(error) == kind

    def test_retry_delay_reads_retry_info(self):
        assert retry_delay_seconds(per_minute_error()) == 37.0
        assert retry_delay_seconds(overloaded_error()) == gemini_client.DEFAULT_RETRY_DELAY_SEC


# ---- Leitura do edital (US1) ---------------------------------------------


class TestExtraction:
    def test_invalid_json_is_an_extraction_failure(self, pdf_text, caplog):
        pdf_text(PDF_TEXT)
        service, _, _ = make_service(lambda n: "não é json")
        assert service.extract_from_pdf(b"%PDF", "a.pdf") is None
        assert "não é JSON" in caplog.text

    def test_gemini_model_env_overrides_the_default(self, pdf_text, monkeypatch):
        pdf_text(PDF_TEXT)
        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.5-flash")
        service, client, _ = make_service(lambda n: edital_json())
        service.extract_from_pdf(b"%PDF", "a.pdf")
        assert client.models.calls[0]["model"] == "gemini-3.5-flash"

    def test_invalid_number_in_env_falls_back_to_default(self, monkeypatch, caplog):
        monkeypatch.setenv("GEMINI_MIN_INTERVAL_SEC", "rápido")
        service, _, _ = make_service(lambda n: edital_json())
        assert service._limiter.min_interval == gemini_client.DEFAULT_MIN_INTERVAL_SEC
        assert "GEMINI_MIN_INTERVAL_SEC inválido" in caplog.text


# ---- Escolha do provedor (US3) -------------------------------------------


class TestProviderChoice:
    def test_default_provider_is_gemini(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
        monkeypatch.setattr(gemini_client.genai, "Client", lambda **kwargs: SimpleNamespace(models=None))
        assert isinstance(get_extraction_service(), GeminiExtractionService)

    @pytest.mark.parametrize("value", ["mistral", "MISTRAL", " Mistral "])
    def test_mistral_is_chosen_by_configuration(self, monkeypatch, value):
        from src.components.transforms import mistral_client

        monkeypatch.setenv("LLM_PROVIDER", value)
        monkeypatch.setenv("MISTRAL_API_KEY", FAKE_KEY)
        monkeypatch.setattr(mistral_client, "Mistral", lambda api_key: SimpleNamespace())
        assert isinstance(get_extraction_service(), mistral_client.MistralExtractionService)

    def test_mistral_without_key_is_unavailable(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "mistral")
        service = get_extraction_service()
        assert isinstance(service, UnavailableExtractionService)
        assert "MISTRAL_API_KEY" in service.reason

    def test_unknown_provider_is_unavailable_with_a_clear_error(self, monkeypatch, caplog):
        monkeypatch.setenv("LLM_PROVIDER", "xyz")
        service = get_extraction_service()
        assert isinstance(service, UnavailableExtractionService)
        assert service.reason == "provedor de IA desconhecido: xyz"
        assert caplog.text.count("provedor de IA desconhecido: xyz") == 1

    def test_same_instance_per_process(self):
        assert get_extraction_service() is get_extraction_service()

    def test_both_providers_send_the_same_extraction_instructions(self, pdf_text):
        from src.components.transforms.mistral_client import MistralExtractionService

        pdf_text(PDF_TEXT)
        service, client, _ = make_service(lambda n: edital_json())
        service.extract_from_pdf(b"%PDF", "a.pdf")
        mistral_prompt = MistralExtractionService(api_key="x")._get_extraction_prompt(PDF_TEXT)
        assert client.models.calls[0]["contents"] == mistral_prompt

    def test_unavailable_service_keeps_the_fallbacks(self):
        service = UnavailableExtractionService("cota diária esgotada")
        assert service.classify_document_titles(["Anexo I - Formulário"]) == {
            "Anexo I - Formulário": "anexo"
        }
        assert service.categorize_finep_by_description("algo") == ""


# ---- Títulos da FAPES e categoria FINEP (US4) ----------------------------


class TestOtherUses:
    def test_gemini_classifies_fapes_document_titles(self):
        answer = {"Edital 10/2026": "edital", "Anexo I": "anexo"}
        service, client, _ = make_service(lambda n: json.dumps(answer))
        assert service.classify_document_titles(list(answer)) == answer
        assert client.models.calls[0]["config"].system_instruction == extraction_prompts.SYSTEM_CLASSIFY

    def test_title_classification_falls_back_when_the_ai_is_unavailable(self):
        service, _, _ = make_service(lambda n: daily_quota_error())
        titles = ["Edital 10/2026", "Anexo I - Formulário", "Retificação 1"]
        assert service.classify_document_titles(titles) == {
            "Edital 10/2026": "edital",
            "Anexo I - Formulário": "anexo",
            "Retificação 1": "alteração",
        }

    def test_empty_title_list_does_not_call_the_ai(self):
        service, client, _ = make_service(lambda n: "{}")
        assert service.classify_document_titles([]) == {}
        assert client.models.calls == []

    def test_gemini_categorizes_a_finep_call(self):
        service, _, _ = make_service(lambda n: json.dumps({"categoria": "Extensão"}))
        assert service.categorize_finep_by_description("Projetos extensionistas") == "extensão"

    def test_finep_category_is_empty_when_the_ai_fails(self):
        service, _, _ = make_service(lambda n: daily_quota_error())
        assert service.categorize_finep_by_description("Projetos") == ""


# ---- Caso real (integration) ---------------------------------------------


@pytest.mark.integration
def test_fapes_10_2026_real_pdf_text_with_gemini():
    """Quickstart, passo 2: texto local do PDF real + Gemini real (SC-001)."""
    import requests
    from dotenv import dotenv_values

    key = (dotenv_values(".env").get("GEMINI_API_KEY") or "").strip()
    if not key:
        pytest.skip("GEMINI_API_KEY ausente no .env")
    try:
        response = requests.get(FAPES_10_PDF_URL, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        pytest.skip(f"sem acesso ao PDF do FAPES 10/2026: {exc}")

    normalizer = EditalNormalizer(
        extraction_service=GeminiExtractionService(api_key=key), today=date(2026, 11, 25)
    )
    raw = pdf_edital(url=FAPES_10_PDF_URL)
    raw.pdf_content = response.content
    edital = normalizer.process(raw)

    assert edital is not None
    assert edital.data_encerramento == "2026-12-15"
    assert edital.data_abertura == "2026-10-14"
    assert len(edital.chamadas) == 2
