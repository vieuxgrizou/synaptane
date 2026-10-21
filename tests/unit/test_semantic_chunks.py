import json
from typing import Any

import httpx2
import pytest

from synaptane.ai.base import AIError
from synaptane.ai.embeddings import EmbeddingConfig, embed
from synaptane.services.semantic import CHUNK_CHARS, split_markdown, split_plain

PAGE = """# Proxmox

Introduction.

## Sauvegarde

La sauvegarde tourne à 2h.

### Restauration

qmrestore.

## VM

Deux VM.
"""


def test_markdown_chunks_keep_their_heading_path() -> None:
    pieces = split_markdown(PAGE, "Proxmox")
    assert [(p.heading, p.text) for p in pieces] == [
        ("Proxmox", "Introduction."),
        ("Proxmox › Sauvegarde", "La sauvegarde tourne à 2h."),
        ("Proxmox › Sauvegarde › Restauration", "qmrestore."),
        ("Proxmox › VM", "Deux VM."),
    ]


def test_long_text_is_packed_under_the_limit() -> None:
    text = "\n\n".join(["mot " * 100] * 20 + ["x" * (CHUNK_CHARS * 2 + 10)])
    pieces = split_plain(text, "doc.pdf")
    assert all(len(p.text) <= CHUNK_CHARS for p in pieces) and len(pieces) > 5
    assert {p.heading for p in pieces} == {"doc.pdf"}


def test_embed_batches_and_keeps_order(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[list[str]] = []

    def post(url: str, **kwargs: Any) -> httpx2.Response:
        batch = kwargs["json"]["input"]
        sent.append(batch)
        data = [
            {"index": i, "embedding": [float(len(t))]} for i, t in reversed(list(enumerate(batch)))
        ]
        return httpx2.Response(200, json={"data": data})

    monkeypatch.setattr(httpx2, "post", post)
    texts = ["a" * n for n in range(1, 101)]
    vectors = embed(EmbeddingConfig("openai", "text-embedding-3-small", "k"), texts)
    assert [len(b) for b in sent] == [64, 36]
    assert vectors == [[float(n)] for n in range(1, 101)]
    monkeypatch.setattr(httpx2, "post", lambda url, **kw: httpx2.Response(429, text=json.dumps({})))
    with pytest.raises(AIError) as error:
        embed(EmbeddingConfig("mistral", "mistral-embed", "k"), ["x"])
    assert error.value.transient


def test_stopwords_are_ignored_unless_alone() -> None:
    from synaptane.services.search import Query

    assert Query.parse("recette de la tarte aux pommes").terms == ("recette", "tarte", "pommes")
    assert Query.parse("Comment sauvegarder ?").terms == ("sauvegarder",)
    assert Query.parse("de la").terms == ("de", "la")  # only stop words: they are kept


def test_code_comments_are_not_headings() -> None:
    from synaptane.services.semantic import split_markdown

    text = "# Proxmox\n\n```bash\n# installer vzdump\napt install vzdump\n```\n\n## Sauvegarde\n\nx"
    headings = [piece.heading for piece in split_markdown(text, "Proxmox")]
    assert headings == ["Proxmox", "Proxmox › Sauvegarde"]


def test_front_matter_context_keeps_short_values_and_drops_identifiers() -> None:
    from synaptane.services.semantic import front_matter_context

    page = (
        '---\nappel: "6a97d07d0ae632ba1507b31d"\nlead: "1384248"\nmarque: "DCOM"\n'
        'signe: "non"\nprix_annonce: true\nprix_eur: 800\nmots_prospect: ""\n'
        'resume: "' + "x" * 80 + '"\n---\n\n# Appel\n\nTexte.'
    )
    assert front_matter_context(page) == (
        "marque: DCOM; signe: non; prix_annonce: true"
    )
    assert front_matter_context("# Sans en-tête\n\nTexte.") == ""
