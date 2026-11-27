"""Vetores de significado com cache (spec 012)."""

import json
from datetime import date

import pytest

from src.components.transforms.extraction_service import AIUnavailableError
from src.recommendation import embeddings as em

TODAY = date(2026, 11, 30)


class FakeService(em.IEmbeddingService):
    def __init__(self, fail_after=None):
        self.calls = []
        self.fail_after = fail_after

    def embed(self, texts):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise AIUnavailableError("cota diária esgotada")
        self.calls.append(list(texts))
        return [[float(len(t) % 7), 1.0, 0.5] for t in texts]


def test_float16_round_trip():
    vector = [0.123, -0.5, 1.0, 0.0]
    assert em.decode(em.encode(vector)) == pytest.approx(vector, abs=1e-3)


def test_cosine():
    assert em.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert em.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert em.cosine([0, 0], [1, 1]) == 0.0


def test_cache_reuses_vectors(tmp_path):
    path = str(tmp_path / "cache.json")
    service = FakeService()
    cache = em.EmbeddingCache(path)
    vectors, pending = cache.vectors_for(["a", "bb", "a"], service, TODAY)
    assert len(vectors) == 2 and pending == [] and len(service.calls) == 1
    cache.save()
    again = em.EmbeddingCache(path)
    vectors2, _ = again.vectors_for(["a", "bb"], service, TODAY)
    assert len(service.calls) == 1 and again.new_count == 0 and vectors2.keys() == vectors.keys()


def test_batches_of_100(tmp_path):
    service = FakeService()
    em.EmbeddingCache(str(tmp_path / "c.json")).vectors_for([f"t{i}" for i in range(250)], service, TODAY)
    assert [len(call) for call in service.calls] == [100, 100, 50]


def test_quota_in_the_middle_leaves_the_rest_pending(tmp_path):
    service = FakeService(fail_after=1)
    vectors, pending = em.EmbeddingCache(str(tmp_path / "c.json")).vectors_for(
        [f"t{i}" for i in range(150)], service, TODAY
    )
    assert len(vectors) == 100 and len(pending) == 50


def test_other_model_discards_the_cache(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"modelo": "outro", "dim": 768, "vetores": {"x": {"v": "AAA="}}}), encoding="utf-8")
    assert em.EmbeddingCache(str(path)).vectors == {}


def test_prune_by_age(tmp_path):
    cache = em.EmbeddingCache(str(tmp_path / "c.json"))
    cache.vectors = {"velho": {"v": "", "usado_em": "2026-01-01"}, "novo": {"v": "", "usado_em": "2026-11-29"}}
    assert cache.prune(TODAY) == 1 and list(cache.vectors) == ["novo"]


def test_gemini_service_maps_quota_errors_to_unavailable():
    class Boom:
        class models:
            @staticmethod
            def embed_content(**kwargs):
                from google.genai import errors

                raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                                         "details": []}})

    with pytest.raises(AIUnavailableError):
        em.GeminiEmbeddingService(api_key="x", client=Boom(), sleep=lambda s: None).embed(["a"])


def test_gemini_service_without_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(AIUnavailableError):
        em.GeminiEmbeddingService().embed(["a"])


def test_batches_are_capped_by_size(tmp_path):
    service = FakeService()
    texts = [f"{i}" + "x" * 25_000 for i in range(5)]
    em.EmbeddingCache(str(tmp_path / "c.json")).vectors_for(texts, service, TODAY)
    assert [len(call) for call in service.calls] == [2, 2, 1]


def test_per_minute_limit_waits_and_retries():
    from google.genai import errors

    waits = []

    class Flaky:
        calls = 0

        class models:
            @staticmethod
            def embed_content(**kwargs):
                Flaky.calls += 1
                if Flaky.calls == 1:
                    raise errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
                        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
                            {"quotaId": "EmbedContentTokensPerMinutePerProjectPerModel-FreeTier"}]},
                        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "20s"}]}})

                class R:
                    embeddings = [type("E", (), {"values": [1.0, 0.0]})()]

                return R()

    service = em.GeminiEmbeddingService(api_key="x", client=Flaky(), sleep=waits.append)
    assert service.embed(["a"]) == [[1.0, 0.0]] and waits == [21.0]
