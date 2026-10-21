"""Configuration: a TOML file, overridden by the `SYNAPTANE_*` environment variables.

Example (`synaptane.toml`):

    public_url = "https://brain.example.com"
    database_url = "postgresql+psycopg://synaptane:secret@localhost:5432/synaptane"
    data_dir = "/var/lib/synaptane"
"""

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ENV_PREFIX = "SYNAPTANE_"
DEFAULT_CONFIG = Path("synaptane.toml")


class ConfigError(Exception):
    pass


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    public_url: str = "http://127.0.0.1:8000"
    database_url: str
    data_dir: Path
    session_days: int = 30
    poll_minutes: int = 5  # fallback sync when no webhook arrives
    git_timeout: int = 300  # seconds: overall timeout of an exchange with the Git host
    allow_local_repos: bool = False  # file:// repositories: development and tests only
    ocr_languages: str = "fra+eng"  # Tesseract languages for scanned PDFs and images
    ocr_images: bool = True  # extract text from images for search
    allow_private_webhooks: bool = False  # webhooks to the local network: development only
    api_docs: bool = False  # publish /api/docs and the OpenAPI schema (lists every route)
    # Semantic search: a page found by meaning counts once the best neighbour reaches this
    # cosine similarity (depends on the embedding model). Unset: the best neighbour must stand
    # out from the median of the others, which fails on a corpus of similar pages.
    meaning_min_similarity: float | None = Field(default=None, gt=0, lt=1)
    # Semantic search: rerank model (Voyage, e.g. `rerank-2.5`) that rescores the best
    # results for the question, with the organization's Voyage key. Unset: no reranking.
    rerank_model: str | None = None
    # Semantic search: each chunk of a Markdown page is embedded with the short values of the
    # page's front matter, so that a chunk deep in the page keeps what the page is about.
    # Takes effect on the next `synaptane reindex`.
    embed_front_matter: bool = False
    # Interface language when the browser asks for none that is supported, and initial
    # language of accounts created without a browser (CLI, administrator).
    default_locale: Literal["en", "fr"] = "en"
    # Off-site copy (`synaptane backup --offsite`, docs/admin/backup.md). Lists are written in
    # TOML (`["a", "b"]`) or, as an environment variable, separated by commas or newlines.
    backup_age_recipients: tuple[str, ...] = ()  # age public keys (`age1…`, `ssh-ed25519 …`)
    backup_age_recipients_file: Path | None = None  # or a recipients file (`age -R`)
    backup_remotes: tuple[str, ...] = ()  # rclone destinations: `remote:path`, or a folder
    backup_retention_days: int = 30  # older remote archives deleted; 0: never
    backup_rclone_config: Path | None = None  # default: $RCLONE_CONFIG, else <data_dir>/rclone/

    @field_validator("public_url")
    @classmethod
    def _public_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("public_url must start with http:// or https://")
        return value.rstrip("/")

    @field_validator("meaning_min_similarity", "rerank_model", mode="before")
    @classmethod
    def _optional_number(cls, value: object) -> object:
        """An empty environment variable (Docker Compose default) means unset."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("backup_age_recipients", "backup_remotes", mode="before")
    @classmethod
    def _list(cls, value: object) -> object:
        """A string (environment variable) becomes a list. Neither spaces nor ";" as
        separators: an SSH key contains spaces, and so does a Google Drive folder."""
        if isinstance(value, str):
            value = value.replace("\n", ",").split(",")
        if isinstance(value, list | tuple):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return value

    @field_validator("backup_age_recipients")
    @classmethod
    def _recipients(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for recipient in value:
            if not recipient.startswith(("age1", "ssh-ed25519 ", "ssh-rsa ")):
                raise ValueError("age public key expected (age1…, ssh-ed25519 … or ssh-rsa …)")
        return value

    @field_validator("backup_remotes")
    @classmethod
    def _remotes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for remote in value:
            # No rclone option disguised as a destination; no connection parameters
            # (`:s3,secret_access_key=…:`): credentials stay in the rclone config.
            if remote.startswith("-") or "\0" in remote:
                raise ValueError("invalid rclone destination")
            if ":" in remote:
                backend = (
                    remote.split(":", 2)[1] if remote.startswith(":") else remote.split(":")[0]
                )
                if "," in backend or "=" in backend:
                    raise ValueError("connection parameters not allowed: use rclone config")
        return value

    @field_validator("backup_age_recipients_file", "backup_rclone_config", mode="before")
    @classmethod
    def _optional_path(cls, value: object) -> object:
        return None if value == "" else value  # empty variable in compose.yaml

    @field_validator("backup_retention_days")
    @classmethod
    def _retention(cls, value: int) -> int:
        if value < 0:
            raise ValueError("a number of days of zero or more is expected")
        return value

    @property
    def rclone_config(self) -> Path:
        """rclone configuration (destinations and their credentials), outside the database."""
        if self.backup_rclone_config:
            return self.backup_rclone_config
        if os.environ.get("RCLONE_CONFIG"):
            return Path(os.environ["RCLONE_CONFIG"])
        return self.data_dir / "rclone" / "rclone.conf"

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def key_file(self) -> Path:
        """Master key encrypting the secrets (32 bytes, to back up outside the database)."""
        return self.data_dir / "master.key"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(os.environ.get(ENV_PREFIX + "CONFIG", DEFAULT_CONFIG))
    data: dict[str, object] = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError(f"Unreadable configuration ({path}): {error}") from None
    for field in Settings.model_fields:
        env = os.environ.get(ENV_PREFIX + field.upper())
        if env is not None:
            data[field] = env
    data_dir = data.get("data_dir")
    if isinstance(data_dir, str) and not Path(data_dir).is_absolute():
        data["data_dir"] = (path.parent / data_dir).resolve()
    try:
        return Settings.model_validate(data)
    except ValidationError as error:
        # No values in the message: database_url can contain a password.
        fields = ", ".join(".".join(map(str, e["loc"])) for e in error.errors())
        raise ConfigError(f"Invalid or incomplete configuration: {fields}") from None
