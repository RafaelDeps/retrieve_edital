"""
Recomendação semanal de editais a professores no Telegram (spec 012).

Horizon, embeddings e Telegram falsos. Os vetores falsos contam temas (gênero,
IA, energia, café, química) com uma base pequena comum, para a semelhança ser
previsível. Nada abre rede.
"""

import json
import shutil
import unicodedata
from datetime import date
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from src.components.transforms.extraction_service import AIUnavailableError
from src.flows import recommend_flow
from src.recommendation import horizon_profiles as hp
from src.recommendation.embeddings import IEmbeddingService
from src.recommendation.message import PART_LIMIT
from src.recommendation.sent_store import FILENAME as SENT_FILENAME
from src.recommendation.telegram_client import TelegramError

scenarios("../../docs/features/recomendacao_telegram.feature")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "recomendacao"
TOPICS = (
    ("genero", "violencia", "sociolog", "mulheres"),
    ("inteligencia", "artificial", "learning", "cyber", "neura", "intrusion"),
    ("energia", "solar", "eletric", "renovav"),
    ("cafe", "agro", "agricult", "cultivo", "coffee"),
    ("quimic", "materia", "catalis"),
)


def _plain(text):
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


class TopicEmbedder(IEmbeddingService):
    def __init__(self, fail_after_calls=None):
        self.calls = 0
        self.fail_after_calls = fail_after_calls

    def embed(self, texts):
        if self.fail_after_calls is not None and self.calls >= self.fail_after_calls:
            raise AIUnavailableError("cota diária esgotada")
        self.calls += 1
        out = []
        for text in texts:
            plain = _plain(text)
            out.append([0.05] + [float(sum(plain.count(k) for k in topic)) for topic in TOPICS])
        return out


class FakeTelegram:
    def __init__(self, can_send=True, fail=False):
        self.can_send, self.fail, self.sent = can_send, fail, []

    def send(self, text):
        if self.fail:
            raise TelegramError("HTTP 403 Forbidden")
        self.sent.append(text)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    output = tmp_path / "output"
    shutil.copytree(FIXTURES / "editais", output)
    records = json.loads((FIXTURES / "professores.json").read_text(encoding="utf-8"))
    return {"output": output, "registry": tmp_path / "registry", "records": records,
            "telegram": FakeTelegram(), "embedder": TopicEmbedder(), "runs": [], "loader": None}


