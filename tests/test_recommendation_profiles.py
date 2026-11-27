"""Perfis dos professores a partir do Horizon ETL (spec 012)."""

import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest
import requests

from src.recommendation import horizon_profiles as hp

FIXTURES = Path(__file__).parent / "fixtures" / "recomendacao"


def records():
    return json.loads((FIXTURES / "professores.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "education, esperado",
    [
        ([{"degree": "Doutorado", "course_name": "Química", "end_year": 2010}], ("doutorado", "Química")),
        ([{"degree": "Mestrado profissional", "course_name": "Ensino", "end_year": 2018}], ("mestrado", "Ensino")),
        ([{"degree": "Mestrado", "course_name": "A", "end_year": 2010},
          {"degree": "Doutorado", "course_name": "B", "end_year": None}], ("mestrado", "A")),
        ([{"degree": "Curso técnico/profissionalizante", "course_name": "Mecânica", "end_year": 2000}],
         ("ensino_medio", "Mecânica")),
        ([], (None, "")),
    ],
)
def test_degree_of(education, esperado):
    assert hp.degree_of(education) == esperado


def test_campus_filter_and_profiles():
    profs = hp.professors_from_records(records(), "Serra")
    assert [p.nome for p in profs] == ["Ana Souza", "Bruno Lima", "Carla Mendes", "Diego Rocha", "Elisa Prado"]
    ana = profs[0]
    assert ana.titulacao == "doutorado" and ana.curso_titulacao == "Sociologia" and ana.tem_lattes
    # As tags são a base do perfil (pedido do responsável, 27/11/2026).
    assert ana.texto.startswith("Temas: Sociologia") and "Projetos: Observatório da violência de gênero no ES" in ana.texto
    assert len(ana.hash) == 64
    diego = profs[3]
    assert diego.titulacao is None and not diego.tem_lattes
    assert "Agronomia e Agroecologia" in diego.itens


def test_profile_text_is_capped():
    record = {"name": "X", "campus": {"name": "Serra"}, "resume": "a" * 10000}
    assert len(hp.build_professor(record).texto) == hp.PROFILE_MAX_CHARS


def _zip(data):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(hp.RESEARCHERS_MEMBER, json.dumps(data))
    return buffer.getvalue()


class FakeSession:
    def __init__(self, content=None, error=None):
        self.content, self.error = content, error

    def get(self, url, timeout):
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code, response._content = 200, self.content
        return response


def test_download_reads_researchers_projects_and_groups():
    data = hp.download_researchers("https://x", session=FakeSession(_zip(records())))
    assert data == {"researchers": records(), "initiatives": [], "groups": []}


def test_tags_come_from_groups_and_projects():
    record = {"name": "Sem Lattes", "campus": {"name": "Serra"},
              "initiatives": [{"id": 7, "name": "Correção automática de redações"}],
              "research_groups": [{"id": 3, "name": "Estudos comparatistas"}]}
    initiatives = [{"id": 7, "description": "Usa modelos de linguagem.",
                    "knowledge_areas": [{"name": "Processamento de Linguagem Natural"}, {"name": "Educação"}]}]
    groups = [{"id": 3, "description": "Grupo de literatura.", "knowledge_areas": [{"name": "Educação"}]}]
    prof = hp.professors_from_records([record], "Serra", initiatives, groups)[0]
    assert prof.temas == ["Educação", "Processamento de Linguagem Natural"]  # a mais frequente primeiro
    assert prof.texto.startswith("Temas: Educação; Processamento de Linguagem Natural")
    assert "Usa modelos de linguagem." in prof.texto and "Grupo de literatura." in prof.texto


def test_good_download_saves_the_reserve_and_failure_uses_it(tmp_path):
    ok = lambda: hp.download_researchers("https://x", session=FakeSession(_zip(records())))
    profs, origem = hp.load_professors(str(tmp_path), date(2026, 11, 30), ok, "Serra")
    assert origem == hp.ORIGIN_HORIZON and len(profs) == 5
    assert (tmp_path / hp.RESERVE_FILENAME).exists()

    def fails():
        raise requests.ConnectionError("fora do ar")

    profs2, origem2 = hp.load_professors(str(tmp_path), date(2026, 12, 7), fails, "Serra")
    assert origem2 == hp.ORIGIN_RESERVE and [p.nome for p in profs2] == [p.nome for p in profs]


def test_failure_without_reserve_gives_nothing(tmp_path):
    def fails():
        raise requests.ConnectionError("fora do ar")

    assert hp.load_professors(str(tmp_path), date(2026, 11, 30), fails, "Serra") == ([], hp.ORIGIN_NONE)


def test_text_repairs_from_the_real_preview():
    assert hp.fix_text("InovaÃ§Ã£o e saÃºde") == "Inovação e saúde"
    assert hp.clean_course("Curso de Doutorado em Educação") == "Educação"
    assert hp.clean_course("Doctorado en Educacion") == "Educacion"
    assert hp.clean_course("Mestrado em Informática") == "Informática"
