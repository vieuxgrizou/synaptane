"""Where a space's files live on the server, and with which Git identity we write.

- `repos_dir/<space_id>/`: working copy of the repository (source of truth: the remote).
- `extract_dir/<space_id>/`: text extracted from PDFs, Office documents and images, for
  search. Can be rebuilt from Git at any time (`synaptane reindex`).
"""

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from synaptane.config import Settings
from synaptane.core.errors import not_found
from synaptane.core.security import SecretBox
from synaptane.git.repo import NETWORK_TIMEOUT, Identity, Repository
from synaptane.storage.models import Space, User


@dataclass(frozen=True)
class Workspace:
    repos_dir: Path
    extract_dir: Path
    committer: Identity
    box: SecretBox
    allow_local_repos: bool = False
    network_timeout: int = NETWORK_TIMEOUT
    ocr_languages: str = "fra+eng"
    ocr_images: bool = True
    public_url: str = ""  # for the links in emails sent by the worker
    meaning_min_similarity: float | None = None  # see Settings
    rerank_model: str | None = None  # see Settings
    embed_front_matter: bool = False  # see Settings

    @classmethod
    def from_settings(cls, settings: Settings, box: SecretBox) -> "Workspace":
        return cls(
            repos_dir=settings.repos_dir,
            extract_dir=settings.data_dir / "extract",
            committer=committer_identity(settings),
            box=box,
            allow_local_repos=settings.allow_local_repos,
            network_timeout=settings.git_timeout,
            ocr_languages=settings.ocr_languages,
            ocr_images=settings.ocr_images,
            public_url=settings.public_url,
            meaning_min_similarity=settings.meaning_min_similarity,
            rerank_model=settings.rerank_model,
            embed_front_matter=settings.embed_front_matter,
        )

    def clone_path(self, space_id: UUID) -> Path:
        return self.repos_dir / str(space_id)

    def extract_path(self, space_id: UUID) -> Path:
        return self.extract_dir / str(space_id)

    def extracted_file(self, space_id: UUID, repo_path: str) -> Path:
        """Text extracted from a repository file (`content/…`): PDF, Office document, image."""
        return self.extract_path(space_id) / f"{repo_path}.txt"

    def extracted_text(self, space_id: UUID, repo_path: str) -> str | None:
        """Extracted text, or None if there is none (not indexed yet, nothing to extract)."""
        cache = self.extracted_file(space_id, repo_path)
        return cache.read_text(encoding="utf-8", errors="replace") if cache.is_file() else None

    def recovery_path(self, space_id: UUID) -> Path:
        """Bundles of local commits set aside (repository changed, history rewritten)."""
        return self.repos_dir.parent / "recuperation" / str(space_id)

    def local_repository(self, space: Space) -> Repository:
        """Local copy to read or write (no network). Not found if never synchronized."""
        repo = Repository(
            self.clone_path(space.id), space.repo_url or "", space.branch, self.committer
        )
        if not repo.exists() or repo.head() is None:
            raise not_found("This space has not been synchronized with its repository yet.")
        return repo


def _host(settings: Settings) -> str:
    return urlsplit(settings.public_url).hostname or "localhost"


def committer_identity(settings: Settings) -> Identity:
    return Identity("Synaptane", f"synaptane@{_host(settings)}")


def author_identity(user: User, committer: Identity) -> Identity:
    """Git author of a human action: the account's display name and email."""
    domain = committer.email.split("@", 1)[1]
    return Identity(user.display_name, user.email or f"{user.username}@users.{domain}")
