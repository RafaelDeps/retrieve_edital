"""
Data de referência injetada no EditalNormalizer.

O normalizador decide publicação e status pelo prazo. Sem uma data de
referência injetável, todo teste que passa por ele depende do dia em que roda
(Constituição, Princípio VI).
"""

from datetime import date
from unittest.mock import MagicMock

from src.components.transforms.edital_normalizer import EditalNormalizer
from src.domain.models import RawEdital

EDITAL_URL = "https://exemplo.gov.br/editais/chamada-01-2026"
DEADLINE = "2026-11-01"
BEFORE_DEADLINE = date(2026, 6, 1)
AFTER_DEADLINE = date(2026, 12, 1)


def _normalizer(today=None):
    service = MagicMock()
    service.extract_from_pdf.return_value = None
    return EditalNormalizer(extraction_service=service, today=today)


def _raw_edital():
    return RawEdital(
        title="Chamada 01/2026 - Apoio a projetos",
        url=EDITAL_URL,
        raw_agency="FAPES",
        raw_description="Apoio financeiro a projetos de pesquisa aplicada.",
        raw_cronograma=[
            {"evento": "início das inscrições", "data": "2026-03-30"},
            {"evento": "encerramento das inscrições", "data": DEADLINE},
        ],
    )


def test_today_before_deadline_publishes_as_open():
    edital = _normalizer(today=BEFORE_DEADLINE).process(_raw_edital())

    assert edital is not None
    assert edital.data_encerramento == DEADLINE
    assert edital.status == "aberto"


def test_today_after_deadline_rejects_with_reason():
    normalizer = _normalizer(today=AFTER_DEADLINE)

    assert normalizer.process(_raw_edital()) is None
    assert "prazo encerrado" in normalizer.rejections[EDITAL_URL]


def test_without_today_keeps_real_clock_default():
    normalizer = _normalizer()

    assert normalizer.today is None
