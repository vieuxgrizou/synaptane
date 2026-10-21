"""Embeddings in the `/v1/embeddings` format (OpenAI, Gemini, Mistral, Voyage, Ollama and
compatible APIs). Gemini goes through its OpenAI-compatible API.

Billed tokens are read from the `usage` of each response (`Tally`); a response without
`usage` is overestimated (one token per three characters)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx2

from synaptane.ai.base import AIError
from synaptane.core.errors import Label

EMBEDDING_PROVIDERS = ("openai", "gemini", "mistral", "voyage", "ollama", "openai_compatible")
DEFAULT_EMBEDDING_MODELS = {
    "openai": "text-embedding-3-small",
    "gemini": "gemini-embedding-001",  # multilingual, 3,072 dimensions
    "mistral": "mistral-embed",
    "voyage": "voyage-3.5",
    "ollama": "bge-m3",  # multilingual: much better in French than nomic-embed-text
}
DEFAULT_EMBEDDING_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "mistral": "https://api.mistral.ai/v1",
    "voyage": "https://api.voyageai.com/v1",
    "ollama": "http://localhost:11434/v1",
}
KEYLESS_EMBEDDINGS = ("ollama", "openai_compatible")
BATCH = 64
TIMEOUT = httpx2.Timeout(120.0, connect=15.0)


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    api_key: str = ""
    base_url: str = ""

    @property
    def endpoint(self) -> str:
        return (self.base_url or DEFAULT_EMBEDDING_URLS.get(self.provider, "")).rstrip("/")


Embedder = Callable[[EmbeddingConfig, list[str]], list[list[float]]]


@dataclass
class Tally:
    """Embedded tokens, summed over calls (for the organization's budget)."""

    tokens: int = 0
    estimated: bool = False  # at least one response without `usage`: tokens estimated

    def count(self, payload: dict[str, Any], batch: list[str]) -> None:
        usage = payload.get("usage")
        used = (
            usage.get("prompt_tokens", usage.get("total_tokens"))
            if isinstance(usage, dict)
            else None
        )
        if isinstance(used, int) and used >= 0:
            self.tokens += used
        else:
            self.tokens += sum(len(text) for text in batch) // 3 + 1
            self.estimated = True


# Some models expect a task prefix, different for the query and for the pages.
_PREFIXES = (
    ("nomic-embed", "search_query: ", "search_document: "),
    ("e5", "query: ", "passage: "),
    ("mxbai-embed", "Represent this sentence for searching relevant passages: ", ""),
    # Voyage: the prompts its API prepends for input_type "query" / "document"
    ("voyage", "Represent the query for retrieving supporting documents: ",
     "Represent the document for retrieval: "),
)


def prefixes(model: str) -> tuple[str, str]:
    """(query prefix, document prefix) for this model."""
    name = model.lower()
    for marker, query, document in _PREFIXES:
        if marker in name:
            return query, document
    return "", ""


def embed(
    config: EmbeddingConfig, texts: list[str], tally: Tally | None = None
) -> list[list[float]]:
    """Vectors of the texts, in the same order (sent in batches of 64). `tally` receives the
    billed tokens."""
    if not config.endpoint:
        raise AIError("Missing embedding provider address.")
    headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}
    vectors: list[list[float]] = []
    for start in range(0, len(texts), BATCH):
        batch = texts[start : start + BATCH]
        try:
            response = httpx2.post(
                f"{config.endpoint}/embeddings",
                json={"model": config.model, "input": batch},
                headers=headers,
                timeout=TIMEOUT,
            )
        except httpx2.HTTPError as error:
            raise AIError(
                "Embedding provider unreachable: {detail}", transient=True, detail=str(error)
            ) from None
        if response.status_code in (401, 403):
            raise AIError("Key refused by the embedding provider.")
        if response.status_code == 429 or response.status_code >= 500:
            raise AIError(
                "Embedding provider unavailable ({status}).",
                transient=True,
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise AIError(
                "Embeddings refused ({status}): {detail}",
                status=response.status_code,
                detail=response.text[:300],
            )
        payload = response.json()
        if tally is not None:
            tally.count(payload, batch)
        data = sorted(payload["data"], key=lambda item: item.get("index", 0))
        if len(data) != len(batch):
            raise AIError("Incomplete embeddings response.")
        vectors.extend([float(x) for x in item["embedding"]] for item in data)
    return vectors


def check_embeddings(config: EmbeddingConfig, embedder: Embedder = embed) -> Label:
    [vector] = embedder(config, ["Synaptane connection test"])
    return Label(
        "Connection successful ({model}, {dimensions}-dimension vectors).",
        model=config.model,
        dimensions=len(vector),
    )
