"""Optional semantic search per space (see docs/concepts.md, "Search"): settings, chunking,
index, query.

Enabled by an organization admin for a space, with the organization's embedding provider.
Pages are split by section; each chunk is embedded with the page title and its headings. The
index (pgvector) is incremental (Git hash of each file) and can be rebuilt from Git.
"""

import json
import logging
import re
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from synaptane.ai import embeddings
from synaptane.ai.base import AIError
from synaptane.ai.embeddings import (
    DEFAULT_EMBEDDING_MODELS,
    EMBEDDING_PROVIDERS,
    KEYLESS_EMBEDDINGS,
    Embedder,
    EmbeddingConfig,
    prefixes,
)
from synaptane.core.access import Action, require
from synaptane.core.errors import forbidden, invalid
from synaptane.core.markdown import title_of
from synaptane.core.security import SecretBox
from synaptane.git.repo import PathRefused, Repository, safe_path
from synaptane.services import Actor, audit, budget, jobs
from synaptane.services.content import kind_of
from synaptane.services.extraction import read_json, write_json
from synaptane.services.inbox import CONTENT_DIR, parse_front_matter
from synaptane.services.secrets import (
    CUSTOM_URL_REFUSED,
    check_destination,
    get_org_secret,
    put_org_secret,
)
from synaptane.services.workspace import Workspace
from synaptane.storage.db import Database
from synaptane.storage.models import EmbeddingChunk, Organization, OrgEmbeddingSettings, Space

EMBEDDING_KEY = "embedding"
CHUNK_CHARS = 1500
MAX_CHARS_PER_FILE = 200_000
SEARCH_CHUNKS = 40
EMBED_BATCH = 128  # chunks per call to the provider (which itself splits by 64)
# Files read without yielding any chunk (binary, empty page), with the hash read: stored next
# to the extracted text, outside the database, so they are not read again at each indexing.
NO_PIECES = "embeddings-sans-morceau.json"
log = logging.getLogger("synaptane.semantic")


# --- Settings ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingView:
    provider: str
    model: str
    base_url: str
    has_key: bool


def view(session: Session, actor: Actor, box: SecretBox, org: Organization) -> EmbeddingView | None:
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    row = session.get(OrgEmbeddingSettings, org.id)
    if row is None:
        return None
    has_key = bool(get_org_secret(session, box, org.id, EMBEDDING_KEY))
    return EmbeddingView(row.provider, row.model, row.base_url, has_key)


def configure(
    session: Session,
    actor: Actor,
    box: SecretBox,
    org: Organization,
    *,
    provider: str,
    model: str = "",
    base_url: str = "",
    api_key: str | None = None,
) -> EmbeddingView:
    """`api_key=None` keeps the key; "" removes it. Changing the model rebuilds the index."""
    require(actor.access, Action.MANAGE_ORG, org_id=org.id)
    if provider not in EMBEDDING_PROVIDERS:
        raise invalid("Unknown embedding provider.")
    model = model.strip() or DEFAULT_EMBEDDING_MODELS.get(provider, "")
    if not model:
        raise invalid("Enter the embedding model.")
    base_url = base_url.strip().rstrip("/")
    if provider == "openai_compatible" and not base_url:
        raise invalid("Enter the OpenAI-compatible API address.")
    if base_url and not base_url.startswith(("https://", "http://")):
        raise invalid("Invalid API address.")
    row = session.get(OrgEmbeddingSettings, org.id)
    refused = check_destination(
        superadmin=actor.access.is_superadmin,
        old=(row.provider, row.base_url) if row else None,
        new=(provider, base_url),
        has_key=bool(get_org_secret(session, box, org.id, EMBEDDING_KEY)),
        key_given=api_key is not None,  # new key, or key removed
    )
    if refused:
        raise forbidden(refused) if refused == CUSTOM_URL_REFUSED else invalid(refused)
    if row is None:
        row = OrgEmbeddingSettings(org_id=org.id, provider=provider, model=model)
        session.add(row)
    row.provider, row.model, row.base_url = provider, model, base_url
    if api_key is not None:
        put_org_secret(session, box, org.id, EMBEDDING_KEY, api_key.strip(), actor.user.id)
    audit(
        session, actor, "org.embeddings_configured", org_id=org.id, provider=provider, model=model
    )
    for space in session.scalars(
        select(Space).where(Space.org_id == org.id, Space.embeddings_enabled.is_(True))
    ):
        jobs.enqueue(session, "index", space.id)
    session.flush()
    result = view(session, actor, box, org)
    assert result is not None
    return result


