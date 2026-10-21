"""Reranking of search results by a cross-encoder (Voyage `/v1/rerank`): each candidate gets a
relevance score for the question, from 0 to 1. It uses the organization's embedding key."""

from collections.abc import Callable

import httpx2

from synaptane.ai.base import AIError
from synaptane.ai.embeddings import EmbeddingConfig

RERANK_PROVIDERS = ("voyage",)
TIMEOUT = httpx2.Timeout(15.0, connect=5.0)

Reranker = Callable[[EmbeddingConfig, str, str, list[str]], list[float]]


def rerank(config: EmbeddingConfig, model: str, query: str, documents: list[str]) -> list[float]:
    """Relevance score of each document, in the same order."""
    if not documents:
        return []
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    try:
        response = httpx2.post(
            f"{config.endpoint}/rerank",
            json={"query": query, "documents": documents, "model": model, "truncation": True},
            headers=headers,
            timeout=TIMEOUT,
        )
    except httpx2.HTTPError as error:
        raise AIError(
            "Reranking provider unreachable: {detail}", transient=True, detail=str(error)
        ) from None
    if response.status_code >= 400:
        raise AIError(
            "Reranking refused ({status}): {detail}",
            status=response.status_code,
            detail=response.text[:300],
        )
    data = response.json().get("data", [])
    if len(data) != len(documents):
        raise AIError("Incomplete reranking response.")
    scores = [0.0] * len(documents)
    for item in data:
        scores[int(item["index"])] = float(item["relevance_score"])
    return scores
