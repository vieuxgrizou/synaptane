[English](installation.md) | Français

# Installer et exploiter Synaptane

Deux façons d'installer :
- **Docker Compose** (recommandé), sur un serveur Linux, avec l'image publiée ou une image que
  vous construisez vous-même ;
- **depuis les sources**, pour développer ou pour un serveur sans Docker.

Synaptane tourne sous Linux, ou WSL2 sous Windows. macOS n'est pas testé.

## Avec Docker Compose

Prérequis :
- Docker Engine avec le plugin Compose ;
- un nom de domaine qui pointe vers le serveur : HTTPS est indispensable pour les cookies,
  OAuth, le SSO et les connecteurs de Claude.ai et ChatGPT.

L'image publiée `ghcr.io/synaptane/synaptane` contient tout le reste : Git, ripgrep,
Tesseract (anglais et français), catdoc (anciens fichiers Office), le client PostgreSQL pour
les sauvegardes, age et rclone. Elle ne contient **pas** les CLI Claude Code et Codex, qui ne
servent qu'à faire tourner l'agent IA sur un abonnement Claude ou ChatGPT : pour cela,
[construisez l'image vous-même](#construire-limage-vous-même).

**Récupérer les fichiers et démarrer.** Le dépôt fournit `compose.yaml`, `.env.example`, la
configuration nginx et les scripts du serveur. Clonez la version, puis lancez l'image
publiée :

```sh
git clone --branch v1.0.0-rc.1 https://github.com/synaptane/synaptane.git ~/synaptane
cd ~/synaptane
cp .env.example .env
chmod 600 .env                # il contient le mot de passe de la base
openssl rand -hex 24          # à copier dans POSTGRES_PASSWORD
nano .env                     # PUBLIC_URL, POSTGRES_PASSWORD, mode (voir plus bas)
docker compose pull
docker compose up -d
docker compose exec app synaptane admin create --username admin --display-name "Admin"
docker compose exec app synaptane doctor
```

Git indique que la copie est en « detached HEAD » : c'est normal après le clone d'une
étiquette, et vous n'avez rien à y committer.

`admin create` demande le mot de passe, de 12 caractères au moins.

`SYNAPTANE_VERSION` choisit l'étiquette de l'image. `.env.example` la règle sur la version
clonée, et `compose.yaml` prend cette même version si elle manque : l'image et le fichier
Compose vont toujours ensemble. Il n'y a pas de `latest` à suivre : une mise à jour est
toujours un choix (voir [Mettre à jour](#mettre-à-jour)).

`doctor`, `synaptane --version` et le pied de la page d'administration affichent la version
et le commit en service (par exemple `1.0.0rc1 (abc1234)`).

Les services :
- `postgres` : PostgreSQL 17 avec pgvector ;
- `migrate` : applique les migrations, puis s'arrête ;
- `app` : web, API REST et MCP ;
- `worker` : synchronisation Git, indexation, rangement, routines et webhooks.

Les données sont dans des volumes Docker :
- `postgres-data` ;
- `synaptane-data` : clé maître, copies des dépôts, texte extrait ;
- `synaptane-agents` : connexions des CLI pour les abonnements, monté dans le seul worker ;
- `synaptane-backups` : là où `synaptane backup` écrit sans `--output`. Il est sur le même
  disque et **`docker compose down -v` l'efface** avec les autres : copiez les sauvegardes
  hors du serveur (voir [Sauvegarde](backup.fr.md)).

### Construire l'image vous-même

Construisez depuis une étiquette de version si vous voulez les CLI Claude Code et Codex
(abonnements), ou si vous préférez ne pas tirer une image toute faite. `compose.yaml` seul ne
construit jamais : la construction est déclarée dans `compose.build.yaml`, que vous ajoutez
par `COMPOSE_FILE`.

```sh
git clone --branch v1.0.0-rc.1 https://github.com/synaptane/synaptane.git ~/synaptane
cd ~/synaptane
cp .env.example .env && chmod 600 .env
nano .env                     # comme plus haut, plus ces deux lignes :
                              #   COMPOSE_FILE=compose.yaml:compose.build.yaml
                              #   SYNAPTANE_IMAGE=synaptane:local
GIT_SHA=$(git rev-parse HEAD) docker compose up -d --build   # commit inscrit dans l'image
docker compose exec app synaptane admin create --username admin --display-name "Admin"
```

La construction prend une dizaine de minutes. `AGENT_CLIS=true` (par défaut) installe Claude
Code et Codex, logiciels tiers soumis à leurs propres conditions ; `AGENT_CLIS=false` dans
`.env` les laisse de côté.

Gardez un nom local comme `synaptane:local` (celui que prend `compose.build.yaml` quand
`SYNAPTANE_IMAGE` est vide), jamais le nom de l'image publiée : le prochain
`docker compose pull` remplacerait votre image. En mode proxy, l'image nginx est construite
aussi, sous le nom `synaptane-nginx:local`. Pour ne mettre à jour que les images téléchargées
(PostgreSQL, certbot) : `docker compose pull --ignore-buildable`.

### Choisir le mode : derrière le nginx du serveur, ou tout-en-un

**Mode par défaut : le serveur a déjà un nginx** (ou Caddy, Traefik…) qui sert d'autres
sites. Laissez `COMPOSE_PROFILES` vide. Synaptane écoute alors sur `127.0.0.1:8000`
(`APP_PORT`), joignable seulement depuis le serveur, et le nginx de l'hôte fait le HTTPS :

```sh
sudo LETSENCRYPT_EMAIL=vous@example.com deploy/host/install-nginx.sh brain.example.com
```

Le script installe le site `deploy/nginx/host-site.conf` avec votre domaine, après avoir
obtenu le certificat Let's Encrypt (certbot en *webroot*, sans toucher aux autres sites) :
HTTP redirigé vers HTTPS, HSTS, `server_tokens off`, journal sans jetons. Mettez
`APP_PORT=…` devant la commande si Synaptane n'écoute pas sur 8000. Sans le script : copiez
ce fichier dans `/etc/nginx/sites-available/`, remplacez `brain.example.com`, et obtenez le
certificat avant d'activer le bloc 443.

Le site désactive la mise en tampon (`proxy_buffering off`) pour le MCP et accepte des
envois de 100 Mo. Ne publiez pas le port 8000 sur Internet (laissez `APP_BIND`
à `127.0.0.1`) : l'application fait confiance aux en-têtes `X-Forwarded-*` venus des réseaux
Docker (`FORWARDED_ALLOW_IPS`, `172.16.0.0/12,192.168.0.0/16` par défaut). Si le journal
montre la même adresse pour tous les visiteurs, c'est que ce réglage ne correspond pas à
votre réseau : la limite des tentatives de connexion compterait alors tout le monde ensemble.

**Mode tout-en-un : rien d'autre ne tourne sur le serveur.** Dans `.env` :

```sh
COMPOSE_PROFILES=proxy
DOMAIN=brain.example.com
LETSENCRYPT_EMAIL=vous@example.com
```

Un nginx dans Docker écoute sur les ports 80 et 443 :
- certbot obtient le certificat Let's Encrypt, puis le renouvelle automatiquement ;
- nginx sert un certificat autosigné provisoire en attendant, puis recharge le vrai dès
  qu'il arrive (environ une minute) ;
- pour un premier essai, `LETSENCRYPT_STAGING=1` donne des certificats de test, sans
  risquer les limites de Let's Encrypt.

Le domaine doit déjà pointer vers le serveur, avec les ports 80 et 443 ouverts. En cas
d'échec, certbot réessaie toutes les heures (`docker compose logs certbot`).

On passe d'un mode à l'autre en changeant `COMPOSE_PROFILES`, puis
`docker compose up -d --remove-orphans`.

### Mettre à jour

Lisez d'abord le [journal des modifications](../../CHANGELOG.md) (en anglais) pour la version
visée. Les migrations s'appliquent toutes seules au démarrage (le service `migrate` journalise
chacune). Comptez une coupure de quelques dizaines de secondes.

**Avec l'image publiée :**

```sh
cd ~/synaptane
umask 077   # la sauvegarde contient la clé maître
docker compose exec -T app synaptane backup --output - > synaptane-avant-maj.tar.gz
git fetch --tags && git checkout v1.0.0      # fichier Compose et scripts de la nouvelle version
nano .env                                    # SYNAPTANE_VERSION=1.0.0
docker compose pull
docker compose up -d
docker compose exec app synaptane --version  # la version attendue ?
```

**Avec une image construite par vos soins** (`COMPOSE_FILE` et `SYNAPTANE_IMAGE` réglés
comme dans [Construire l'image vous-même](#construire-limage-vous-même)),
`deploy/host/deploy.sh` fait tout sur le serveur :

```sh
cd ~/synaptane
deploy/host/deploy.sh v1.0.0      # une étiquette, une branche ou un commit
```

Le script fait une sauvegarde, garde l'image en service, construit la nouvelle avec son
commit, redémarre, puis contrôle pendant 3 minutes au plus (`/readyz`, version, worker,
`doctor`). **En cas d'échec, il revient seul en arrière** : commit et image précédents, et
restauration de la sauvegarde si une migration a changé la base. Chaque déploiement laisse
une ligne `date commit résultat` dans `~/.local/state/synaptane/deploy.log`.
`DRY_RUN=1` montre ce qui serait fait ; `--check-ci` refuse un commit dont la CI n'est pas
verte. Détail : [`deploy/host/README.fr.md`](../../deploy/host/README.fr.md).

À la main, si le script ne convient pas :

```sh
umask 077
docker compose exec -T app synaptane backup --output - > synaptane-avant-maj.tar.gz
git fetch --tags && git checkout v1.0.0
docker compose pull --ignore-buildable      # images téléchargées : PostgreSQL, certbot
GIT_SHA=$(git rev-parse HEAD) docker compose build --pull --no-cache  # correctifs Debian compris
docker compose up -d
docker compose exec app synaptane --version
```

Mettez à jour au moins une fois par mois pour les correctifs de sécurité du système : avec
l'image publiée, passez à la dernière version (chacune est construite avec les correctifs
Debian du jour) ; avec une image construite par vos soins, reconstruisez avec
`--pull --no-cache`, même sans nouvelle version (`deploy.sh` reconstruit avec `--pull`). Les
images téléchargées (PostgreSQL, certbot) se mettent à jour
à part, après une sauvegarde : `docker compose pull --ignore-buildable && docker compose up -d`.

### Revenir en arrière

Pour revenir à une version antérieure **sans migration entre les deux** : avec l'image
publiée, remettez son numéro dans `SYNAPTANE_VERSION` (`.env`), puis `docker compose up -d` ;
avec une image construite par vos soins, déployez son étiquette ou son commit (il est dans le
journal) : `deploy/host/deploy.sh <commit>`. `deploy.sh` revient déjà seul en arrière quand
ses contrôles échouent.

Si une migration a changé la base entre-temps, l'ancien code ne démarre pas sur la base
migrée : il faut aussi restaurer la sauvegarde faite juste avant la mise à jour fautive
(`synaptane-avant-maj.tar.gz`, ou avec `deploy.sh`
`~/synaptane-backups/synaptane-…-avant-<commit fautif>.tar.gz`) :

```sh
docker compose stop app worker
git checkout --detach v1.0.0-rc.1               # la version précédente
nano .env                                       # image publiée : SYNAPTANE_VERSION=1.0.0-rc.1
# image construite par vos soins : GIT_SHA=$(git rev-parse HEAD) docker compose build
docker compose run --rm --no-deps -T app synaptane restore --force - < synaptane-avant-maj.tar.gz
docker compose up -d
```

La base est restaurée dans l'état de la sauvegarde faite avant la mise à jour (schéma
compris) ; les dépôts Git restent la référence pour le contenu.

### Journaux

`docker compose logs -f app worker` : une ligne par événement, avec l'heure. Le worker
écrit une ligne par tâche (synchronisation, indexation, rangement : durée, résultat).
`SYNAPTANE_LOG_LEVEL=DEBUG` dans `.env` donne plus de détail ; `WARNING` en donne moins.

### Abonnements Claude et ChatGPT

Il faut une [image construite par vos soins](#construire-limage-vous-même), avec les CLI.
Ils se connectent une fois, dans le conteneur du worker. Leur connexion est rangée dans le
volume `synaptane-agents`, donc elle survit aux mises à jour.

```sh
docker compose exec worker claude auth login         # abonnement Claude
docker compose exec worker codex login --device-auth # abonnement ChatGPT
```

Vérifiez d'abord les conditions de l'éditeur : voir [Intelligence artificielle](ai.fr.md).

### Autres réglages

Les options ci-dessous passent par des variables `SYNAPTANE_<OPTION>`. Pour en ajouter
une, créez un fichier `compose.override.yaml`. Compose ne le lit de lui-même que si
`COMPOSE_FILE` n'est pas défini ; si `.env` définit `COMPOSE_FILE` (image construite par vos
soins, démonstration), ajoutez-le à la liste, par exemple
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

## Depuis les sources

Prérequis : Python 3.12 avec [uv](https://docs.astral.sh/uv/), et PostgreSQL 17 avec
l'extension pgvector.

```sh
uv sync
uv run synaptane init --database-url postgresql+psycopg://synaptane:MOT_DE_PASSE@localhost:5432/synaptane \
                      --data-dir /var/lib/synaptane --public-url https://brain.example.com
uv run synaptane db upgrade
uv run synaptane admin create --username admin --display-name "Admin"
uv run synaptane doctor
uv run synaptane serve --host 127.0.0.1 --port 8000   # web, REST et MCP
uv run synaptane worker                                # tâches de fond
```

Outils système :
- Git 2.28 ou plus récent ;
- OpenSSH ;
- **ripgrep** (`rg`), pour la recherche ;
- **Tesseract**, facultatif mais recommandé : texte des PDF scannés et des images
  (`tesseract-ocr` avec `tesseract-ocr-eng` et `tesseract-ocr-fra`) ;
- **catdoc**, facultatif mais recommandé : texte des anciens fichiers Word et Excel `.doc` et
  `.xls` (paquet `catdoc`, qui fournit aussi `xls2csv` ; les anciens `.ppt` sont lus sans lui) ;
- le client PostgreSQL (`pg_dump`, `pg_restore`) de même version que le serveur, pour les
  sauvegardes ;
- `age` et `rclone`, pour la copie chiffrée hors serveur.

`synaptane doctor` vérifie leur présence.

## Configuration

`init` écrit `synaptane.toml` (droits 600) et génère la **clé maître** `master.key` dans le
dossier de données. Avec Docker, la configuration passe par les variables d'environnement
et la clé est générée au premier démarrage. Cette clé chiffre les secrets stockés en base,
comme les identifiants Git et les clés IA. **Sauvegardez-la hors de la base** : sans elle,
ces secrets sont perdus. Voir [Sauvegarde et restauration](backup.fr.md).

Chaque option de `synaptane.toml` peut être remplacée par une variable d'environnement
`SYNAPTANE_<OPTION>`, par exemple `SYNAPTANE_DATABASE_URL`. Le fichier se choisit avec
`--config` ou `SYNAPTANE_CONFIG`.

| Option | Rôle |
|---|---|
| `public_url` | URL publique. En `https://`, les cookies sont marqués `Secure`. C'est aussi l'émetteur OAuth et l'adresse du MCP (`<public_url>/mcp`) : pour Claude.ai, elle doit être en HTTPS et joignable depuis Internet. |
| `database_url` | Connexion PostgreSQL (`postgresql+psycopg://…`). |
| `data_dir` | Clé maître et copies locales des dépôts. |
| `default_locale` | Langue par défaut de l'interface : `en` (par défaut) ou `fr`. Elle sert quand le navigateur ne demande ni l'une ni l'autre, et pour les comptes créés par un administrateur ou la CLI ; chacun change la sienne dans **Mon compte**. Variable : `SYNAPTANE_DEFAULT_LOCALE`. |
| `session_days` | Durée d'une session web, 30 jours par défaut. |
| `poll_minutes` | Fréquence de la synchronisation de secours, 5 minutes par défaut. |
| `git_timeout` | Délai global d'un échange avec la forge (clone, récupération, envoi), en secondes, 300 par défaut. À augmenter seulement pour le premier clone d'un très gros dépôt. Voir [Git](git.fr.md). |
| `allow_local_repos` | Autorise les dépôts `file://`. **Développement uniquement.** |
| `ocr_languages` | Langues Tesseract, `fra+eng` par défaut. |
| `ocr_images` | Extraire le texte des images (OCR), `true` par défaut. |
| `allow_private_webhooks` | Autorise les webhooks en HTTP et vers le réseau local. **Développement uniquement.** |
| `api_docs` | Publie la documentation interactive `/api/docs` et le schéma `/api/v1/openapi.json`, `false` par défaut. |
| `meaning_min_similarity` | Recherche par le sens : une page trouvée seulement par le sens est proposée dès que la meilleure page atteint cette similarité (entre 0 et 1). Non défini (par défaut) : elle doit nettement se détacher des autres voisines, ce qui arrive rarement dans un espace de pages semblables (transcriptions d'appels, tickets). L'échelle dépend du modèle d'embeddings : la mesurer sur des questions liées à l'espace et sur des questions sans rapport. Variable Compose : `MEANING_MIN_SIMILARITY`. |
| `rerank_model` | Recherche par le sens : un modèle de reclassement Voyage (`rerank-2.5`, `rerank-2.5-lite`) qui renote les 40 meilleurs résultats selon la question, avec la clé Voyage de l'organisation. Non défini (par défaut) : pas de reclassement. Une recherche par mots seuls n'est jamais reclassée, et une panne du fournisseur garde l'ordre habituel. Variable Compose : `RERANK_MODEL`. |
| `embed_front_matter` | Recherche par le sens : chaque morceau d'une page Markdown est vectorisé avec les valeurs courtes de l'en-tête de la page (sans les identifiants ni les valeurs longues), pour qu'un morceau du milieu de la page garde le sujet de la page. Utile pour des pages générées avec des métadonnées (transcriptions d'appels, tickets). `false` par défaut ; lancer `synaptane reindex` après l'avoir changé. Variable Compose : `EMBED_FRONT_MATTER`. |
| `backup_age_recipients` | Clés publiques age (`age1…`) pour lesquelles `backup --offsite` chiffre. En variable d'environnement : séparées par des virgules. Voir [Sauvegarde](backup.fr.md). |
| `backup_age_recipients_file` | Ou un fichier de destinataires age. |
| `backup_remotes` | Destinations rclone de la copie hors serveur (`nom:chemin`, ou un dossier). |
| `backup_retention_days` | Archives distantes plus anciennes effacées, 30 jours par défaut ; `0` : jamais. |
| `backup_rclone_config` | Configuration rclone. Par défaut `$RCLONE_CONFIG` (`/data/rclone/rclone.conf` avec Docker), sinon `<data_dir>/rclone/rclone.conf`. |

Avec Docker Compose, ces variables se règlent dans `.env` (voir les commentaires de
`.env.example`) :

| Variable | Rôle |
|---|---|
| `PUBLIC_URL`, `POSTGRES_PASSWORD` | Adresse publique et mot de passe de la base (obligatoires). |
| `COMPOSE_PROFILES` | Vide (derrière le nginx du serveur) ou `proxy` (tout-en-un). |
| `COMPOSE_FILE` | Fichiers Compose, séparés par `:`. Non défini : `compose.yaml`, plus `compose.override.yaml` s'il existe. |
| `SYNAPTANE_VERSION` | Étiquette des images publiées, la version de `compose.yaml` par défaut. |
| `SYNAPTANE_IMAGE`, `SYNAPTANE_NGINX_IMAGE` | Une autre image pour Synaptane, ou pour le nginx du mode proxy (`synaptane:local`, `synaptane-nginx:local` pour une construction). |
| `GIT_SHA`, `AGENT_CLIS` | Construction seulement (`compose.build.yaml`) : commit inscrit dans l'image, CLI d'abonnement (`true` par défaut). |
| `APP_PORT`, `APP_BIND`, `FORWARDED_ALLOW_IPS` | Mode par défaut : port local (8000), son adresse (`127.0.0.1`), réseaux des proxys de confiance. |
| `DOMAIN`, `LETSENCRYPT_EMAIL`, `LETSENCRYPT_STAGING`, `HTTP_PORT`, `HTTPS_PORT` | Mode proxy : domaine, compte Let's Encrypt et certificats de test, ports publiés (80 et 443). |
| `BACKUP_AGE_RECIPIENTS`, `BACKUP_REMOTES`, `BACKUP_RETENTION_DAYS` | Copie hors serveur (voir [Sauvegarde](backup.fr.md)). |
| `SYNAPTANE_DEFAULT_LOCALE`, `OCR_LANGUAGES`, `TZ` | Langue de l'interface, langues de l'OCR, fuseau horaire. |
| `SYNAPTANE_WORKER_MEMORY`, `SYNAPTANE_LOG_LEVEL` | Mémoire maximale du worker (`2g`), niveau des journaux. |

## Envoi d'e-mails, invitations et mot de passe oublié

**Administration → E-mail** : serveur SMTP, port, sécurité (STARTTLS ou SSL), identifiant,
mot de passe (chiffré, jamais réaffiché) et adresse d'expédition. « Envoyer un e-mail de
test » vérifie le réglage, vers l'adresse de votre compte.

Avec l'envoi d'e-mails :
- un admin d'organisation **invite** depuis l'onglet **Membres**. Il choisit l'adresse, le
  rôle dans l'organisation et, au choix, un rôle par espace. La personne reçoit un lien
  valable 14 jours et crée son compte. Si l'adresse est celle, **vérifiée**, d'un compte
  existant, les accès sont donnés aussitôt ;
- **« Mot de passe oublié ? »** sur la page de connexion envoie un lien valable 30 minutes.

Sans envoi d'e-mails, les invitations marchent aussi : le lien est montré une fois à la
personne qui invite, qui le transmet. Le mot de passe oublié passe alors par un super-admin
(Administration → Comptes).

Un mot de passe choisi par un admin, à la création du compte ou par réinitialisation, doit
être changé à la première connexion. On se connecte avec son nom d'utilisateur ou son
e-mail.

**Adresses vérifiées.** Une adresse saisie dans « Mon compte » ou par un admin reste « à
vérifier » tant qu'elle n'est pas confirmée. Le lien envoyé à cette adresse (valable 3 jours)
doit être ouvert en étant connecté au compte qui l'a demandé, puis confirmé par un bouton : un
lien ouvert par quelqu'un d'autre, ou par l'antivirus d'une messagerie, ne vérifie rien. Seule
une adresse vérifiée sert aux invitations vers un compte existant, à la liaison SSO et au mot
de passe oublié. Changer son e-mail demande le mot de passe actuel, et l'ancienne adresse est
prévenue. Une adresse déclarée mais jamais vérifiée ne bloque pas son vrai titulaire : elle
lui revient quand il l'utilise (invitation, SSO).

**Limites d'envoi.** Chacun de ces e-mails part vers une adresse saisie par quelqu'un qui n'en
est pas forcément le titulaire, d'où un plafond : 3 liens de vérification par compte et par
heure, et 30 invitations par organisation et par heure (comptées que l'e-mail parte ou non). Au-delà, la
demande est refusée (HTTP 429) jusqu'à ce que l'heure soit passée.

**Connexions.** Après 10 échecs en 15 minutes depuis une même adresse IP, ce compte est
bloqué pour cette adresse ; après 50 échecs toutes adresses confondues, pour tous. Les
variantes d'écriture d'un identifiant de connexion (espaces, majuscules, e-mail) comptent pour
le même compte. Le journal désigne le compte par son nom d'utilisateur ; un identifiant qui ne
correspond à aucun compte y figure sous forme d'empreinte courte (`login:` suivi de 8
caractères hexadécimaux), jamais tel que saisi, car ce peut être un mot de passe tapé dans le
mauvais champ.

## Index de recherche

Le texte extrait des PDF, des documents Office et des images est rangé hors des dépôts, dans
`data_dir/extract/`.

Chaque document est lu par un processus à part, borné, pour qu'un fichier piégé ou
démesuré (plan grand format, scan de centaines de pages) ne fasse pas tomber le worker :
- 1 Go de mémoire au plus (Tesseract et catdoc compris) et 10 minutes par document ;
- pages rendues pour l'OCR plafonnées à 40 millions de pixels (300 dpi pour un format
  courant, moins pour un grand format) ;
- 500 pages lues, dont 100 au plus passées à l'OCR, 60 secondes par page ; au-delà, le
  texte natif reste indexé et le journal du worker le signale ;
- images de plus de 250 millions de pixels ignorées.

Un fichier qui échoue (délai, mémoire, fichier invalide) est inscrit comme tel dans
`data_dir/extract/<espace>/manifest.json`, avec la raison, et n'est pas retenté tant qu'il
ne change pas : il est inscrit avant même d'être lu, si bien qu'un arrêt du worker au milieu
ne se répète pas indéfiniment (deux essais au plus). `synaptane reindex` retente tout,
échecs compris.

L'index se reconstruit à tout moment depuis Git :

```sh
uv run synaptane reindex                  # tous les espaces
uv run synaptane reindex --space acme/infra
docker compose exec app synaptane reindex   # avec Docker
```

## Alertes

Les administrateurs d'une organisation sont prévenus, par e-mail si l'envoi est configuré et
par le webhook `space.alert`, de ce qui demande leur attention, par exemple quand :
- le rangement d'un espace échoue durablement (pas une simple coupure réseau), ou s'arrête
  avant la fin ;
- un espace ne se synchronise plus avec son dépôt (identifiants refusés, dépôt introuvable) ;
- une inbox garde des éléments jamais examinés depuis plus de 3 jours ;
- l'agent de rangement atteint le plafond de suppressions, ou fait de gros changements.

La liste complète des types d'alerte est dans [Webhooks](webhooks.fr.md). Une même alerte
(type et espace) ne repart pas avant 20 heures. Le budget d'IA a ses propres alertes (voir
[Intelligence artificielle](ai.fr.md)).

## Rôles

| Rôle | Portée | Peut |
|---|---|---|
| Super-admin | Instance | Tout : comptes, toutes les organisations, et lecture et écriture de tous les contenus dans le web et l'API REST. Par un assistant (MCP), seulement ses appartenances réelles. |
| Admin d'organisation | Organisation | Créer et régler les espaces, gérer les membres. Contributeur sur tous les espaces de l'organisation. |
| Contributeur | Espace | Lire, chercher, déposer dans l'inbox, modifier le contenu. |
| Lecteur | Espace | Lire et chercher. |

Ajouter quelqu'un à un espace en fait automatiquement un membre simple de l'organisation.
Le retirer de l'organisation lui retire aussi ses espaces.

## Premiers pas

Au premier démarrage, la page d'accueil du super-admin rappelle ces étapes.

1. **Administration** : onglet **Organisations** pour les créer (case cochée par défaut :
   vous en devenez administrateur, sinon vos assistants n'y verraient rien), onglet
   **Comptes** pour créer les comptes (chaque compte a sa page : droits, mot de passe).
2. Page de l'organisation, une page par sujet :
   - **Espaces**, puis « Nouvel espace » : nom, identifiant, adresse du dépôt Git ;
   - **Membres** : ajouter, nommer les administrateurs ;
   - **Intelligence artificielle** : le fournisseur du rangement nocturne ;
   - **Recherche sémantique** et **Webhooks**, facultatifs ;
   - **Paramètres** : nom, description, suppression.
3. Espace, onglet **Paramètres** : **Dépôt Git** (adresse, accès, webhook, état et conflits),
   **Membres** (lecteur ou contributeur), **Rangement** (heure, fuseau, modèle),
   **Recherche sémantique** et **Général**.

Une suppression d'espace ou d'organisation demande de retaper son identifiant.

Pour relier un espace à son dépôt, voir [Git](git.fr.md). Pour la connexion par GitLab, GitHub,
Google ou OIDC, voir [SSO](sso.fr.md) ; pour prévenir d'autres outils, voir
[Webhooks](webhooks.fr.md). Tout cela est aussi possible par
l'[API REST](../reference/api.fr.md).

## Données de démonstration

Pour essayer Synaptane avant de relier vos propres dépôts, `synaptane demo` charge le wiki
d'une équipe informatique fictive ([examples/acme-wiki](../../examples/acme-wiki/README.md), en
anglais) : 50 pages, une vocation, six notes d'inbox qui couvrent les cas types du rangement,
des pages vérifiées, une page à revoir et un lien cassé. Huit fichiers autres que Markdown
sont reliés depuis les pages, pour voir ce que Synaptane en fait : un contrat en PDF, un bon
de livraison scanné (OCR), une checklist Word, une présentation, deux tableurs (dont un à
l'ancien format `.xls`), un schéma de baie (OCR) et un rapport de statut en HTML. Cherchez « Kestrel », un mot qui n'apparaît que dans le
scan (Tesseract requis, présent dans l'image Docker). Sur une instance initialisée et
migrée, elle crée ce qui manque, puis synchronise et indexe l'espace :
- un super-admin `demo`, dont le mot de passe est affiché une fois (ou donné par
  `SYNAPTANE_DEMO_PASSWORD`, ou `--password`) ;
- l'organisation `acme`, dont `demo` est administrateur ;
- l'espace `acme/it`, adossé à un dépôt Git local, `<data_dir>/demo/acme-it.git`.

Relancée, elle ne change rien : un compte existant garde son mot de passe, et ce que vous avez
modifié dans l'espace reste.

Le dépôt est local (`file://`) : Synaptane le refuse sans `SYNAPTANE_ALLOW_LOCAL_REPOS=true`
pour la commande, le serveur et le worker. **Démonstration et développement seulement** : ce
réglage permet à un administrateur d'organisation de relier un espace à n'importe quel dossier
Git du serveur. Faites la démonstration sur une instance jetable, pas sur celle de production.

Avec Docker Compose, `examples/compose.demo.yaml` le règle pour `app` et `worker` :

```sh
echo 'COMPOSE_FILE=compose.yaml:examples/compose.demo.yaml' >> .env
docker compose up -d
docker compose exec app synaptane demo
```

Avec une image construite par vos soins :
`COMPOSE_FILE=compose.yaml:compose.build.yaml:examples/compose.demo.yaml`.

Depuis les sources :

```sh
export SYNAPTANE_ALLOW_LOCAL_REPOS=true   # aussi dans les terminaux de serve et du worker
uv run synaptane demo
```

La commande affiche ce qu'elle a créé (`super admin demo`, `organization acme`…), l'adresse
à ouvrir, le mot de passe de `demo` et comment brancher un assistant IA. Elle rappelle aussi ce
qui garde le dépôt de démonstration synchronisé : dans un conteneur, le service Compose
`worker`, à laisser tourner ; depuis les sources, un `synaptane worker` à garder lancé, avec
`SYNAPTANE_ALLOW_LOCAL_REPOS=true`. Le rangement de l'inbox demande un fournisseur d'IA (page
de l'organisation, **Intelligence artificielle**). `docker compose down -v` efface toute
l'instance de démonstration.

## Annexe : fork privé

Si vous déployez depuis votre propre fork **privé**, un `git clone https://…` demande un
identifiant et échoue sur un serveur. Donnez plutôt au serveur une **clé de déploiement** :
une clé SSH à lui, en lecture seule, déclarée sur ce seul dépôt (GitHub → Settings → Deploy
keys, sans droit d'écriture). Clonez ensuite par un alias d'hôte SSH qui n'utilise que cette
clé :

```sh
git clone git@<alias-ssh>:<compte>/synaptane.git ~/synaptane
```

Création de la clé et de l'alias SSH dans `~/.ssh/config` : voir
[`deploy/host/README.fr.md`](../../deploy/host/README.fr.md). La suite de l'installation est
celle d'une [image construite par vos soins](#construire-limage-vous-même).
