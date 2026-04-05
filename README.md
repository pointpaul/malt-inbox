# Malt Inbox

[![CI](https://github.com/pointpaul/malt-inbox/actions/workflows/ci.yml/badge.svg)](https://github.com/pointpaul/malt-inbox/actions/workflows/ci.yml)

Petit CRM **local** pour tes conversations et opportunités Malt : sync dans SQLite sur ta machine, IA optionnelle. **FastAPI** + **Uvicorn** servent l’API et les pages ; rien à « déployer » : tu installes, tu ouvres le navigateur sur **localhost**.

![Aperçu du dashboard](docs/malt-inbox.png)

## Lancer sur ta machine

Prérequis : **Python 3.10+** et [**uv**](https://docs.astral.sh/uv/).

```bash
cp .env.example .env
uv sync --frozen --group dev
uv run python main.py
```

Au démarrage, le **navigateur s’ouvre** sur **http://127.0.0.1:8765** ; colle ton `remember-me` (voir plus bas) si besoin, laisse tourner la première sync, puis utilise le dashboard.

Si tu changes les dépendances dans `pyproject.toml` : `uv lock` puis commit du `uv.lock`.

## Première fois

1. Cookie **remember-me** (obligatoire) et éventuellement **OPENAI_API_KEY** dans `.env` ou via l’écran Settings au premier lancement.
2. Page de progression pendant la sync initiale, puis le CRM.
3. Si Malt renvoie **403** (session expirée), l’app te renvoie vers les réglages pour mettre à jour le cookie.

## Cookie `remember-me`

Navigateur connecté à Malt → DevTools → Application / Cookies → `https://www.malt.fr` → copier la valeur **remember-me**.

## IA (optionnel)

Sans `OPENAI_API_KEY`, tout fonctionne sans enrichissement IA. Avec une clé : résumés, prochaines actions, brouillons de réponses (voir `MALT_CRM_OPENAI_MODEL` dans [`.env.example`](.env.example)).

## Fichiers utiles

- **`.env`** — secrets et options (ne pas committer).
- **`.local/malt_crm.sqlite3`** — base SQLite (créée au premier run).
- Code applicatif : package **`malt_crm/`**, point d’entrée **`main.py`**.

Détail des variables : [`.env.example`](.env.example) (la plupart du temps seuls `MALT_REMEMBER_ME` et éventuellement `OPENAI_API_KEY` comptent en usage perso).

## Option : Docker

Si tu préfères un conteneur au lieu d’uv sur l’hôte :

```bash
cp .env.example .env
docker compose up --build
```

Même URL et mêmes volumes (`.env`, `.local`). Pas nécessaire pour l’usage courant.

## Développement

```bash
make sync          # ou : make install
make test
make lint
make check         # lint + tests + vulture + compileall
make hooks         # une fois : pre-commit (Ruff avant commit)
```

Équivalent sans Make : `uv sync --frozen --group dev`, puis `uv run pytest`, `uv run ruff check .`, etc.

### Qualité / code mort

- **`make deadcode`** ou `uv run vulture` : détecte le code vraiment inutilisé (les routes FastAPI sont ignorées via la config dans `pyproject.toml`).
- **`make cov`** : rapport de couverture ; le seuil minimal est défini dans `pyproject.toml` (`pytest` le vérifie tout seul).

## Sécurité

Pas de mot de passe Malt dans l’app — uniquement le cookie que tu fournis. Données locales (`.env` + SQLite). Ne committe pas `.env`, `.local/`, `.venv/`.

## Limites

Projet non officiel Malt ; l’API Malt peut évoluer. Les réponses s’envoient toujours depuis Malt.

## Licence

MIT
