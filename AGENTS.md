# QuantDinger project instructions

## Project map

- This repository contains the Python 3.12 Flask backend, Docker Compose deployment files, database migrations, documentation, and a separate Python MCP package.
- Backend source: `backend_api_python/`
- Backend tests: `backend_api_python/tests/`
- MCP package: `mcp_server/` (`setuptools`, Python >=3.10)
- Default stack: PostgreSQL 18, Redis 8, backend, prebuilt web frontend, and prebuilt mobile H5 frontend.
- Web and mobile source are not part of this checkout. Their normal images come from GHCR. `docker-compose.build.yml` is only for separately cloned sibling UI repositories.
- Follow `.cursor/skills/quantdinger-agent-workflow/SKILL.md` for repository safety boundaries. For larger backend work, also read `docs/ARCHITECTURE.md`, `docs/MODULE_BOUNDARIES.md`, `docs/CONCURRENCY_MODEL.md`, and `docs/API_CONVENTIONS.md`.

## Local multi-repository workspace

This server uses three peer checkouts owned through the user's GitHub forks:

| Component | Local path | Working remote (`origin`) | Default branch |
| --- | --- | --- | --- |
| Backend/orchestration | `/home/quantadinger/QuantDinger` | `git@github.com:luhuiyuan/QuantDinger.git` | `main` |
| Web frontend | `/home/quantadinger/QuantDinger-Vue` | `git@github.com:luhuiyuan/QuantDinger-Vue.git` | `main` |
| Mobile + H5 | `/home/quantadinger/QuantDinger-Mobile` | `git@github.com:luhuiyuan/QuantDinger-Mobile.git` | `master` |

- Treat the `luhuiyuan` forks as the writable working repositories. Push feature branches and release tags to these `origin` remotes, not directly to the canonical repositories.
- Canonical upstream repositories are `OpenByteInc/QuantDinger`, `OpenByteInc/QuantDinger-Vue`, and `OpenByteInc/QuantDinger-Mobile`. As observed on 2026-07-20, no `upstream` remotes are configured locally. Do not silently add, fetch, merge, rebase, or push an upstream remote; ask before changing repository topology or synchronizing upstream changes.
- Keep the three repositories independent. Do not copy frontend/mobile source into the backend repository and do not convert them to Git submodules unless explicitly requested.
- For local Compose UI builds, provide the peer paths through root-level Compose variables:

  ```dotenv
  FRONTEND_SRC_PATH=/home/quantadinger/QuantDinger-Vue
  ```

- The project-root `.env` is intentionally present and gitignored for local Compose orchestration. It enables `docker-compose.build.yml`, points to the two peer UI checkouts, and sets low-memory build defaults. Do not put backend secrets in this file.
- Fork GitHub Actions derive the image namespace from the repository owner. If release workflows run in these forks, expected image names are `ghcr.io/luhuiyuan/quantdinger-backend`, `ghcr.io/luhuiyuan/quantdinger-frontend`, and `ghcr.io/luhuiyuan/quantdinger-mobile`.
- As observed on 2026-07-20, those fork image names/tags were not anonymously reachable. Do not point deployment at them until the workflow has published the desired tag and package visibility/authentication has been verified. Until then, retain the currently working image source or use explicitly built local images.

## Low-memory packaging and build policy

The global low-memory constraints apply. During the current development phase, this host uses local source builds for the backend and web frontend only. Mobile/H5 source builds are currently disabled. Build only the changed service, keep builds sequential, and use targeted verification. GHCR remains the future production/release path, not the current development deployment path.

### Current development deployment: local source builds

The root `.env` automatically merges `docker-compose.yml` with `docker-compose.build.yml`, so normal Compose commands use local backend and frontend sources. Build and start one changed service at a time:

```bash
free -h && swapon --show && df -h /
docker compose build backend
docker compose up -d --no-build --no-deps backend

docker compose build frontend
docker compose up -d --no-build --no-deps frontend
```

- Run only the pair for the service that changed.
- `--no-deps` is for updating a service in an already-running stack. For the first installation, configure `backend_api_python/.env`, build backend and frontend sequentially, then run `docker compose up -d --no-build postgres redis backend frontend` so Mobile/H5 is not started.
- Never run backend and frontend builds concurrently.
- `COMPOSE_PARALLEL_LIMIT=1`, peer source paths, a 1536 MiB Node heap cap, and native dependency job count 1 are persisted in the gitignored root `.env`/build args.
- The development frontend image uses the local name `quantdinger-frontend`, not an upstream or fork GHCR namespace. Do not push this local development tag as a release.
- `backend_api_python/.env` is intentionally present on this host and gitignored. Keep it out of commits and images; review required secrets/admin settings against `backend_api_python/env.example` before copying this deployment pattern to another host.
- Do not use `docker-compose.ghcr.yml` for ordinary development while this source-build mode is active.

