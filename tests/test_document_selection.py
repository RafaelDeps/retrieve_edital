"""Escolha do documento que a IA lê (spec 010): títulos reais da CAPES e da FINEP."""

import json
from pathlib import Path

import pytest

from src.components.sources.document_selection import (
    PAGE_HEADER,
    ROLE_ALTERACAO,
    ROLE_APOIO,
    ROLE_EDITAL,
    classify_attachment,
    combine_texts,
    max_amendments,
    select_documents,
)

FIXTURES = Path(__file__).parent / "fixtures" / "prazos"


def anexo(titulo, link="https://www.gov.br/capes/x.pdf/@@display-file/file"):
    return {"titulo": titulo, "link": link, "tipo": "pdf"}


# Listas reais (26/11/2026), na ordem da página: da mais nova para a mais antiga.
CATEDRA = [
    anexo("Alteração Edital nº 11 - Programa Cátedra Jorge Amado, formato, pdf, 5", "https://www.gov.br/capes/2845089_sei_2843522_edital_n__11_2026.pdf/@@display-file/file"),
    anexo("Edital nº 11/2026 - Programa Cátedra Jorge Amado, formato, pdf, 196kb", "https://www.gov.br/capes/l_2806958_SEI_2806040_Edital_11_2026.pdf/@@display-file/file"),
    anexo("Modelo carta do orientador brasileiro proficiência, formato - pdf, 80k"),
    anexo("file", "https://www.gov.br/capes/Declaracao_Fluencia_Orient_Brasileiro_2.pdf/@@display-file/file"),
    anexo("Termo evento cátedra, formato - pdf, 121kb"),
    anexo("Termo de Outorga PVE exterior, formato - pdf, 173kb"),
]
PAEP = [
    anexo("Manual de inscrição do PAEP, formato, pdf, 2,5mb"),
    anexo("Alteração - Edital nº 25/2026 - Edição 2027, formato, pdf, 52,4kb"),
    anexo("Alteração - Edital nº 25/2026 - Edição 2027, formato, pdf, 119kb", "https://www.gov.br/capes/870_edital_25_2026_paep_alteracao_ii.pdf/@@display-file/file"),
    anexo("Alteração - Edital nº 25/2026 - Edição 2027, formato, pdf, 50,3kb"),
    anexo("Edital nº 25/2026 - Edição 2027, formato, pdf, 145kb"),
    anexo("Resultado final eventos apoiados do Edital PAEP nº 19/2025 - formato"),
    anexo("Alteração do edital PAEP nº 19/2025 - formato, pdf, 51kb"),
    anexo("Alteração do edital PAEP nº 19/2025 - formato, pdf, 49kb"),
]


@pytest.mark.parametrize(
    "titulo, esperado",
    [
        ("Edital nº 11/2026 - Programa Cátedra Jorge Amado, formato, pdf, 196kb", ROLE_EDITAL),
        ("Alteração Edital nº 21/2026 - Programa de Desenvolvimento da Pós-Graduação", ROLE_ALTERACAO),
        ("Resultado Final do Edital nº 16/2026 - Programa AURORA", ROLE_APOIO),
        ("Portaria nº 129, de 25 de março de 2026", ROLE_APOIO),
        ("TERMO DE OUTORGA E ACEITE DE BOLSA", ROLE_APOIO),
        ("Termo de Outorga PVE exterior, formato - pdf, 173kb", ROLE_APOIO),
        ("Manual de Inscrição no Sistema SICAPES, formato, 758kb", ROLE_APOIO),
        ("Edital", ROLE_EDITAL),
        ("Chamada internacional", ROLE_EDITAL),
        ("Regulamento Chamada Globalstars Brasil - EUREKA", ROLE_EDITAL),
        ("Rerratificação - Edital rerratificado", ROLE_ALTERACAO),
        ("2ª Rerratificação - documento rerratificado", ROLE_ALTERACAO),
        ("Aviso de 2ª rerratificação", ROLE_APOIO),
        ("FAQ - Perguntas frequentes - Atualizado em 25/07/2024", ROLE_APOIO),
        ("Decisão TRF1 -Agravo de instrumento", ROLE_APOIO),
        ("Anexo 3 Rerratificado - TERMO DE OUTORGA DE SUBVENÇÃO ECONÔMICA", ROLE_APOIO),
        ("Diretrizes FAPES para participação no Horizonte Europa", ROLE_APOIO),
        ("Anexo I", ROLE_APOIO),
        ("Anexo 6 Rerratificado – ESPECIFICAÇÕES TÉCNICAS DO DESAFIO", ROLE_APOIO),
        ("Anexos do Edital FIP Bioeconomia e Sustentabilidade", ROLE_APOIO),
    ],
)
def test_classify_attachment_real_titles(titulo, esperado):
    assert classify_attachment(titulo) == esperado


