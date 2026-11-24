"""
Testes do teto de espera do backoff do Mistral.

Contar tentativas não limita duração: dez retentativas dobrando a partir de 60s
somavam mais de 17 horas numa única chamada, muito além de qualquer janela de job.
O corte passou a ser por tempo acumulado.
"""

from types import SimpleNamespace
import json
import pytest

from src.components.transforms import mistral_client
from src.components.transforms.mistral_client import (
    DEFAULT_LLM_MODEL,
    RATE_LIMIT_MAX_TOTAL_WAIT_SEC,
    MistralExtractionService,
    _call_with_rate_limit_retry,
    _is_non_retryable_quota_or_tier_error,
    _is_rate_limit_error,
)


@pytest.fixture
def esperas(monkeypatch):
    """Captura as esperas sem realmente dormir."""
    registradas = []
    monkeypatch.setattr(mistral_client.time, "sleep", registradas.append)
    return registradas


class TestRateLimitDetection:
    @pytest.mark.parametrize(
        "mensagem",
        ["Status 429", "rate_limited", "Rate limit exceeded"],
    )
    def test_recognises_rate_limit_errors(self, mensagem):
        assert _is_rate_limit_error(RuntimeError(mensagem)) is True

    def test_other_errors_are_not_rate_limits(self):
        assert _is_rate_limit_error(RuntimeError("Status 401 Unauthorized")) is False


class TestCumulativeWaitCap:
    def test_total_wait_never_exceeds_the_cap(self, esperas):
        def sempre_429():
            raise RuntimeError("Status 429 rate_limited")

        with pytest.raises(RuntimeError):
            _call_with_rate_limit_retry(sempre_429, context="teste")

        assert sum(esperas) <= RATE_LIMIT_MAX_TOTAL_WAIT_SEC
        # Sem o teto, dez tentativas dobrando de 60s passariam de 17 horas.
        assert sum(esperas) < 17 * 3600

    def test_last_wait_is_shortened_to_fit_the_cap(self, esperas):
        def sempre_429():
            raise RuntimeError("Status 429")

        with pytest.raises(RuntimeError):
            _call_with_rate_limit_retry(sempre_429)

        # A soma encosta no teto sem passar: a última espera foi encurtada.
        assert sum(esperas) == pytest.approx(RATE_LIMIT_MAX_TOTAL_WAIT_SEC)

    def test_success_after_a_retry_does_not_raise(self, esperas):
        tentativas = {"n": 0}

        def falha_uma_vez():
            tentativas["n"] += 1
            if tentativas["n"] == 1:
                raise RuntimeError("Status 429")
            return "ok"

        assert _call_with_rate_limit_retry(falha_uma_vez) == "ok"
        assert esperas == [60]

    def test_non_rate_limit_error_is_raised_without_waiting(self, esperas):
        def falha_401():
            raise RuntimeError("Status 401 Unauthorized")

        with pytest.raises(RuntimeError, match="401"):
            _call_with_rate_limit_retry(falha_401)
        assert esperas == []

    def test_zero_quota_rate_limit_fails_fast_without_waiting(self, esperas):
        class FakeZeroQuotaError(RuntimeError):
            status_code = 429
            headers = {"x-ratelimit-limit-req-minute": "0", "x-ratelimit-remaining-req-minute": "0"}

        def falha_limite_zero():
            raise FakeZeroQuotaError("Status 429 Rate limit exceeded")

        assert _is_non_retryable_quota_or_tier_error(FakeZeroQuotaError("Status 429")) is True
        assert _is_rate_limit_error(FakeZeroQuotaError("Status 429")) is False

        with pytest.raises(FakeZeroQuotaError):
            _call_with_rate_limit_retry(falha_limite_zero, context="OCR teste")
        assert esperas == []

    def test_tier_not_allowed_403_fails_fast_without_waiting(self, esperas):
        class FakeTierError(RuntimeError):
            status_code = 403

        def falha_403():
            raise FakeTierError('Status 403 {"type":"tier_not_allowed","code":"1910"}')

        assert _is_non_retryable_quota_or_tier_error(FakeTierError("tier_not_allowed")) is True
        with pytest.raises(FakeTierError):
            _call_with_rate_limit_retry(falha_403, context="LLM teste")
        assert esperas == []


class TestModelAndOcrFallback:
    def test_default_llm_model_and_env_override(self, monkeypatch):
        monkeypatch.setattr(mistral_client, "Mistral", lambda api_key: SimpleNamespace())
        monkeypatch.delenv("MISTRAL_LLM_MODEL", raising=False)
        svc = MistralExtractionService(api_key="fake-key")
        assert svc.llm_model == DEFAULT_LLM_MODEL == "ministral-14b-latest"

        monkeypatch.setenv("MISTRAL_LLM_MODEL", "ministral-8b-latest")
        svc_override = MistralExtractionService(api_key="fake-key")
        assert svc_override.llm_model == "ministral-8b-latest"

    def test_ocr_zero_quota_falls_back_to_pdfplumber(self, monkeypatch, esperas):
        class FakeZeroQuotaError(RuntimeError):
            status_code = 429
            headers = {"x-ratelimit-limit-req-minute": "0"}

        deleted = []
        ocr_calls = {"n": 0}

        class FakeClient:
            class files:
                @staticmethod
                def upload(file, purpose):
                    return SimpleNamespace(id="file-123")

                @staticmethod
                def delete(file_id):
                    deleted.append(file_id)

            class ocr:
                @staticmethod
                def process(model, document):
                    ocr_calls["n"] += 1
                    raise FakeZeroQuotaError("Status 429 Rate limit exceeded")

            class chat:
                @staticmethod
                def complete(model, messages, response_format):
                    payload = {
                        "nome": "Edital Teste",
                        "descrição": "Objeto do edital",
                        "orgão_fomento": "FAPES",
                        "categoria": "pesquisa",
                        "status": "aberto",
                        "data_abertura": "2026-11-01",
                        "data_encerramento": "2026-12-31",
                        "cronograma": [],
                        "tags": ["fapes", "pesquisa", "teste"],
                    }
                    return SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
                    )

        monkeypatch.setattr(mistral_client, "Mistral", lambda api_key: FakeClient())
        monkeypatch.setattr(
            mistral_client,
            "_extract_text_with_pdfplumber",
            lambda pdf_bytes: "Texto extraído localmente do PDF",
        )

        svc = MistralExtractionService(api_key="fake-key")
        domain = svc.extract_from_pdf(b"%PDF-1.4 fake", "edital.pdf")
        assert domain is not None
        assert domain.nome == "EDITAL TESTE"
        assert svc._ocr_unavailable is True
        assert deleted == ["file-123"]
        # Segunda chamada já vai direto para pdfplumber sem chamar OCR da API
        domain2 = svc.extract_from_pdf(b"%PDF-1.4 fake", "edital2.pdf")
        assert domain2 is not None
        assert ocr_calls["n"] == 1
        # Apenas os sleeps curtos de 2s antes do LLM (nenhum backoff de 60s/120s)
        assert esperas == [2, 2]
