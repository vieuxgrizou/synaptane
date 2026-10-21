"""Reranking: the Voyage client, and the order of the results it rescores."""

from typing import Any

import httpx2
import pytest

from synaptane.ai import rerank
from synaptane.ai.base import AIError
from synaptane.ai.embeddings import EmbeddingConfig
from synaptane.services.search import Hit, Snippet, _rerank_order, _rerank_text

VOYAGE = EmbeddingConfig("voyage", "voyage-4-large", "pa-test")


class Response:
    def __init__(self, status: int, payload: dict[str, Any]) -> None:
        self.status_code = status
        self.payload = payload
        self.text = str(payload)

    def json(self) -> dict[str, Any]:
        return self.payload


def hit(path: str) -> Hit:
    return Hit("o", "s", "S", path, path.upper(), 1.0, [Snippet(3, "ligne", ["avant"], ["après"])])


def test_scores_come_back_in_the_order_of_the_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def post(url: str, **kwargs: Any) -> Response:
        sent.update(kwargs, url=url)
        data = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]
        return Response(200, {"data": data})

    monkeypatch.setattr("synaptane.ai.rerank.httpx2.post", post)
    assert rerank.rerank(VOYAGE, "rerank-2.5", "cartons", ["a", "b"]) == [0.2, 0.9]
    assert sent["url"] == "https://api.voyageai.com/v1/rerank"
    assert sent["json"]["model"] == "rerank-2.5"
    assert sent["headers"] == {"Authorization": "Bearer pa-test"}


def test_reranking_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "synaptane.ai.rerank.httpx2.post", lambda *_, **__: Response(401, {"detail": "invalid"})
    )
    with pytest.raises(AIError):
        rerank.rerank(VOYAGE, "rerank-2.5", "q", ["a"])

    def down(*_: Any, **__: Any) -> Response:
        raise httpx2.ConnectError("down")

    monkeypatch.setattr("synaptane.ai.rerank.httpx2.post", down)
    with pytest.raises(AIError) as error:
        rerank.rerank(VOYAGE, "rerank-2.5", "q", ["a"])
    assert error.value.transient
    assert rerank.rerank(VOYAGE, "rerank-2.5", "q", []) == []


def test_results_follow_the_scores_and_ties_keep_their_order() -> None:
    a, b, c = hit("a.md"), hit("b.md"), hit("c.md")
    assert _rerank_order([a, b, c], [0.1, 0.7, 0.1]) == [b, a, c]


def test_the_reranker_reads_title_section_and_excerpts() -> None:
    found = hit("a.md")
    found.section = "Assurance"
    assert _rerank_text(found) == "A.MD\nAssurance\navant\nligne\naprès"