BUFFALO = [  # página real, da mais nova para a mais antiga
    anexo("Termo de Outorga e Aceite de Bolsa - formato, pdf, 280kb"),
    anexo("Edital nº 19/2026 - Programa CAPES/Universidade em Buffalo (CAPES/UB) - Professor Visitante",
          "https://www.gov.br/capes/Edital_2843097_SEI_2842333_Edital_n__19_2026.pdf/@@display-file/file"),
    anexo("Edital nº 19/2026 - Lista de inscritos - Programa CAPES/Universidade em Buffalo",
          "https://www.gov.br/capes/lista_listagem_2892345_lista_de_inscritos.pdf/@@display-file/file"),
    anexo("Portaria nº 289, de 28 de dezembro de 2018  - formato, pdf, 557kb"),
    anexo("Resultado final do edital nº 16/2025 - formato, pdf, 55kb"),
    anexo("Edital nº 16/2025 - formato, pdf, 360kb", "https://www.gov.br/capes/11082025_Edital_2654124.pdf/@@display-file/file"),
]


def test_buffalo_reads_the_most_recent_edital():
    original, _ = select_documents(BUFFALO, newest_first=True)
    assert original["titulo"].startswith("Edital nº 19/2026 - Programa")


def test_aurora_act_is_not_an_edital():
    assert classify_attachment(
        "Ato nº 3/2026 - Programa AURORA", "https://www.gov.br/capes/editais/edital_n__3.pdf/@@display-file/file"
    ) == ROLE_APOIO
    assert classify_attachment("Contrato nº 3 do edital") == ROLE_EDITAL


def test_classify_uses_the_file_name():
    assert classify_attachment("file", "https://x/Declaracao_Fluencia.pdf/@@display-file/file") == ROLE_APOIO
    assert classify_attachment("file", "https://x/edital_n__3.pdf/@@display-file/file") == ROLE_EDITAL


def test_catedra_original_comes_before_the_amendment():
    original, alteracoes = select_documents(CATEDRA, newest_first=True)
    assert original["titulo"].startswith("Edital nº 11/2026")
    assert [a["titulo"][:24] for a in alteracoes] == ["Alteração Edital nº 11 -"]


def test_paep_keeps_only_amendments_after_the_original_in_order():
    original, alteracoes = select_documents(PAEP, newest_first=True, limit=5)
    assert original["titulo"].startswith("Edital nº 25/2026")
    assert [a["titulo"][-6:].strip() for a in alteracoes] == ["50,3kb", "119kb", "52,4kb"]


def test_amendment_limit_keeps_the_most_recent():
    _, alteracoes = select_documents(PAEP, newest_first=True, limit=2)
    assert [a["titulo"][-6:].strip() for a in alteracoes] == ["119kb", "52,4kb"]


def test_amendment_without_original_is_read_alone():
    original, alteracoes = select_documents([anexo("Alteração Edital nº 21/2026")], newest_first=True)
    assert original["titulo"] == "Alteração Edital nº 21/2026"
    assert alteracoes == []


def test_termo_de_outorga_is_never_chosen():
    original, alteracoes = select_documents(
        [anexo("file", "https://x/TERMO_DE_OUTORGA_E_ACEITE_DE_BOLSA__r1.pdf"), anexo("Termo de Outorga PVE")],
        newest_first=True,
    )
    assert (original, alteracoes) == (None, [])


def test_finep_lists_in_publication_order():
    anexos = json.loads((FIXTURES / "finep_anexos_fip_nordeste.json").read_text(encoding="utf-8"))["anexos"]
    original, alteracoes = select_documents(anexos, newest_first=False)
    assert original["titulo"] == "Edital"
    assert [a["titulo"] for a in alteracoes] == ["Rerratificação - Edital rerratificado"] * 2


def test_finep_subvencao_real_attachments():
    anexos = json.loads((FIXTURES / "finep_anexos_subvencao.json").read_text(encoding="utf-8"))["anexos"]
    # Sem um "Edital" simples: vale a rerratificação mais recente, que é o edital
    # consolidado. "Anexo 6 Rerratificado" é anexo, não alteração.
    original, alteracoes = select_documents(anexos, newest_first=False)
    assert original["titulo"] == "3ª Rerratificação - Edital rerratificado"
    assert alteracoes == []


def test_max_amendments_env(monkeypatch):
    monkeypatch.setenv("MAX_ALTERACOES_POR_EDITAL", "5")
    assert max_amendments() == 5
    monkeypatch.setenv("MAX_ALTERACOES_POR_EDITAL", "zero")
    assert max_amendments() == 3
    monkeypatch.setenv("MAX_ALTERACOES_POR_EDITAL", "-1")
    assert max_amendments() == 3


def test_combine_texts_order_and_headers():
    text, used = combine_texts(
        ("Edital 11", "prazo 10/12"), [("Alt A", "prorroga A"), ("Alt B", "prorroga B")], "aviso da página"
    )
    assert used == 2
    assert text.index("=== EDITAL ORIGINAL: Edital 11") < text.index("=== ALTERAÇÃO 1: Alt A")
    assert text.index("=== ALTERAÇÃO 2: Alt B") < text.index(PAGE_HEADER)


def test_combine_texts_cuts_oldest_amendments_then_page_never_original():
    original = ("Edital", "O" * 50)
    alteracoes = [("Antiga", "A" * 50), ("Nova", "N" * 50)]
    text, used = combine_texts(original, alteracoes, "P" * 50, max_chars=240)
    assert used == 1 and "Nova" in text and "Antiga" not in text and PAGE_HEADER in text
    text, used = combine_texts(original, alteracoes, "P" * 50, max_chars=90)
    assert used == 0 and PAGE_HEADER not in text and "O" * 50 in text
