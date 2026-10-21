"""Visible content of a space: the repository's `content/` directory
(docs/reference/repository-format.md).

Read from the Git objects at the last commit (consistent view even during a synchronization).
Write: a commit that holds the change and its `change` inbox item, then synchronization (push)
and reindexing by the worker.
"""

import codecs
import itertools
import logging
import mimetypes
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

from synaptane.core.access import Action, require
from synaptane.core.errors import SynaptaneError, conflict, invalid, not_found
from synaptane.core.markdown import WikiIndex, broken_links, links_of, title_of
from synaptane.core.secret_scan import added_findings, refuse_secrets
from synaptane.git.repo import Blob, Commit, GitError, PathRefused, Repository, safe_path
from synaptane.services import Actor, audit, jobs, links, webhooks
from synaptane.services.inbox import CONTENT_DIR
from synaptane.services.workspace import Workspace, author_identity
from synaptane.storage.db import Database, try_space_lock
from synaptane.storage.models import Space

MARKDOWN = {".md", ".markdown"}
IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".bmp"}
# Old binary formats: .doc and .xls read by catdoc (when it is installed), .ppt in Python.
OFFICE = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".doc", ".xls", ".ppt"}
MAX_TEXT_VIEW = 2 * 1024 * 1024
MAX_UPLOAD = 25 * 1024 * 1024
# Types a browser would execute: never served as is from Synaptane's origin.
SANDBOXED = {"text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/xml"}

log = logging.getLogger("synaptane.content")


def kind_of(path: str, data: bytes | None = None) -> str:
    """markdown, text, pdf, image, office or binary."""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in MARKDOWN:
        return "markdown"
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGES:
        return "image"
    if suffix in OFFICE:
        return "office"
    if data is not None:
        head = data[:8192]
        if b"\0" not in head:
            try:
                # Incremental: a multi-byte character cut by the 8 KiB window is not an error,
                # an invalid byte still is (a strict decode classed such text files as binary).
                codecs.getincrementaldecoder("utf-8")().decode(head, final=len(data) <= len(head))
                return "text"
            except UnicodeDecodeError:
                pass
    return "binary"


def window(text: str, offset: int = 0, limit: int = 50_000) -> tuple[str, bool]:
    """Slice of `text` for reading by an assistant, and True if it is partial.

    A truncated read always says so, with the position of the rest: otherwise, the assistant
    would believe it has read the whole page (and could lose its end when rewriting it).
    """
    offset = max(0, offset)
    part = text[offset : offset + limit]
    end = offset + len(part)
    partial = offset > 0 or end < len(text)
    if end < len(text):
        part += (
            f"\n\n[… truncated: characters {offset} to {end} of {len(text)}. "
            f"Read the rest with offset={end}.]"
        )
    if offset:
        part = f"[… reading from character {offset} of {len(text)}]\n" + part
    return part, partial


