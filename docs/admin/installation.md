English | [Français](installation.fr.md)

# Install and run Synaptane

Two ways to install:
- **Docker Compose** (recommended), on a Linux server, with the published image or an image
  you build yourself;
- **from source**, for development or for a server without Docker.

Synaptane runs on Linux, or WSL2 on Windows. macOS is not tested.

## With Docker Compose

Requirements:
- Docker Engine with the Compose plugin;
- a domain name that points to the server: HTTPS is required for cookies, OAuth, single
  sign-on and the Claude.ai and ChatGPT connectors.

The published image `ghcr.io/synaptane/synaptane` contains everything else: Git, ripgrep,
Tesseract (English and French), catdoc (old Office files), the PostgreSQL client for backups,
age and rclone. It does **not** contain the Claude Code and Codex CLIs, needed only to run the
AI agent on a Claude or ChatGPT subscription: for that,
[build the image yourself](#build-the-image-yourself).

**Get the files and start.** The repository provides `compose.yaml`, `.env.example`, the nginx
configuration and the server scripts. Clone the release, then start the published image:

```sh
git clone --branch v1.0.0-rc.1 https://github.com/synaptane/synaptane.git ~/synaptane
cd ~/synaptane
cp .env.example .env
chmod 600 .env                # it holds the database password
openssl rand -hex 24          # copy into POSTGRES_PASSWORD
nano .env                     # PUBLIC_URL, POSTGRES_PASSWORD, mode (see below)
docker compose pull
docker compose up -d
docker compose exec app synaptane admin create --username admin --display-name "Admin"
docker compose exec app synaptane doctor
```

Git says the copy is in "detached HEAD" state: this is normal after cloning a tag, and you
have nothing to commit there.

`admin create` asks for the password, at least 12 characters long.

`SYNAPTANE_VERSION` picks the image tag. `.env.example` sets it to the release you cloned, and
`compose.yaml` falls back to that same version if it is missing: the image and the Compose
file always match. There is no `latest` to follow: an upgrade is always a choice (see
[Upgrade](#upgrade)).

`doctor`, `synaptane --version` and the footer of the administration page show the version
and commit in service (for example `1.0.0rc1 (abc1234)`).

The services:
- `postgres`: PostgreSQL 17 with pgvector;
- `migrate`: applies the migrations, then stops;
- `app`: web, REST API and MCP;
- `worker`: Git sync, indexing, tidy-up, routines and webhooks.

Data lives in Docker volumes:
- `postgres-data`;
- `synaptane-data`: master key, repository clones, extracted text;
- `synaptane-agents`: CLI logins for subscriptions, mounted in the worker only;
- `synaptane-backups`: where `synaptane backup` writes without `--output`. It is on the same
  disk and **`docker compose down -v` deletes it** with the others: copy backups off the
  server (see [Backup](backup.md)).

### Build the image yourself

Build from a release tag if you want the Claude Code and Codex CLIs (subscriptions), or if you
prefer not to pull a prebuilt image. `compose.yaml` alone never builds: the build is declared
in `compose.build.yaml`, which you add through `COMPOSE_FILE`.

```sh
git clone --branch v1.0.0-rc.1 https://github.com/synaptane/synaptane.git ~/synaptane
cd ~/synaptane
cp .env.example .env && chmod 600 .env
nano .env                     # as above, plus these two lines:
                              #   COMPOSE_FILE=compose.yaml:compose.build.yaml
                              #   SYNAPTANE_IMAGE=synaptane:local
GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build   # commit recorded in the image
docker compose exec app synaptane admin create --username admin --display-name "Admin"
```

The build takes about ten minutes. `AGENT_CLIS=true` (the default) installs Claude Code and
Codex, which are third-party software under their own terms; `AGENT_CLIS=false` in `.env`
leaves them out.

Keep a local name such as `synaptane:local` (`compose.build.yaml` uses it when
`SYNAPTANE_IMAGE` is unset), never the name of the published image: the next
`docker compose pull` would replace your build. In proxy mode, the nginx image is built too,
as `synaptane-nginx:local`. To update only the downloaded images (PostgreSQL, certbot), use
`docker compose pull --ignore-buildable`.

### Choose the mode: behind the server's nginx, or all-in-one

**Default mode: the server already runs nginx** (or Caddy, Traefik…) for other sites. Leave
`COMPOSE_PROFILES` empty. Synaptane then listens on `127.0.0.1:8000` (`APP_PORT`), reachable
only from the server, and the host's nginx handles HTTPS:

```sh
sudo LETSENCRYPT_EMAIL=you@example.com deploy/host/install-nginx.sh brain.example.com
```

The script installs the site `deploy/nginx/host-site.conf` with your domain, after getting the
Let's Encrypt certificate (certbot in *webroot* mode, without touching other sites): HTTP
redirected to HTTPS, HSTS, `server_tokens off`, access log without tokens. Put
`APP_PORT=…` before the command if Synaptane does not listen on 8000. Without the script: copy
this file to `/etc/nginx/sites-available/`, replace `brain.example.com`, and get the
certificate before enabling the 443 block.

The site turns off buffering (`proxy_buffering off`) for MCP and accepts 100 MB uploads. Do
not publish port 8000 on the Internet (keep `APP_BIND` at `127.0.0.1`): the application trusts
the `X-Forwarded-*` headers that come from Docker networks (`FORWARDED_ALLOW_IPS`,
`172.16.0.0/12,192.168.0.0/16` by default). If the log shows the same address for every
visitor, this setting does not match your network, and the login attempt limit would count
everyone together.

**All-in-one mode: nothing else runs on the server.** In `.env`:

```sh
COMPOSE_PROFILES=proxy
DOMAIN=brain.example.com
LETSENCRYPT_EMAIL=you@example.com
```

An nginx container listens on ports 80 and 443:
- certbot gets the Let's Encrypt certificate, then renews it automatically;
- nginx serves a temporary self-signed certificate meanwhile, then reloads the real one as
  soon as it arrives (about a minute);
- for a first try, `LETSENCRYPT_STAGING=1` gives test certificates, without hitting Let's
  Encrypt rate limits.

The domain must already point to the server, with ports 80 and 443 open. If it fails, certbot
retries every hour (`docker compose logs certbot`).

To switch modes, change `COMPOSE_PROFILES`, then run `docker compose up -d --remove-orphans`.

### Upgrade

Read the [changelog](../../CHANGELOG.md) for the target version first. Migrations run on
their own at startup (the `migrate` service logs each one). Expect a few tens of seconds of
downtime.

**With the published image:**

```sh
cd ~/synaptane
umask 077   # the backup contains the master key
docker compose exec -T app synaptane backup --output - > synaptane-before-upgrade.tar.gz
git fetch --tags && git checkout v1.0.0      # Compose file and scripts of the new version
nano .env                                    # SYNAPTANE_VERSION=1.0.0
docker compose pull
docker compose up -d
docker compose exec app synaptane --version  # the expected version?
```

**With a self-built image** (`COMPOSE_FILE` and `SYNAPTANE_IMAGE` set as in
[Build the image yourself](#build-the-image-yourself)), `deploy/host/deploy.sh` does it all on
the server:

```sh
cd ~/synaptane
deploy/host/deploy.sh v1.0.0      # a tag, a branch or a commit
```

The script takes a backup, keeps the image in service, builds the new one with its commit,
restarts, then checks for up to 3 minutes (`/readyz`, version, worker, `doctor`). **If
something fails, it rolls back on its own**: previous commit and image, and restore of the
backup if a migration changed the database. Each deployment adds a line
`date commit result` to `~/.local/state/synaptane/deploy.log`. `DRY_RUN=1` shows what would
be done; `--check-ci` refuses a commit whose CI is not green. Details:
[`deploy/host/README.md`](../../deploy/host/README.md).

By hand, if the script does not suit you:

```sh
umask 077
docker compose exec -T app synaptane backup --output - > synaptane-before-upgrade.tar.gz
git fetch --tags && git checkout v1.0.0
docker compose pull --ignore-buildable      # downloaded images: PostgreSQL, certbot
GIT_SHA=$(git rev-parse HEAD) docker compose build --pull --no-cache  # with Debian fixes
docker compose up -d
docker compose exec app synaptane --version
```

Upgrade at least once a month to get operating system security fixes: with the published
image, move to the latest release (each one is built with the Debian fixes of the day); with a
self-built image, rebuild with `--pull --no-cache`, even without a new version (`deploy.sh`
rebuilds with `--pull`). Downloaded images (PostgreSQL, certbot) are updated separately, after a backup:
`docker compose pull --ignore-buildable && docker compose up -d`.

### Roll back

To go back to an earlier version **with no migration in between**: with the published image,
set `SYNAPTANE_VERSION` back to it in `.env` and run `docker compose up -d`; with a self-built
image, deploy its tag or commit (it is in the log): `deploy/host/deploy.sh <commit>`.
`deploy.sh` already rolls back on its own when its checks fail.

If a migration changed the database in between, the old code does not start on the migrated
database: also restore the backup taken just before the faulty upgrade
(`synaptane-before-upgrade.tar.gz`, or with `deploy.sh`
`~/synaptane-backups/synaptane-…-avant-<faulty commit>.tar.gz`):

```sh
docker compose stop app worker
git checkout --detach v1.0.0-rc.1               # the previous version
nano .env                                       # published image: SYNAPTANE_VERSION=1.0.0-rc.1
# self-built image: GIT_SHA=$(git rev-parse HEAD) docker compose build
docker compose run --rm --no-deps -T app synaptane restore --force - < synaptane-before-upgrade.tar.gz
docker compose up -d
```

The database is restored to the state of the backup taken before the upgrade (schema
included); the Git repositories remain the reference for content.

### Logs

`docker compose logs -f app worker`: one line per event, with the time. The worker writes one
line per job (sync, indexing, tidy-up: duration, result). `SYNAPTANE_LOG_LEVEL=DEBUG` in
`.env` gives more detail; `WARNING` gives less.

### Claude and ChatGPT subscriptions

This requires a [self-built image](#build-the-image-yourself) with the CLIs. They log in once,
in the worker container. The login is stored in the `synaptane-agents` volume, so it survives
upgrades.

```sh
docker compose exec worker claude auth login         # Claude subscription
docker compose exec worker codex login --device-auth # ChatGPT subscription
```

Check the provider's terms first: see [AI and tidy-up](ai.md).

### Other settings

The options below use `SYNAPTANE_<OPTION>` variables. To add one, create a
`compose.override.yaml` file. Compose reads it automatically only when `COMPOSE_FILE` is not
set; if `.env` sets `COMPOSE_FILE` (self-built image, demo), add it to the list, for example
`COMPOSE_FILE=compose.yaml:compose.build.yaml:compose.override.yaml`.

```yaml
services:
  app:
    environment:
      SYNAPTANE_SESSION_DAYS: "14"
  worker:
    environment:
      SYNAPTANE_SESSION_DAYS: "14"
```

## From source

Requirements: Python 3.12 with [uv](https://docs.astral.sh/uv/), and PostgreSQL 17 with the
pgvector extension.

```sh
uv sync
uv run synaptane init --database-url postgresql+psycopg://synaptane:PASSWORD@localhost:5432/synaptane \
                      --data-dir /var/lib/synaptane --public-url https://brain.example.com
uv run synaptane db upgrade
uv run synaptane admin create --username admin --display-name "Admin"
uv run synaptane doctor
uv run synaptane serve --host 127.0.0.1 --port 8000   # web, REST and MCP
uv run synaptane worker                                # background jobs
```

System tools:
- Git 2.28 or later;
- OpenSSH;
- **ripgrep** (`rg`), for search;
- **Tesseract**, optional but recommended: text of scanned PDFs and images (`tesseract-ocr`
  with `tesseract-ocr-eng` and `tesseract-ocr-fra`);
- **catdoc**, optional but recommended: text of the old Word and Excel files `.doc` and `.xls`
  (`catdoc` package, which also provides `xls2csv`; old `.ppt` files are read without it);
- the PostgreSQL client (`pg_dump`, `pg_restore`), same version as the server, for backups;
- `age` and `rclone`, for encrypted off-site backups.

`synaptane doctor` checks that they are present.

## Configuration

`init` writes `synaptane.toml` (mode 600) and generates the **master key** `master.key` in the
data folder. With Docker, configuration goes through environment variables and the key is
generated on first start. This key encrypts the secrets stored in the database, such as Git
credentials and AI keys. **Back it up outside the database**: without it, these secrets are
lost. See [Backup and restore](backup.md).

Each option of `synaptane.toml` can be overridden by an environment variable
`SYNAPTANE_<OPTION>`, for example `SYNAPTANE_DATABASE_URL`. The file is chosen with
`--config` or `SYNAPTANE_CONFIG`.

| Option | Purpose |
|---|---|
| `public_url` | Public URL. With `https://`, cookies are marked `Secure`. It is also the OAuth issuer and the MCP address (`<public_url>/mcp`): for Claude.ai, it must use HTTPS and be reachable from the Internet. |
| `database_url` | PostgreSQL connection (`postgresql+psycopg://…`). |
| `data_dir` | Master key and local copies of the repositories. |
| `default_locale` | Default language of the interface: `en` (default) or `fr`. It applies when the browser asks for neither, and to accounts created by an administrator or the CLI; everyone can change their own in **My account**. Variable: `SYNAPTANE_DEFAULT_LOCALE`. |
| `session_days` | Length of a web session, 30 days by default. |
| `poll_minutes` | Interval of the fallback sync, 5 minutes by default. |
| `git_timeout` | Overall timeout of an exchange with the Git host (clone, fetch, push), in seconds, 300 by default. Raise it only for the first clone of a very large repository. See [Git](git.md). |
| `allow_local_repos` | Allows `file://` repositories. **Development only.** |
| `ocr_languages` | Tesseract languages, `fra+eng` by default. |
| `ocr_images` | Extract text from images (OCR), `true` by default. |
| `allow_private_webhooks` | Allows webhooks over HTTP and to the local network. **Development only.** |
| `api_docs` | Publishes the interactive documentation `/api/docs` and the schema `/api/v1/openapi.json`, `false` by default. |
| `meaning_min_similarity` | Semantic search: a page found only by meaning is suggested once the best page reaches this similarity (between 0 and 1). Unset (default): it must clearly stand out from the other neighbors, which rarely happens in a space of similar pages (call transcripts, tickets). The scale depends on the embedding model: measure it on questions about the space and on unrelated ones. Compose variable: `MEANING_MIN_SIMILARITY`. |
| `rerank_model` | Semantic search: a Voyage rerank model (`rerank-2.5`, `rerank-2.5-lite`) that rescores the 40 best results for the question, with the organization's Voyage key. Unset (default): no reranking. Word-only searches are never reranked, and a provider outage keeps the usual order. Compose variable: `RERANK_MODEL`. |
| `embed_front_matter` | Semantic search: each chunk of a Markdown page is embedded with the short values of the page's front matter (identifiers and long values left out), so that a chunk deep in the page keeps what the page is about. Useful for pages generated with metadata (call transcripts, tickets). `false` by default; run `synaptane reindex` after changing it. Compose variable: `EMBED_FRONT_MATTER`. |
| `backup_age_recipients` | age public keys (`age1…`) that `backup --offsite` encrypts for. As an environment variable: comma-separated. See [Backup](backup.md). |
| `backup_age_recipients_file` | Or a file of age recipients. |
| `backup_remotes` | rclone destinations of the off-site copy (`name:path`, or a folder). |
| `backup_retention_days` | Older remote archives are deleted, 30 days by default; `0`: never. |
| `backup_rclone_config` | rclone configuration. Defaults to `$RCLONE_CONFIG` (`/data/rclone/rclone.conf` with Docker), otherwise `<data_dir>/rclone/rclone.conf`. |

With Docker Compose, these variables are set in `.env` (see the comments in `.env.example`):

| Variable | Purpose |
|---|---|
| `PUBLIC_URL`, `POSTGRES_PASSWORD` | Public address and database password (required). |
| `COMPOSE_PROFILES` | Empty (behind the server's nginx) or `proxy` (all-in-one). |
| `COMPOSE_FILE` | Compose files, separated by `:`. Unset: `compose.yaml`, plus `compose.override.yaml` if it exists. |
| `SYNAPTANE_VERSION` | Tag of the published images, the version of `compose.yaml` by default. |
| `SYNAPTANE_IMAGE`, `SYNAPTANE_NGINX_IMAGE` | Another image for Synaptane, or for nginx in proxy mode (`synaptane:local`, `synaptane-nginx:local` for a build). |
| `GIT_SHA`, `AGENT_CLIS` | Build only (`compose.build.yaml`): commit recorded in the image, subscription CLIs (`true` by default). |
| `APP_PORT`, `APP_BIND`, `FORWARDED_ALLOW_IPS` | Default mode: local port (8000), its address (`127.0.0.1`), trusted proxy networks. |
| `DOMAIN`, `LETSENCRYPT_EMAIL`, `LETSENCRYPT_STAGING`, `HTTP_PORT`, `HTTPS_PORT` | Proxy mode: domain, Let's Encrypt account and test certificates, published ports (80 and 443). |
| `BACKUP_AGE_RECIPIENTS`, `BACKUP_REMOTES`, `BACKUP_RETENTION_DAYS` | Off-site backup (see [Backup](backup.md)). |
| `SYNAPTANE_DEFAULT_LOCALE`, `OCR_LANGUAGES`, `TZ` | Interface language, OCR languages, time zone. |
| `SYNAPTANE_WORKER_MEMORY`, `SYNAPTANE_LOG_LEVEL` | Worker memory cap (`2g`), log level. |

## Email, invitations and password reset

**Administration → Email**: SMTP server, port, security (STARTTLS or SSL), username,
password (encrypted, never shown again) and sender address. **Send a test email** checks the
setting, to your account's address.

With email set up:
- an organization admin **invites** people from the **Members** tab. They choose the address,
  the role in the organization and, optionally, a role per space. The person gets a link
  valid for 14 days and creates their account. If the address is the **verified** address of
  an existing account, access is granted at once;
- **Forgot your password?** on the sign-in page sends a link valid for 30 minutes.

Without email, invitations still work: the link is shown once to the person who invites, who
passes it on. Password resets then go through a super admin (Administration → Accounts).

A password chosen by an admin, when creating the account or resetting it, must be changed at
first sign-in. People sign in with their username or their email.

**Verified addresses.** An address entered in **My account** or by an admin stays
"unverified" until it is confirmed. The link sent to that address (valid for 3 days) must be
opened while signed in to the account that asked for it, then confirmed with a button: a link
opened by someone else, or by a mail scanner, verifies nothing. Only a verified address is
used for invitations to an existing account, single sign-on linking and password reset.
Changing your email requires your current password, and the old address is notified. An
address that was entered but never verified does not block its real owner: it goes back to
them when they use it (invitation, single sign-on).

**Sending limits.** Each of these emails goes to an address typed in by someone who may not
own it, so their number is capped: 3 verification links per account per hour, and 30
invitations per organization per hour (counted whether or not the email is sent). Beyond
that, the request is refused (HTTP 429) until the hour has passed.

**Sign-in attempts.** After 10 failures in 15 minutes from the same IP address, the account is
blocked for that address; after 50 failures from all addresses, for everyone. Variants of a
login (spaces, case, email) count for the same account. The log names the account by its
identifier; a login that matches no account is logged as a short fingerprint
(`login:` and 8 hexadecimal characters), never as typed, since it may be a password entered
in the wrong field.

## Search index

Text extracted from PDFs, Office documents and images is stored outside the repositories, in
`data_dir/extract/`.

Each document is read by a separate, bounded process, so that a malicious or oversized file
(large-format plan, scan of hundreds of pages) cannot bring the worker down:
- 1 GB of memory at most (Tesseract and catdoc included) and 10 minutes per document;
- pages rendered for OCR capped at 40 million pixels (300 dpi for a common format, less for
  a large format);
- 500 pages read, of which at most 100 go through OCR, 60 seconds per page; beyond that, the
  native text stays indexed and the worker log says so;
- images over 250 million pixels are skipped.

A file that fails (timeout, memory, invalid file) is recorded as such in
`data_dir/extract/<space>/manifest.json`, with the reason, and is not retried until it
changes: it is recorded before it is even read, so a worker stopping halfway does not repeat
forever (two attempts at most). `synaptane reindex` retries everything, failures included.

The index can be rebuilt from Git at any time:

```sh
uv run synaptane reindex                  # all spaces
uv run synaptane reindex --space acme/infra
docker compose exec app synaptane reindex   # with Docker
```

## Alerts

The admins of an organization are notified, by email if it is set up and by the `space.alert`
webhook, when something needs their attention, for example:
- the tidy-up of a space keeps failing (not just a network glitch), or stops before the end;
- a space no longer syncs with its repository (credentials refused, repository not found);
- an inbox holds items that nobody has looked at for more than 3 days;
- the tidy-up agent reaches the deletion cap, or makes large changes.

The full list of alert kinds is in [Webhooks](webhooks.md). The same alert (kind and space)
is not sent again within 20 hours. The AI budget has its own alerts (see
[AI and tidy-up](ai.md)).

## Roles

| Role | Scope | Can |
|---|---|---|
| Super admin | Instance | Everything: accounts, all organizations, and reading and writing all content in the web UI and the REST API. Through an assistant (MCP), only their actual memberships. |
| Organization admin | Organization | Create and configure spaces, manage members. Contributor on every space of the organization. |
| Contributor | Space | Read, search, drop notes into the inbox, edit content. |
| Reader | Space | Read and search. |

Adding someone to a space automatically makes them a plain member of the organization.
Removing them from the organization also removes them from its spaces.

## First steps

On first start, the super admin's home page lists these steps.

1. **Administration**: the **Organizations** tab to create them (box checked by default: you
   become their admin, otherwise your assistants would see nothing), the **Accounts** tab to
   create accounts (each account has its own page: rights, password).
2. On the organization page, one page per topic:
   - **Spaces**, then **New space**: name, identifier, Git repository address;
   - **Members**: add people, choose admins;
   - **Artificial intelligence**: the provider of the nightly tidy-up;
   - **Semantic search** and **Webhooks**, optional;
   - **Settings**: name, description, deletion.
3. On a space, the **Settings** tab: **Git repository** (address, access, webhook, status
   and conflicts), **Members** (reader or contributor), **Tidy-up** (time, time zone, model),
   **Semantic search** and **General**.

Deleting a space or an organization requires typing its identifier again.

To link a space to its repository, see [Git](git.md). For sign-in with GitLab, GitHub, Google
or OIDC, see [SSO](sso.md); to notify other tools, see [Webhooks](webhooks.md). All of this is
also possible through the [REST API](../reference/api.md).

## Demo data

To try Synaptane before linking your own repositories, `synaptane demo` loads the wiki of a
fictional IT team ([examples/acme-wiki](../../examples/acme-wiki/README.md)): 50 pages, a space
purpose, six inbox notes covering typical tidy-up cases, verified pages, a page to review and a
broken link. Eight files that are not Markdown are linked from the pages, to see what
Synaptane does with them: a PDF contract, a scanned delivery note (OCR), a Word checklist, a
slide deck, two spreadsheets (one of them in the old `.xls` format), a rack diagram (OCR) and an
HTML status report. Search for "Kestrel", a word that only appears in the scan (Tesseract
required, included in the Docker image). On an initialized and migrated instance, it creates
what is missing, then syncs and indexes the space:
- a `demo` super admin, whose password is printed once (or given by
  `SYNAPTANE_DEMO_PASSWORD`, or `--password`);
- the `acme` organization, with `demo` as admin;
- the `acme/it` space, backed by a local Git repository, `<data_dir>/demo/acme-it.git`.

Running it again changes nothing: an existing account keeps its password, and what you changed
in the space stays.

The repository is local (`file://`): Synaptane refuses it unless
`SYNAPTANE_ALLOW_LOCAL_REPOS=true` is set for the command, the server and the worker. **Demo
and development only**: this setting lets an organization admin link a space to any Git folder
of the server. Run the demo on a throwaway instance, not on your production one.

With Docker Compose, `examples/compose.demo.yaml` sets it for `app` and `worker`:

```sh
echo 'COMPOSE_FILE=compose.yaml:examples/compose.demo.yaml' >> .env
docker compose up -d
docker compose exec app synaptane demo
```

With a self-built image: `COMPOSE_FILE=compose.yaml:compose.build.yaml:examples/compose.demo.yaml`.

From source:

```sh
export SYNAPTANE_ALLOW_LOCAL_REPOS=true   # in the shells of serve and worker too
uv run synaptane demo
```

The command prints what it created (`super admin demo`, `organization acme`…), the address to
open, the `demo` password and how to connect an AI assistant. It also says what keeps the demo
repository in sync: in a container, the Compose `worker` service, to leave running; from source,
a `synaptane worker` to keep running, with `SYNAPTANE_ALLOW_LOCAL_REPOS=true`. Tidying the inbox
needs an AI provider (organization page, **Artificial intelligence**). `docker compose down -v`
deletes the whole demo instance.

## Appendix: private fork

If you deploy from your own **private** fork, `git clone https://…` asks for credentials and
fails on a server. Give the server a **deploy key** instead: its own SSH key, read-only,
declared on that repository only (GitHub → Settings → Deploy keys, without write access). Then
clone through an SSH host alias that uses only that key:

```sh
git clone git@<ssh-alias>:<account>/synaptane.git ~/synaptane
```

Creating the key and the SSH alias in `~/.ssh/config`: see
[`deploy/host/README.md`](../../deploy/host/README.md). The rest of the
installation is the same as [building the image yourself](#build-the-image-yourself).
