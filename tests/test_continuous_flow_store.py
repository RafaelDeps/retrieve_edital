"""Índice registry/fluxo_continuo.json (spec 010)."""

import json
from datetime import date

import pytest

from src import continuous_flow_store as store

TODAY = date(2026, 11, 26)


def test_load_missing_or_invalid_file(tmp_path):
    assert store.load(str(tmp_path / "nao_existe.json")) == {}
    broken = tmp_path / "fluxo_continuo.json"
    broken.write_text("{ quebrado", encoding="utf-8")
    assert store.load(str(broken)) == {}


def test_record_writes_and_clears(tmp_path):
    path = str(tmp_path / "fluxo_continuo.json")
    store.record({"a": "sem-prazo", "b": "declarada"}, today=TODAY, path=path)
    store.record({}, cleared=["a"], today=TODAY, path=path)
    data = json.loads((tmp_path / "fluxo_continuo.json").read_text(encoding="utf-8"))
    assert data == {"b": {"origem": "declarada", "lido_em": "2026-11-26"}}


def test_path_next_to_processed_index(tmp_path):
    assert store.path_next_to(str(tmp_path / "processed.json")) == str(tmp_path / "fluxo_continuo.json")
    assert store.path_next_to("registry/processed_editais.json") == "registry/fluxo_continuo.json"


@pytest.mark.parametrize(
    "entry, esperado",
    [
        (None, True),
        ({"origem": "sem-prazo", "lido_em": "2026-11-19"}, True),  # 7 dias exatos
        ({"origem": "sem-prazo", "lido_em": "2026-11-20"}, False),
        ({"origem": "declarada", "lido_em": "2026-01-01"}, False),
        ({"origem": "sem-prazo", "lido_em": "data ruim"}, True),
    ],
)
def test_due_for_reread(entry, esperado):
    assert store.due_for_reread(entry, TODAY, 7) is esperado


def test_reread_days_env(monkeypatch):
    monkeypatch.delenv("RELEITURA_FLUXO_CONTINUO_DIAS", raising=False)
    assert store.reread_days() == 7
    monkeypatch.setenv("RELEITURA_FLUXO_CONTINUO_DIAS", "14")
    assert store.reread_days() == 14
    monkeypatch.setenv("RELEITURA_FLUXO_CONTINUO_DIAS", "0")
    assert store.reread_days() == 7
    monkeypatch.setenv("RELEITURA_FLUXO_CONTINUO_DIAS", "sete")
    assert store.reread_days() == 7


def test_closing_date_beats_a_declaration_read_in_the_document():
    """Validação real (26/11/2026): editais com prazo saíam como fluxo contínuo."""
    from src.components.transforms.publication_rules import resolve_modalidade_and_origin
    from src.domain.models import EditalDomain, RawEdital

    def edital(closing, modalidade="fluxo-contínuo", nome="Edital nº 11/2026"):
        return EditalDomain(nome=nome, descrição="", orgão_fomento="CAPES", categoria="pesquisa",
                            status="aberto", data_abertura="", data_encerramento=closing, link="x",
                            cronograma=[], tags=[], modalidade=modalidade)

    raw = RawEdital(title="t", url="x", raw_agency="CAPES")
    assert resolve_modalidade_and_origin(edital("2026-12-28"), raw, True) == ("", "")
    assert resolve_modalidade_and_origin(edital(""), raw, True) == ("fluxo-contínuo", "declarada")
    assert resolve_modalidade_and_origin(edital("", modalidade=""), raw, True) == ("fluxo-contínuo", "sem-prazo")
    named = edital("2026-12-01", modalidade="", nome="Subvenção em fluxo contínuo")
    assert resolve_modalidade_and_origin(named, raw, True) == ("fluxo-contínuo", "declarada")


def test_retire_links_removes_support_and_closes_expired(tmp_path):
    from src.components.sinks.json_sink import LocalJSONSink
    from src.flows.common import retire_refused

    for name, link in (("apoio", "https://a"), ("vencido", "https://v"), ("outro", "https://o")):
        (tmp_path / f"{name}.json").write_text(json.dumps({"link": link, "status": "aberto"}), encoding="utf-8")
    retire_refused(LocalJSONSink(str(tmp_path)), {
        "https://a": "documento de apoio, não é um edital",
        "https://v": "prazo encerrado em 2026-10-20",
    })
    assert not (tmp_path / "apoio.json").exists()
    assert json.loads((tmp_path / "vencido.json").read_text(encoding="utf-8"))["status"] == "encerrado"
    assert json.loads((tmp_path / "outro.json").read_text(encoding="utf-8"))["status"] == "aberto"