def _edital(path, slug, nome, desc, fim, fonte):
    data = json.loads((FIXTURES / "editais" / "fapes_ia.json").read_text(encoding="utf-8"))
    data.update(nome=nome, descrição=desc, data_encerramento=fim, fonte_key=fonte,
                link=f"https://exemplo/{slug}", elegibilidade=None, tags=[])
    (path / f"{slug}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---- Given -----------------------------------------------------------------


@given("the professors of campus Serra from the Horizon fixture")
def professors(ctx):
    ctx["loader"] = lambda: (hp.professors_from_records(ctx["records"], "Serra"), hp.ORIGIN_HORIZON)


@given("the editais of the fixture")
def editais(ctx):
    assert len(list(ctx["output"].glob("*.json"))) == 8


@given(parsers.parse('the edital "{slug}" requires "{degree}"'))
def requires(ctx, slug, degree):
    path = ctx["output"] / f"{slug}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["elegibilidade"] = {"titulacao_minima": degree, "areas_conhecimento": [], "requisitos_resumo": ""}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@given(parsers.parse("{n:d} extra Brazilian editais and {m:d} extra Horizon calls about artificial intelligence"))
def many(ctx, n, m):
    for i in range(n):
        _edital(ctx["output"], f"br{i}", f"EDITAL EXTRA {i} DE INTELIGÊNCIA ARTIFICIAL",
                "Projetos de inteligência artificial e aprendizado de máquina " * 5, "2027-01-15", "cnpq")
    for i in range(m):
        _edital(ctx["output"], f"hz{i}", f"HORIZON AI CALL {i}", "Artificial intelligence and machine learning",
                "2027-02-15", "horizon")


@given("the Telegram group refuses the message")
def refuses(ctx):
    ctx["telegram"] = FakeTelegram(fail=True)


@given("the Horizon export is unavailable and there is no previous copy")
def horizon_down(ctx):
    ctx["loader"] = lambda: ([], hp.ORIGIN_NONE)


@given("the AI quota runs out after the professors")
def quota(ctx):
    # Os vetores dos professores já estão no cache (semana anterior); a cota
    # acaba na primeira chamada desta semana, a dos editais.
    ctx["embedder"] = TopicEmbedder(fail_after_calls=0)
    ctx["embedder_split"] = True


# ---- When ------------------------------------------------------------------


def _run(ctx, today, telegram):
    embedder = ctx["embedder"]
    if ctx.get("embedder_split"):
        # Calcula antes os vetores dos professores, como numa semana anterior.
        from src.recommendation.embeddings import CACHE_FILENAME, EmbeddingCache

        cache = EmbeddingCache(str(ctx["registry"] / CACHE_FILENAME))
        cache.vectors_for([p.texto for p in ctx["loader"]()[0]], TopicEmbedder(), date.fromisoformat(today))
        cache.save()
    code = recommend_flow.run(
        today=date.fromisoformat(today), profiles_loader=ctx["loader"], embedder=embedder,
        telegram=telegram, output_dir=str(ctx["output"]), registry_dir=str(ctx["registry"]),
    )
    ctx["runs"].append({"code": code, "sent": list(telegram.sent)})
    telegram.sent.clear()


@when(parsers.parse("the weekly recommendation runs on {today} with the Telegram group"))
def run_with_group(ctx, today):
    _run(ctx, today, ctx["telegram"])


@when(parsers.parse("the weekly recommendation runs on {today} without credentials"))
def run_preview(ctx, today):
    ctx["telegram"] = FakeTelegram(can_send=False)
    _run(ctx, today, ctx["telegram"])


# ---- Then ------------------------------------------------------------------


def _message(ctx, index=0):
    return "\n".join(ctx["runs"][index]["sent"])


def _block(message, fragment):
    for block in message.split("\n\n"):
        if fragment in block:
            return block
    return ""


@then(parsers.parse('the message recommends "{prof}" for "{fragment}"'))
def recommends(ctx, prof, fragment):
    assert prof in _block(_message(ctx), fragment)


@then(parsers.parse('"{prof}" is not recommended for "{fragment}"'))
def not_recommended(ctx, prof, fragment):
    assert prof not in _block(_message(ctx), fragment)


@then(parsers.parse('the reason of "{prof}" mentions "{fragment}"'))
def reason_mentions(ctx, prof, fragment):
    line = next(line for line in _message(ctx).splitlines() if prof in line)
    assert fragment in line


@then(parsers.parse('the message says "{text}"'))
def message_says(ctx, text):
    assert text in _message(ctx)


@then(parsers.parse('the message does not mention "{text}"'))
def not_mention(ctx, text):
    assert text not in _message(ctx)


@then("the Horizon section comes after the Brazilian editais")
def horizon_after(ctx):
    message = _message(ctx)
    assert message.index("VIOLÊNCIA DE GÊNERO") < message.index("Horizon Europe") < message.index("CYBERSECURITY")


@then("the Horizon section has at most 5 editais")
def horizon_max(ctx):
    message = _message(ctx)
    section = message[message.index("Horizon Europe"):]
    assert section.count("Ver edital") <= 5


@then(parsers.parse('the second message says "{text}"'))
def second_says(ctx, text):
    assert text in _message(ctx, 1)


@then(parsers.parse('the second message reminds "{fragment}"'))
def second_reminds(ctx, fragment):
    message = _message(ctx, 1)
    assert fragment in message[message.index("Fecham nos próximos"):]


@then(parsers.parse("every part has at most {limit:d} characters and is numbered"))
def parts_ok(ctx, limit):
    parts = ctx["runs"][0]["sent"]
    assert len(parts) > 1 and limit == PART_LIMIT
    for i, part in enumerate(parts, 1):
        assert len(part) <= limit and part.endswith(f"(Parte {i}/{len(parts)})")


@then(parsers.parse("nothing was sent and the exit code is {code:d}"))
def nothing_sent(ctx, code):
    assert ctx["runs"][0]["sent"] == [] and ctx["runs"][0]["code"] == code


@then(parsers.parse("the exit code is {code:d}"))
def exit_code(ctx, code):
    assert ctx["runs"][0]["code"] == code


@then("no edital is marked as sent")
def none_marked(ctx):
    path = ctx["registry"] / SENT_FILENAME
    assert not path.exists() or json.loads(path.read_text(encoding="utf-8")) == {}
