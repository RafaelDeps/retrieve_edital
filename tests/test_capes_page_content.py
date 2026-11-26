"""Área de conteúdo das páginas reais da CAPES no gov.br (spec 010)."""

from pathlib import Path

import pytest

from src.components.sources.capes_source import page_content_text, short_description

FIXTURES = Path(__file__).parent / "fixtures" / "prazos"


def page(name):
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["catedra_jorge_amado", "premio_capes_tese", "programa_aurora"])
def test_content_has_no_menu_header_or_footer(name):
    text = page_content_text(page(name))
    assert text
    for noise in ("Ir para o conteúdo", "Botão Menu", "Redefinir Cookies", "Compartilhe", "Publicado em"):
        assert noise not in text


def test_premio_tese_starts_with_the_description():
    assert page_content_text(page("premio_capes_tese")).startswith("O Prêmio CAPES de Tese reconhece")


def test_aurora_keeps_the_closing_notice():
    assert "ficam encerrados os ciclos mensais" in page_content_text(page("programa_aurora"))


def test_page_without_main_gives_empty_text():
    assert page_content_text("<html><body><p>Ir para o conteúdo</p></body></html>") == ""


def test_short_description_cuts_at_a_sentence():
    text = "Primeira frase longa o bastante. " * 80
    short = short_description(text, max_chars=200)
    assert len(short) <= 200 and short.endswith(".")
    assert short_description("curta") == "curta"