### Future production deployment: GHCR images

For a formal release, return to CI-built GHCR images so this low-memory server only pulls and starts versioned artifacts. Do not switch deployment to the `luhuiyuan` image namespace until workflows have successfully published the desired tags and image access has been verified.

### Local backend source build

The main `docker-compose.yml` builds only the backend from this checkout and pulls frontend/mobile images. After backend changes, build and start only that service, sequentially:

```bash
free -h && swapon --show && df -h /
APP_VERSION="$(cat VERSION)" COMPOSE_PARALLEL_LIMIT=1 docker compose build backend
COMPOSE_PARALLEL_LIMIT=1 docker compose up -d --no-build backend
```

- Build from the repository root.
- Keep Docker BuildKit cache enabled; do not add `--no-cache` unless diagnosing a cache-specific problem.
- Do not use `docker compose up --build` for the whole stack on this host.
- Do not use `--with-dependencies` for builds; PostgreSQL and Redis are images, not build targets.
- Do not run local multi-platform/QEMU builds. Release multi-arch images belong in `.github/workflows/docker-publish.yml` on GitHub-hosted runners. Local builds should target the native `linux/amd64` platform only.
- The backend Dockerfile already uses `pip --prefer-binary --no-cache-dir`; preserve this because wheels avoid expensive native compilation and no pip cache is needed inside the image layer.
- `BUILD_REGION=cn` may be used from mainland China for mirror fallback. It changes package sources, not build concurrency.
- Avoid building while the full application stack or other memory-heavy jobs are active. Do not stop user services without permission.

### Frontend build and Mobile/H5 status

- The frontend source repository is connected through `FRONTEND_SRC_PATH` in the root `.env`.
- Build the frontend with:

```bash
COMPOSE_PARALLEL_LIMIT=1 docker compose -f docker-compose.yml -f docker-compose.build.yml build frontend
```

- Mobile/H5 source remains in `/home/quantadinger/QuantDinger-Mobile`, but it has no active build override and must not be built on this server unless the user re-enables it.
- The base `docker-compose.yml` still defines a prebuilt Mobile/H5 service for future use. Exclude `mobile` from explicit `docker compose up` service lists while it is not needed.
- The frontend Dockerfile accepts `NODE_OPTIONS` and `NPM_CONFIG_JOBS` build arguments. The Compose override passes the low-memory defaults from `BUILD_NODE_OPTIONS` and `BUILD_NPM_JOBS`; preserve this path when editing its Dockerfile.

### MCP package/image

The MCP server is independent of the main Compose stack. Validate it separately and sequentially:

```bash
python -m pytest mcp_server/tests -q
docker build -t quantdinger-mcp:local mcp_server
```

For a Python distribution artifact, use a virtual environment with the `dev` extra/build tooling and create only the wheel unless a source distribution is specifically needed:

```bash
python -m build --wheel mcp_server
```

Do not build the MCP image concurrently with the backend image.

## Verification ladder

Use the smallest applicable level first. Do not install dependencies or rebuild images merely to run a check if the existing environment can run it.

1. Static/config checks from the repository root:

   ```bash
   python scripts/check_version.py
   python scripts/check_mojibake.py
   docker compose -f docker-compose.yml config -q
   docker compose -f docker-compose.ghcr.yml config -q
   ```

2. Backend syntax checks from `backend_api_python/`:

   ```bash
   python -m py_compile run.py
   python -m compileall -q app scripts
   ```

3. Focused backend tests from `backend_api_python/`:

   ```bash
   python -m pytest tests/test_relevant_area.py -q
   # Prefer a single test node for a narrow change:
   python -m pytest tests/test_relevant_area.py::test_name -q
   ```

4. Full backend suite only when justified:

   ```bash
   python -m pytest tests -q
   ```

- Do not enable pytest-xdist or other parallel test execution on this host.
- Tests marked `integration` may call live exchange APIs and require explicit credentials; do not run them implicitly.
- When Agent Gateway routes change, update/export the agent OpenAPI contract and run the relevant `test_agent_*` tests.
- When Compose files change, validate both the normal source-build file and GHCR deployment file. Validate the merged UI override only when the sibling source contexts exist or when doing configuration-only validation.

## Packaging and release boundaries

- `VERSION` and `backend_api_python/VERSION` must remain aligned; use `scripts/check_version.py` to verify and `scripts/bump_version.py` for intentional version changes.
- Production backend image publishing and multi-arch packaging are CI responsibilities, not local-server responsibilities.
- Never put secrets, real `.env` files, API keys, exchange credentials, or database passwords into images or commits.
- Preserve Docker layer order: dependencies before application source so source-only changes reuse the expensive dependency layer.
- Avoid destructive cleanup such as `docker system prune`, broad cache deletion, or removing volumes without explicit user approval.
