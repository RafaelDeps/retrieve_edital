"""Regras da escolha, da mensagem e do envio (spec 012)."""

from datetime import date

import pytest
import requests

from src.recommendation import matcher, message
from src.recommendation.horizon_profiles import Professor
from src.recommendation.telegram_client import TelegramClient, TelegramError

TODAY = date(2026, 11, 30)


def prof(nome="Ana", titulacao="doutorado", curso="Sociologia", itens=(), lattes=True):
    return Professor(nome, "Serra", titulacao, curso, lattes, list(itens), f"texto {nome}", nome)


def test_relative_threshold(monkeypatch):
    monkeypatch.delenv("RECOMENDACAO_DESVIOS", raising=False)
    assert matcher.threshold([0.0, 0.0, 0.0]) == 0.0
    assert matcher.threshold([-0.1, 0.0, 0.1]) == pytest.approx(1.5 * 0.0816, abs=1e-3)  # padrão: médio
    monkeypatch.setenv("RECOMENDACAO_DESVIOS", "1")
    assert matcher.threshold([-0.1, 0.0, 0.1]) == pytest.approx(0.0816, abs=1e-3)
    monkeypatch.setenv("RECOMENDACAO_DESVIOS", "2")
    assert matcher.threshold([-0.1, 0.0, 0.1]) == pytest.approx(0.1633, abs=1e-3)


def test_generic_profile_does_not_win_everything(monkeypatch):
    """Prévia real (27/11/2026): um perfil "parecido com tudo" aparecia em todos os editais."""
    monkeypatch.setenv("RECOMENDACAO_LIMITE_MINIMO", "0.1")
    monkeypatch.setenv("RECOMENDACAO_DESVIOS", "1")
    gen = prof("Genérico", itens=["Saúde e energia"])
    saude, energia = prof("Saúde", itens=["Saúde pública"]), prof("Energia", itens=["Energia solar"])
    e1 = {"nome": "Saúde", "status": "aberto", "data_encerramento": "2026-12-30", "link": "1"}
    e2 = {"nome": "Energia", "status": "aberto", "data_encerramento": "2026-12-30", "link": "2"}
    k = matcher.text_key
    vectors = {
        k(gen.texto): [0.7, 0.7], k(saude.texto): [1.0, 0.05], k(energia.texto): [0.05, 1.0],
        k(matcher.edital_text(e1)): [1.0, 0.1], k(matcher.edital_text(e2)): [0.1, 1.0],
    }
    recs = {r.edital["link"]: [i.professor.nome for i in r.indicacoes]
            for r in matcher.recommend([e1, e2], [gen, saude, energia], vectors, TODAY)}
    assert recs == {"1": ["Saúde"], "2": ["Energia"]}


def test_single_new_edital_uses_the_reference_for_centering(monkeypatch):
    monkeypatch.setenv("RECOMENDACAO_LIMITE_MINIMO", "0.1")
    monkeypatch.setenv("RECOMENDACAO_DESVIOS", "1")
    saude, energia = prof("Saúde", itens=["Saúde pública"]), prof("Energia", itens=["Energia solar"])
    e1 = {"nome": "Saúde", "status": "aberto", "data_encerramento": "2026-12-30", "link": "1"}
    e2 = {"nome": "Energia", "status": "aberto", "data_encerramento": "2026-12-30", "link": "2"}
    k = matcher.text_key
    vectors = {k(saude.texto): [1.0, 0.05], k(energia.texto): [0.05, 1.0],
               k(matcher.edital_text(e1)): [1.0, 0.1], k(matcher.edital_text(e2)): [0.1, 1.0]}
    assert matcher.recommend([e1], [saude, energia], vectors, TODAY) == []  # sem referência, zera
    rec = matcher.recommend([e1], [saude, energia], vectors, TODAY, reference=[e1, e2])
    assert [i.professor.nome for i in rec[0].indicacoes] == ["Saúde"]


@pytest.mark.parametrize(
    "titulacao, minima, esperado",
    [
        ("doutorado", "doutorado", (True, "")),
        ("mestrado", "doutorado", (False, "")),
        ("mestrado", "", (True, "")),
        (None, "doutorado", (True, matcher.NO_DEGREE_WARNING)),
        (None, "", (True, matcher.NO_DEGREE_WARNING)),
    ],
)
def test_degree_ok(titulacao, minima, esperado):
    assert matcher.degree_ok(prof(titulacao=titulacao), minima) == esperado


def test_reason_uses_degree_and_closest_item():
    p = prof(itens=["Robótica educacional", "Observatório da violência de gênero no ES"])
    edital = {"nome": "Chamada sobre violência de gênero", "descrição": ""}
    assert matcher.reason(p, edital) == 'Doutorado em Sociologia; "Observatório da violência de gênero no ES"'


def test_reason_without_degree_and_cut():
    p = prof(titulacao=None, curso="", itens=["Violência " + "x" * 200])
    texto = matcher.reason(p, {"nome": "violência", "descrição": ""})
    assert len(texto) <= matcher.REASON_MAX_CHARS and texto.endswith("…")


