"""Regras puras da elegibilidade (spec 011)."""

import pytest

from src.components.transforms import eligibility_rules as er


@pytest.mark.parametrize(
    "valor, esperado",
    [
        ("Ciências da Saúde", ["Ciências da Saúde"]),
        ("ciencias da saude", ["Ciências da Saúde"]),
        ("Saúde", ["Ciências da Saúde"]),
        ("Engenharia", ["Engenharias"]),
        ("Linguística, Letras e Artes", ["Linguística, Letras e Artes"]),
        ("Interdisciplinar", ["Multidisciplinar"]),
        ("Ciência da Computação", []),  # 2º nível: fora da lista
        ("", []),
        (None, []),
    ],
)
def test_normalize_area(valor, esperado):
    assert er.normalize_area(valor) == esperado


def test_all_areas_gives_the_nine():
    assert er.normalize_areas(["Todas as áreas"]) == list(er.GRANDES_AREAS)
    assert len(er.GRANDES_AREAS) == 9


def test_areas_without_duplicates_in_official_order():
    assert er.normalize_areas(["Engenharias", "saúde", "Engenharia", "xyz"]) == [
        "Engenharias",
        "Ciências da Saúde",
    ]


@pytest.mark.parametrize(
    "valor, esperado",
    [("doutorado", "doutorado"), ("Doutor", "doutorado"), ("mestre", "mestrado"),
     ("Ensino médio", "ensino_medio"), ("pós-doutorado", ""), ("", ""), (None, "")],
)
def test_normalize_titulacao(valor, esperado):
    assert er.normalize_titulacao(valor) == esperado


def test_normalize_publico_keeps_only_the_new_vocabulary():
    assert er.normalize_publico(["Docente", "pesquisador", "pos-graduando", "aluno", "docente"]) == [
        "pos_graduando",
        "pesquisador",
        "docente",
    ]


@pytest.mark.parametrize(
    "antigo, novo",
    [
        (["pesquisador", "internacional"], ["pesquisador"]),
        (["ict-empresa"], ["empresa", "ict"]),
        (["estudante"], ["estudante_graduacao", "pos_graduando"]),
        (["empresa", "startup"], ["empresa", "startup"]),
        ([], []),
    ],
)
def test_convert_legacy_publico(antigo, novo):
    assert er.convert_legacy_publico(antigo) == novo


def test_clean_summary_cuts_at_a_sentence():
    texto = "Coordenador com doutorado. " * 30
    resumo = er.clean_summary(texto)
    assert len(resumo) <= er.REQUISITOS_MAX_CHARS and resumo.endswith(".")
    assert er.clean_summary("  curto   demais ") == "curto demais"


def test_build_eligibility_always_has_the_three_keys():
    vazio = {"titulacao_minima": "", "areas_conhecimento": [], "requisitos_resumo": ""}
    assert er.build_eligibility({}) == vazio
    assert er.build_eligibility(None) == vazio
    assert er.build_eligibility({"elegibilidade": "texto"}) == vazio
    assert er.build_eligibility({"elegibilidade": {
        "titulacao_minima": "Doutorado", "areas_conhecimento": ["Saúde", "Inventada"],
        "requisitos_resumo": "Vínculo com ICT."}}) == {
        "titulacao_minima": "doutorado", "areas_conhecimento": ["Ciências da Saúde"],
        "requisitos_resumo": "Vínculo com ICT."}


def test_is_eligibility():
    assert er.is_eligibility(er.build_eligibility({}))
    assert not er.is_eligibility(None)
    assert not er.is_eligibility({"titulacao_minima": ""})


def test_curate_converts_legacy_public(tmp_path):
    """Spec 011: o realinhamento diário converte o público dos arquivos gravados."""
    import json
    from datetime import date
    from scripts.curate_output import curate

    out = tmp_path / "data" / "output"
    out.mkdir(parents=True)
    base = {"descrição": "x", "cronograma": [{"evento": "Encerramento", "data": "2026-12-20"}],
            "data_encerramento": "2026-12-20", "status": "aberto", "categoria": "pesquisa",
            "orgão_fomento": "HORIZON EUROPE", "fonte_key": "horizon", "nome": "Chamada"}
    (out / "a.json").write_text(json.dumps({**base, "link": "https://a",
                                            "publico_alvo": ["pesquisador", "internacional"]}), encoding="utf-8")
    (out / "b.json").write_text(json.dumps({**base, "link": "https://b", "publico_alvo": ["ict-empresa"]}),
                                encoding="utf-8")
    curate(tmp_path, apply=True, refresh_status_only=False, today=date(2026, 11, 26))
    assert json.loads((out / "a.json").read_text(encoding="utf-8"))["publico_alvo"] == ["pesquisador"]
    assert json.loads((out / "b.json").read_text(encoding="utf-8"))["publico_alvo"] == ["empresa", "ict"]


@pytest.mark.parametrize("texto", [
    "Consulte o portal Funding & Tenders para verificar os requisitos específicos.",
    "Participação em conformidade com as regras gerais do programa Horizon Europe.",
    "Empresas e instituições elegíveis conforme as condições estabelecidas no edital.",
])
def test_generic_summaries_become_empty(texto):
    """Validação real (26/11/2026): resumos genéricos não dizem requisito nenhum."""
    assert er.clean_summary(texto) == ""


def test_multidisciplinar_alone_means_any_area():
    """Validação real (27/11/2026): "Multidisciplinar" sozinha era "qualquer área"."""
    assert er.normalize_areas(["Multidisciplinar"]) == list(er.GRANDES_AREAS)
    assert er.normalize_areas(["Ciências Humanas", "Multidisciplinar"]) == [
        "Ciências Humanas",
        "Multidisciplinar",
    ]