def org_config(session: Session, box: SecretBox, org_id: UUID) -> EmbeddingConfig | None:
    row = session.get(OrgEmbeddingSettings, org_id)
    if row is None:
        return None
    key = get_org_secret(session, box, org_id, EMBEDDING_KEY) or ""
    if not key and row.provider not in KEYLESS_EMBEDDINGS:
        return None
    return EmbeddingConfig(row.provider, row.model, key, row.base_url)


def config_for(session: Session, box: SecretBox, space: Space) -> EmbeddingConfig | None:
    """Effective configuration of a space, or None if semantic search is off there."""
    return org_config(session, box, space.org_id) if space.embeddings_enabled else None


def set_enabled(session: Session, actor: Actor, space: Space, enabled: bool) -> None:
    require(actor.access, Action.MANAGE_ORG, org_id=space.org_id)
    space.embeddings_enabled = enabled
    if enabled:
        jobs.enqueue(session, "index", space.id)
    else:
        session.execute(delete(EmbeddingChunk).where(EmbeddingChunk.space_id == space.id))
    audit(session, actor, "space.embeddings", space=space, enabled=enabled)


def stats(session: Session, space_id: UUID) -> tuple[int, int]:
    """(indexed files, chunks)."""
    rows = (
        session.execute(select(EmbeddingChunk.path).where(EmbeddingChunk.space_id == space_id))
        .scalars()
        .all()
    )
    return len(set(rows)), len(rows)


# --- Chunking ---------------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass(frozen=True)
class Piece:
    heading: str  # "Title › Section › Subsection"
    text: str
    context: str = ""  # values of the page's front matter, embedded with the chunk


CONTEXT_CHARS = 600  # context taken from the front matter, per page
CONTEXT_VALUE_CHARS = 60  # longer values (summaries, lists) are left to the chunks themselves
# Hashes, identifiers and bare numbers (codes, counters): no meaning for the search
_IDENTIFIER = re.compile(r"^(?:[0-9a-f]{12,}|-?[0-9][0-9.,]*)$")


def front_matter_context(text: str) -> str:
    """Short values of a page's front matter, as `key: value; …`: identifiers, bare numbers,
    empty and long values left out."""
    fields, _ = parse_front_matter(text)
    parts = []
    for key, value in fields.items():
        if isinstance(value, bool | int | float):
            value = json.dumps(value)
        if not isinstance(value, str):
            continue
        value = " ".join(value.split())
        if not value or len(value) > CONTEXT_VALUE_CHARS or _IDENTIFIER.match(value):
            continue
        parts.append(f"{key}: {value}")
    return "; ".join(parts)[:CONTEXT_CHARS]


def _pack(paragraphs: list[str]) -> list[str]:
    chunks, current = [], ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        while len(paragraph) > CHUNK_CHARS:  # very long paragraph: hard split
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:CHUNK_CHARS])
            paragraph = paragraph[CHUNK_CHARS:]
        if current and len(current) + len(paragraph) + 2 > CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