def test_tie_puts_lattes_first(monkeypatch):
    monkeypatch.setenv("RECOMENDACAO_LIMITE_MINIMO", "0.1")
    monkeypatch.setenv("RECOMENDACAO_DESVIOS", "0.5")
    a = prof("A", lattes=False, titulacao=None, itens=["Robótica móvel"])
    b, c = prof("B", itens=["Robótica móvel"]), prof("C", itens=["Química"])
    e1 = {"nome": "Robótica", "status": "aberto", "data_encerramento": "2026-12-30", "link": "x"}
    e2 = {"nome": "E2", "status": "aberto", "data_encerramento": "2026-12-30", "link": "y"}
    k = matcher.text_key
    vectors = {k(a.texto): [1.0, 0.0], k(b.texto): [1.0, 0.0], k(c.texto): [0.0, 1.0],
               k(matcher.edital_text(e1)): [1.0, 0.0], k(matcher.edital_text(e2)): [0.0, 1.0]}
    rec = matcher.recommend([e1], [a, b, c], vectors, TODAY, reference=[e1, e2])[0]
    assert [i.professor.nome for i in rec.indicacoes] == ["B", "A"]


def test_edital_eligibility():
    base = {"status": "aberto"}
    assert matcher.eligible_edital({**base, "data_encerramento": "2026-12-03"}, TODAY, 3)
    assert not matcher.eligible_edital({**base, "data_encerramento": "2026-12-02"}, TODAY, 3)
    assert matcher.eligible_edital({**base, "modalidade": "fluxo-contínuo"}, TODAY, 3)
    assert not matcher.eligible_edital({"status": "encerrado", "data_encerramento": "2027-01-01"}, TODAY, 3)


def test_split_parts_never_cuts_a_block():
    blocks = [f"bloco {i} " + "x" * 900 for i in range(10)]
    parts = message.split_parts(blocks, limit=2000)
    assert all(len(p) <= 2000 for p in parts)
    assert sum(p.count("bloco ") for p in parts) == 10
    assert parts[-1].endswith(f"(Parte {len(parts)}/{len(parts)})")
    assert message.split_parts(["curto"]) == ["curto"]


def test_escape_html_in_names():
    rec = matcher.Recomendacao(
        {"nome": "A <b> & B", "status": "aberto", "data_encerramento": "2026-12-20", "link": "https://x?a=1&b=2"},
        [matcher.Indicacao(prof("Zé <script>"), 0.9, "Projeto <x>")],
    )
    block = message.edital_block(rec)
    assert "A &lt;b&gt; &amp; B" in block and "Zé &lt;script&gt;" in block and 'href="https://x?a=1&amp;b=2"' in block


class FakeResponse:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, data, timeout):
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_telegram_sends_and_retries():
    session = FakeSession([requests.ConnectionError(), FakeResponse(200)])
    TelegramClient("TOKEN", "-1", session=session, sleep=lambda s: None).send("oi")
    assert len(session.calls) == 2


def test_telegram_error_never_shows_the_token(caplog):
    session = FakeSession([FakeResponse(403, {"description": "Forbidden"})] * 2)
    with pytest.raises(TelegramError) as error:
        TelegramClient("SEGREDO123", "-1", session=session, sleep=lambda s: None).send("oi")
    assert "403" in str(error.value)
    assert "SEGREDO123" not in str(error.value) and "SEGREDO123" not in caplog.text


def test_telegram_without_credentials_cannot_send(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert not TelegramClient().can_send


def test_weekly_workflow():
    with open(".github/workflows/run_recomendacao_weekly.yml", encoding="utf-8") as handle:
        text = handle.read()
    assert 'cron: "0 11 * * 1"' in text and "workflow_dispatch:" in text
    for secret in ("GEMINI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        assert f"secrets.{secret}" in text
    for path in ("registry/recomendacoes_enviadas.json", "registry/embeddings_cache.json",
                 "registry/horizon_researchers_serra.json"):
        assert path in text
    assert "group: data-commit-${{ github.ref }}" in text


def test_reason_prefers_matching_tags():
    p = prof(itens=["Projeto qualquer"])
    p.temas = ["Inteligência Artificial", "Saúde", "Robótica"]
    texto = matcher.reason(p, {"nome": "IA na saúde", "descrição": "inteligência artificial para saúde"})
    assert texto == "Doutorado em Sociologia; temas: Inteligência Artificial, Saúde"


def test_a_tag_needs_half_of_its_words():
    p = prof(itens=[])
    p.temas = ["Processamento de Linguagem Natural", "Hierarquia de gênero"]
    temas, _ = matcher.evidence(p, {"nome": "Agrofloresta e recursos naturais", "descrição": "solo natural"})
    assert temas == []
    temas, _ = matcher.evidence(p, {"nome": "Violência de gênero", "descrição": ""})
    assert temas == ["Hierarquia de gênero"]


def test_no_evidence_no_recommendation(monkeypatch):
    monkeypatch.setenv("RECOMENDACAO_LIMITE_MINIMO", "0.1")
    a, c = prof("A", itens=["Robótica"]), prof("C", itens=["Química"])
    e1 = {"nome": "Agricultura", "status": "aberto", "data_encerramento": "2026-12-30", "link": "x"}
    e2 = {"nome": "Outro", "status": "aberto", "data_encerramento": "2026-12-30", "link": "y"}
    k = matcher.text_key
    vectors = {k(a.texto): [1.0, 0.0], k(c.texto): [0.0, 1.0],
               k(matcher.edital_text(e1)): [1.0, 0.0], k(matcher.edital_text(e2)): [0.0, 1.0]}
    assert matcher.recommend([e1], [a, c], vectors, TODAY, reference=[e1, e2]) == []
