"""Campos novos da resposta da IA (spec 010): tipo de documento e fluxo contínuo."""

import json

import pytest

from src.components.transforms import extraction_prompts as ep
from src.components.transforms.gemini_client import GeminiExtractionService


def test_document_rule_is_always_in_the_prompt():
    prompt = ep.build_extraction_prompt("Edital nº 1")
    assert '"tipo_documento"' in prompt and '"fluxo_continuo"' in prompt
    assert prompt.rstrip().endswith("Edital nº 1")


def test_combined_rule_only_with_headers():
    assert "VÁRIOS DOCUMENTOS" not in ep.build_extraction_prompt("Edital simples")
    combined = "=== EDITAL ORIGINAL: E ===\nx\n\n=== ALTERAÇÃO 1: A ===\ny"
    assert "VÁRIOS DOCUMENTOS" in ep.build_extraction_prompt(combined)


@pytest.mark.parametrize(
    "data, esperado",
    [
        ({"tipo_documento": "apoio"}, "apoio"),
        ({"tipo_documento": "Apoio"}, "apoio"),
        ({"tipo_documento": "edital"}, "edital"),
        ({}, "edital"),
        ({"tipo_documento": 3}, "edital"),
        ([], "edital"),
    ],
)
def test_document_kind(data, esperado):
    assert ep.document_kind(data) == esperado


@pytest.mark.parametrize(
    "data, text, esperado",
    [
        ({"fluxo_continuo": True}, "", True),
        ({"fluxo_continuo": False}, "", False),
        ({"fluxo_continuo": "sim"}, "", False),
        ({}, "As propostas podem ser enviadas em FLUXO CONTÍNUO.", True),
        # "A qualquer tempo" é cláusula comum ("poderá, a qualquer tempo, revogar"):
        # só a IA decide (validação real de 26/11/2026).
        ({}, "A CAPES poderá, a qualquer tempo, revogar este edital.", False),
        ({}, "Chamada sem prazo de encerramento definido.", True),
        ({}, "Fluxo-contínuo", True),
        ({}, "Prazo: 10/12/2026.", False),
    ],
)
def test_declares_continuous_flow(data, text, esperado):
    assert ep.declares_continuous_flow(data, text) is esperado


def test_map_to_domain_sets_modality_only_with_declaration():
    base = {"nome": "Edital X", "cronograma": []}
    assert ep.map_to_domain(base).modalidade == ""
    assert ep.map_to_domain({**base, "fluxo_continuo": True}).modalidade == "fluxo-contínuo"
    assert ep.map_to_domain(base, "submissão em fluxo contínuo").modalidade == "fluxo-contínuo"


class _FakeGemini(GeminiExtractionService):
    def __init__(self, response):
        self._response = response

    def _generate(self, system, prompt, context):
        self.prompt = prompt
        return json.dumps(self._response)


def test_gemini_raises_for_supporting_document():
    service = _FakeGemini({"nome": "Diretrizes", "tipo_documento": "apoio"})
    with pytest.raises(ep.SupportingDocumentError) as error:
        service.extract_from_text("Diretrizes FAPES para o Horizonte Europa", "d.pdf")
    assert error.value.motivo == "documento de apoio, não é um edital"


def test_gemini_marks_continuous_flow_from_text():
    service = _FakeGemini({"nome": "Chamada", "cronograma": []})
    edital = service.extract_from_text("Propostas recebidas em fluxo contínuo.", "c.pdf")
    assert edital.modalidade == "fluxo-contínuo"


def test_eligibility_prompt_explains_any_area_and_degree_held():
    """Validação real (26/11/2026): "qualquer área" saía como Multidisciplinar."""
    prompt = ep.build_extraction_prompt("Edital")
    assert '["Todas as áreas"]' in prompt
    assert "declaradamente multi ou interdisciplinar" in prompt
    assert "JÁ POSSUI" in prompt
    assert '["Todas as áreas"]' in ep.build_eligibility_prompt("Descrição")


def test_eligibility_prompt_asks_areas_by_theme():
    """Validação real: edital temático saía com todas as áreas."""
    prompt = ep.build_extraction_prompt("Edital")
    assert "TEMA do edital" in prompt and "SÓ quando o edital não tiver tema próprio" in prompt
    assert "consulte o portal" in prompt