def split_markdown(text: str, title: str) -> list[Piece]:
    """Chunks by section: each section keeps the trail of its headings."""
    sections: list[tuple[list[str], list[str]]] = [([], [])]
    trail: list[tuple[int, str]] = []
    fence = ""  # inside a code block: a "# comment" is not a heading
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            marker = stripped[:3]
            fence = "" if fence == marker else (fence or marker)
        match = None if fence else _HEADING.match(line)
        if match:
            level, name = len(match.group(1)), match.group(2).strip()
            trail = [(lvl, n) for lvl, n in trail if lvl < level] + [(level, name)]
            sections.append(([n for _, n in trail], []))
        else:
            sections[-1][1].append(line)
    pieces = []
    for path, lines in sections:
        names = [n for n in path if n != title]
        heading = " › ".join([title, *names]) if names else title
        for chunk in _pack("\n".join(lines).split("\n\n")):
            pieces.append(Piece(heading, chunk))
    return pieces


def split_plain(text: str, title: str) -> list[Piece]:
    return [Piece(title, chunk) for chunk in _pack(re.split(r"\n\s*\n", text))]


# --- Index ------------------------------------------------------------------------------------


def _sources(ws: Workspace, space_id: UUID, repo: Repository) -> dict[str, str]:
    """Indexable files: path (relative to content/) → Git hash."""
    found = {}
    for blob in repo.files(CONTENT_DIR):
        relative = blob.path[len(CONTENT_DIR) + 1 :]
        try:
            safe_path(blob.path)
        except PathRefused:  # booby-trapped Git tree (`..`): skipped, not fatal
            log.warning("Embeddings: path refused in the repository, skipped: %r", blob.path)
            continue
        kind = kind_of(blob.path)
        if kind in ("pdf", "office", "image"):
            if not ws.extracted_file(space_id, blob.path).is_file():
                continue
        elif kind == "binary" and blob.size > 2_000_000:
            continue
        found[relative] = blob.sha
    return found


def _pieces(ws: Workspace, space_id: UUID, repo: Repository, path: str) -> list[Piece]:
    full = f"{CONTENT_DIR}/{path}"
    kind = kind_of(full)
    if kind in ("pdf", "office", "image"):
        text = (ws.extracted_text(space_id, full) or "")[:MAX_CHARS_PER_FILE]
        return split_plain(text, path.rsplit("/", 1)[-1])
    data = repo.blob(full) or b""
    if b"\0" in data[:8192]:
        return []
    text = data.decode("utf-8", "replace")[:MAX_CHARS_PER_FILE]
    if kind == "markdown":
        pieces = split_markdown(text, title_of(text, path.rsplit("/", 1)[-1]))
        context = front_matter_context(text) if ws.embed_front_matter else ""
        return [replace(p, context=context) for p in pieces] if context else pieces
    return split_plain(text, path.rsplit("/", 1)[-1])