def mime_of(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def content_path(relative: str) -> str:
    """Path relative to `content/` (as seen in the wiki) → path in the repository."""
    relative = relative.strip().strip("/")
    if not relative:
        return CONTENT_DIR
    try:
        return safe_path(f"{CONTENT_DIR}/{relative}")
    except GitError:
        raise invalid("Invalid path.") from None


def relative_path(repo_path: str) -> str:
    return repo_path[len(CONTENT_DIR) + 1 :] if repo_path.startswith(CONTENT_DIR + "/") else ""


# --- Reading ----------------------------------------------------------------------------------


def _readable(actor: Actor, ws: Workspace, space: Space) -> Repository:
    require(actor.access, Action.READ_SPACE, org_id=space.org_id, space_id=space.id)
    return ws.local_repository(space)


def list_files(actor: Actor, ws: Workspace, space: Space) -> list[Blob]:
    """All the files of `content/`, paths relative to `content/`."""
    repo = _readable(actor, ws, space)
    return [
        Blob(relative_path(b.path), b.sha, b.size)
        for b in repo.files(CONTENT_DIR)
        if relative_path(b.path)
    ]


def wiki_index(actor: Actor, ws: Workspace, space: Space) -> WikiIndex:
    """Files of the space, to resolve wiki links [[name]]."""
    return WikiIndex.of(b.path for b in list_files(actor, ws, space))


def broken_links_in(actor: Actor, ws: Workspace, space: Space, path: str) -> str:
    """Broken links of a Markdown page, as a sentence to append to an answer (empty if none)."""
    if not path.endswith(".md"):
        return ""
    file = read_file(actor, ws, space, path)
    if file.text is None:
        return ""
    broken = broken_links(file.text, file.path, wiki_index(actor, ws, space))
    if not broken:
        return ""
    parts = [
        f"{written} (did you mean {suggestion}?)" if suggestion else f"{written} (not found)"
        for written, suggestion in broken[:20]
    ]
    return " Broken links in this page, to fix: " + "; ".join(parts) + "."


@dataclass
class Folder:
    name: str
    path: str
    folders: dict[str, "Folder"] = field(default_factory=dict)
    files: list[Blob] = field(default_factory=list)

    def walk_files(self) -> Iterator[Blob]:
        yield from self.files
        for folder in self.folders.values():
            yield from folder.walk_files()


def build_tree(files: list[Blob]) -> Folder:
    root = Folder("", "")
    for blob in sorted(files, key=lambda b: b.path.lower()):
        parts = blob.path.split("/")
        node = root
        for depth, part in enumerate(parts[:-1]):
            node = node.folders.setdefault(part, Folder(part, "/".join(parts[: depth + 1])))
        node.files.append(blob)
    return root


@dataclass(frozen=True)
class File:
    path: str  # relative to content/
    sha: str
    size: int
    kind: str
    data: bytes

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def text(self) -> str | None:
        if self.kind in ("markdown", "text") and self.size <= MAX_TEXT_VIEW:
            return self.data.decode("utf-8", "replace")
        return None


def read_file(actor: Actor, ws: Workspace, space: Space, path: str) -> File:
    """File and its content. Beyond MAX_TEXT_VIEW, the content is not loaded (`data` empty):
    it is then served as a stream (see `open_raw`)."""
    repo = _readable(actor, ws, space)
    full = content_path(path)
    entry = repo.blob_entry(full)
    if entry is None:
        raise not_found("Page not found.")
    sha, size = entry
    if size > MAX_TEXT_VIEW:
        return File(relative_path(full), sha, size, kind_of(full), b"")
    data = repo.object_data(sha)
    if data is None:
        raise not_found("Page not found.")
    return File(relative_path(full), sha, size, kind_of(full, data), data)


@dataclass(frozen=True)
class RawFile:
    path: str
    size: int
    kind: str
    chunks: Iterator[bytes]

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name


def open_raw(actor: Actor, ws: Workspace, space: Space, path: str) -> RawFile:
    """Raw file as a stream: memory stays constant, whatever its size."""
    repo = _readable(actor, ws, space)
    full = content_path(path)
    entry = repo.blob_entry(full)
    if entry is None:
        raise not_found("Page not found.")
    sha, size = entry
    chunks = repo.object_stream(sha)
    head = next(chunks, b"")
    return RawFile(relative_path(full), size, kind_of(full, head), itertools.chain([head], chunks))


def folder_index(files: list[Blob], folder: str) -> str | None:
    """Landing page of a folder: README.md or index.md."""
    prefix = f"{folder}/" if folder else ""
    paths = {b.path for b in files}
    for name in ("README.md", "readme.md", "index.md", "Index.md"):
        if prefix + name in paths:
            return prefix + name
    return None


def history(actor: Actor, ws: Workspace, space: Space, path: str) -> list[Commit]:
    return _readable(actor, ws, space).history(content_path(path))


def last_change(actor: Actor, ws: Workspace, space: Space, path: str) -> Commit | None:
    """Last commit that touched the file (author and date, for the document header)."""
    commits = _readable(actor, ws, space).history(content_path(path), limit=1)
    return commits[0] if commits else None


def diff(actor: Actor, ws: Workspace, space: Space, path: str, sha: str) -> str:
    try:
        return _readable(actor, ws, space).diff(sha, content_path(path))
    except GitError:
        raise not_found("Version not found.") from None


def diff_range(actor: Actor, ws: Workspace, space: Space, path: str, old: str, new: str) -> str:
    """Diff of a file between two versions (hexadecimal commit ids: nothing else reaches
    `git diff`, not even an option)."""
    for sha in (old, new):
        if not 7 <= len(sha) <= 64 or any(c not in "0123456789abcdef" for c in sha):
            raise invalid("Invalid commit identifier: {sha}.", sha=sha[:80])
    try:
        return _readable(actor, ws, space).compare_range(old, new, content_path(path))
    except GitError:
        raise not_found("Version not found.") from None


def backlinks(
    actor: Actor, ws: Workspace, space: Space, path: str, index: WikiIndex | None = None
) -> list[tuple[str, str]]:
    """Markdown pages that link to `path`: (path, title), 50 at most. With `index` (files of
    the space), wiki links [[name]] count too."""
    _readable(actor, ws, space)
    root = ws.clone_path(space.id) / CONTENT_DIR
    stem = PurePosixPath(path).stem
    if not root.is_dir() or not stem:
        return []
    result = subprocess.run(
        [
            shutil.which("rg") or "rg",
            "--files-with-matches",
            "--fixed-strings",
            "--ignore-case",
            "--glob",
            "*.md",
            "--",
            stem,
            str(root),
        ],
        capture_output=True,
        timeout=20,
    )
    found = []
    for line in sorted(result.stdout.decode("utf-8", "replace").splitlines()):
        candidate = Path(line).relative_to(root).as_posix()
        if candidate == path:
            continue
        text = Path(line).read_text(encoding="utf-8", errors="replace")
        if path in links_of(text, candidate, index):
            found.append((candidate, title_of(text, PurePosixPath(candidate).stem)))
        if len(found) == 50:
            break
    return found


# --- Writing ----------------------------------------------------------------------------------


@contextmanager
def writable(
    session: Session, database: Database, actor: Actor, ws: Workspace, space: Space
) -> Iterator[Repository]:
    require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
    # The request's connection goes back to the pool during git (lock, write, commit):
    # otherwise each write would hold two, and the pool would quickly run dry.
    session.commit()
    # During a conflict, writing is still possible: it will be included in the resolution.
    repo = ws.local_repository(space)
    with try_space_lock(database.engine, space.id, timeout=30) as acquired:
        if not acquired:
            raise conflict("The space is busy (synchronization in progress). Try again.")
        if repaired := repo.heal():
            log.warning("Space %s: local copy repaired (%s)", space.id, ", ".join(repaired))
        yield repo


def _commit(
    repo: Repository,
    ws: Workspace,
    actor: Actor,
    source: str,
    files: dict[str, bytes | None],
    actions: dict[str, str],
    message: str,
) -> str | None:
    author = author_identity(actor.user, ws.committer)
    # No inbox item: the commit is enough, the tidy run will find it in the history.
    subject = message.strip().splitlines()[0][:200] if message.strip() else _summary(actions)
    subject += f"\n\nSource: {source}" + (f"\nVia: {actor.via}" if actor.via else "")
    try:
        return repo.commit_changes(files, author, subject)
    except PathRefused as refused:
        raise invalid("Write refused: {detail}", detail=str(refused)) from None


def commit_files(
    repo: Repository,
    ws: Workspace,
    actor: Actor,
    source: str,
    files: dict[str, bytes | None],
    message: str,
) -> str | None:
    """Commit in the person's name, with its source (under the lock of `writable`)."""
    return _commit(repo, ws, actor, source, files, dict.fromkeys(files, "edited"), message)


def _summary(actions: dict[str, str]) -> str:
    path, action = next(iter(sorted(actions.items())))
    more = f" (+{len(actions) - 1})" if len(actions) > 1 else ""
    return f"{relative_path(path) or path}: {action}{more}"


def _after_write(
    session: Session, actor: Actor, space: Space, action: str, path: str, source: str = "web"
) -> None:
    jobs.enqueue(session, "sync", space.id)
    jobs.enqueue(session, "index", space.id)
    audit(session, actor, action, space=space, path=path)
    webhooks.emit(
        session,
        space,
        "content.changed",
        {
            "paths": [relative_path(path) or path],
            "action": action.removeprefix("content."),
            "by": actor.user.username,
            "source": source,
            "via": actor.via,
        },
    )


after_write = _after_write  # for writes made elsewhere (verification of a page)


def write_file(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    path: str,
    data: bytes,
    *,
    message: str = "",
    base_sha: str | None = None,
    source: str = "web",
    allow_secrets: bool = False,
) -> str | None:
    """Creates or replaces a file. `base_sha`: digest read before editing (conflict otherwise).

    A likely secret in the added lines is refused, unless `allow_secrets` (confirmed by the
    person). Returns the commit created, or None if the content did not change.
    """
    full = content_path(path)
    if full == CONTENT_DIR:
        raise invalid("Enter the file path.")
    if len(data) > MAX_UPLOAD:
        raise invalid("File too large (25 MB maximum).")
    if not allow_secrets:  # scan outside the space lock (up to 2 MB of text)
        require(actor.access, Action.CONTRIBUTE_SPACE, org_id=space.org_id, space_id=space.id)
        try:
            old = ws.local_repository(space).blob(full)
        except SynaptaneError:
            old = None
        refuse_secrets(added_findings(relative_path(full) or full, data, old))
    with writable(session, database, actor, ws, space) as repo:
        current = repo.blob_sha(full)
        if base_sha is not None and current != (base_sha or None):
            raise conflict("The page changed since you opened it: reload it before saving.")
        action = "edited" if current else "created"
        sha = _commit(repo, ws, actor, source, {full: data}, {full: action}, message)
    if sha:
        _after_write(session, actor, space, "content.written", full, source)
    return sha


def delete_file(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    path: str,
    *,
    message: str = "",
    source: str = "web",
) -> None:
    full = content_path(path)
    with writable(session, database, actor, ws, space) as repo:
        if repo.blob_sha(full) is None:
            raise not_found("Page not found.")
        _commit(repo, ws, actor, source, {full: None}, {full: "deleted"}, message)
    _after_write(session, actor, space, "content.deleted", full, source)


def move_file(
    session: Session,
    database: Database,
    actor: Actor,
    ws: Workspace,
    space: Space,
    source_path: str,
    target_path: str,
    *,
    message: str = "",
    source: str = "web",
) -> list[str]:
    """Moves or renames a file. Links follow, in the same commit: those of the moved page are
    recomputed for its new location, and the pages that linked to it are rewritten (see
    `links.move_changes`, the same computation as for the agent).

    Returns the pages whose links were updated (paths relative to `content/`)."""
    old, new = content_path(source_path), content_path(target_path)
    if CONTENT_DIR in (old, new):
        raise invalid("Enter the file path.")
    with writable(session, database, actor, ws, space) as repo:
        head = repo.head()
        if head is None or repo.blob_sha(old) is None:
            raise not_found("Page not found.")
        if repo.blob_sha(new) is not None:
            raise conflict("A file already exists at this location.")
        files = {b.path for b in repo.files(CONTENT_DIR, head)}
        writes = links.move_changes(
            lambda paths: repo.read_many(paths, head),
            files,
            links.base_links(repo, head, files),
            old,
            new,
        )
        updated = sorted(p for p in writes if p != new)
        actions = {new: f"moved from `{relative_path(old)}`", old: "moved"}
        actions |= {page: "links updated" for page in updated}
        message = message or f"{relative_path(old)} moved to {relative_path(new)}"
        _commit(repo, ws, actor, source, {old: None, **writes}, actions, message)
    _after_write(session, actor, space, "content.moved", new, source)
    return [relative_path(page) for page in updated]