def index_space(
    database: Database,
    ws: Workspace,
    space_id: UUID,
    repo: Repository,
    config: EmbeddingConfig,
    embedder: Embedder | None = None,
) -> tuple[int, int]:
    """Updates the index. Returns (files embedded, files removed).

    The tokens billed by the provider (default embedder) count towards the organization's AI
    budget for the month, even if indexing stops midway."""
    tally = embeddings.Tally()
    embedder = embedder or partial(embeddings.embed, tally=tally)
    sources = _sources(ws, space_id, repo)
    with database.session() as session:
        indexed = {
            path: (sha, model)
            for path, sha, model in session.execute(
                select(EmbeddingChunk.path, EmbeddingChunk.blob_sha, EmbeddingChunk.model)
                .where(EmbeddingChunk.space_id == space_id)
                .distinct()
            )
        }
        stale = [
            p
            for p, (sha, model) in indexed.items()
            if p not in sources or sha != sources[p] or model != config.model
        ]
        if stale:
            session.execute(
                delete(EmbeddingChunk).where(
                    EmbeddingChunk.space_id == space_id, EmbeddingChunk.path.in_(stale)
                )
            )
    fresh = {p: (sha, config.model) for p, (sha, _) in indexed.items() if p not in stale}
    memo = ws.extract_path(space_id) / NO_PIECES
    known = _no_pieces(memo)
    empty = {p: sha for p, sha in known.items() if sources.get(p) == sha}
    todo = [
        p for p, sha in sources.items() if fresh.get(p, ("", ""))[0] != sha and empty.get(p) != sha
    ]
    prefix = prefixes(config.model)[1]

    def store(files: list[tuple[str, list[Piece]]], vectors: list[list[float]]) -> None:
        rows, position = [], 0
        for path, pieces in files:
            for n, piece in enumerate(pieces):
                rows.append(
                    EmbeddingChunk(
                        space_id=space_id,
                        path=path,
                        blob_sha=sources[path],
                        chunk=n,
                        heading=piece.heading,
                        text=piece.text,
                        model=config.model,
                        embedding=vectors[position],
                    )
                )
                position += 1
        with database.session() as session:
            session.add_all(rows)

    def texts(files: list[tuple[str, list[Piece]]]) -> list[str]:
        return [
            f"{prefix}{p.heading}\n{p.context}\n\n{p.text}"
            if p.context
            else f"{prefix}{p.heading}\n\n{p.text}"
            for _, pieces in files
            for p in pieces
        ]

    def flush(files: list[tuple[str, list[Piece]]]) -> None:
        """One call to the provider for several files, and a single transaction."""
        try:
            store(files, embedder(config, texts(files)))
            return
        except AIError as error:
            if error.transient or len(files) == 1:
                raise
        for single in files:  # batch refused: file by file, without blocking the others
            try:
                store([single], embedder(config, texts([single])))
            except AIError as error:
                if error.transient:
                    raise
                log.warning("Embeddings: %s skipped (%s)", single[0], error)

    batch: list[tuple[str, list[Piece]]] = []
    vectorised = 0
    try:
        for path in todo:
            pieces = _pieces(ws, space_id, repo, path)
            if pieces:
                batch.append((path, pieces))
                vectorised += 1
            else:
                empty[path] = sources[path]
            if sum(len(p) for _, p in batch) >= EMBED_BATCH:
                flush(batch)
                batch = []
        if batch:
            flush(batch)
    finally:  # even after a provider outage: these reads are done for good
        if empty != known:
            write_json(memo, empty)
        if tally.tokens:
            _count_cost(database, ws, space_id, config, tally)
    return vectorised, len([p for p in stale if p not in sources])


def _count_cost(
    database: Database,
    ws: Workspace,
    space_id: UUID,
    config: EmbeddingConfig,
    tally: embeddings.Tally,
) -> None:
    """Indexing cost in the month's budget (paid providers), then alerts."""
    with database.session() as session:
        space = session.get(Space, space_id)
        if space is None:
            return
        amount = budget.record_embeddings(
            session, space, config.provider, config.model, tally.tokens, tally.estimated
        )
        if amount:
            budget.check_alerts(session, ws.box, space, ws.public_url)


def _no_pieces(memo: Path) -> dict[str, str]:
    """Path → hash of the files read without any chunk. Damaged file: everything is read
    again."""
    raw = read_json(memo)
    if not isinstance(raw, dict):
        return {}
    return {p: sha for p, sha in raw.items() if isinstance(p, str) and isinstance(sha, str)}


def clear(session: Session, space_id: UUID) -> None:
    session.execute(delete(EmbeddingChunk).where(EmbeddingChunk.space_id == space_id))


# --- Query ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SemanticHit:
    path: str
    heading: str
    text: str
    similarity: float


def nearest(
    session: Session, space_id: UUID, model: str, vector: list[float], limit: int = 20
) -> list[SemanticHit]:
    """Closest pages (best chunk of each page), from closest to least close."""
    distance = EmbeddingChunk.embedding.cosine_distance(vector)
    rows = session.execute(
        select(EmbeddingChunk.path, EmbeddingChunk.heading, EmbeddingChunk.text, distance)
        .where(EmbeddingChunk.space_id == space_id, EmbeddingChunk.model == model)
        .order_by(distance)
        .limit(SEARCH_CHUNKS)
    )
    best: dict[str, SemanticHit] = {}
    for path, heading, text, value in rows:
        if path not in best:
            best[path] = SemanticHit(path, heading, text, 1.0 - float(value))
    return list(best.values())[:limit]
