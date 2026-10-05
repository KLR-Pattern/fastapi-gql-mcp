# Agent.md — release process and attention points

Operating instructions for agents (and humans) releasing this repo.

## Release flow

Prerequisites: on `master`, in sync with origin, working tree clean, CI green
(`gh run list`). The Unreleased section of `CHANGELOG.md` should carry the
changes you are shipping.

1. **Decide the version.** This repo is 0.x: breaking changes bump the minor
   (0.1 → 0.2 → …), additive changes may too, fixes alone patch. Look at the
   Unreleased entries: anything marked *(breaking)* forces a minor bump.
2. **Bump the version in THREE places** — this is the classic miss:

   | File | What to change |
   |---|---|
   | `pyproject.toml` | `version = "X.Y.Z"` |
   | `src/fastapi_gql_mcp/__init__.py` | `__version__ = "X.Y.Z"` |
   | `CHANGELOG.md` | `## Unreleased` → `## X.Y.Z (YYYY-MM-DD)` |

   Verify consistency before committing:

   ```bash
   grep '^version' pyproject.toml && grep __version__ src/fastapi_gql_mcp/__init__.py
   # both must print the same X.Y.Z
   ```

   (0.5.0 shipped with `__version__` still at 0.4.0 — the published package
   reports the wrong runtime version. Don't repeat it.)

3. **Commit**: `chore(release): X.Y.Z`, one-line summary of the wave.
4. **Tag and push**:

   ```bash
   git tag vX.Y.Z
   git push origin master vX.Y.Z
   ```

5. **Publish is automatic.** The `v*` tag triggers `.github/workflows/publish.yml`:
   `uv build` → `uv publish` (token from the `PYPI_PUBLISHER` secret) → a
   GitHub Release with generated notes. Watch it:

   ```bash
   gh run watch $(gh run list --workflow publish.yml --limit 1 --json databaseId -q '.[0].databaseId')
   ```

6. **Verify before announcing**:

   ```bash
   curl -s https://pypi.org/pypi/fastapi-gql-mcp/json | python3 -c \
     "import sys,json; print(json.load(sys.stdin)['info']['version'])"
   pip download fastapi-gql-mcp==X.Y.Z --no-deps -d /tmp/check && rm -rf /tmp/check
   ```

   Stronger check (used for 0.4.0): run the example off the published wheel —
   from `examples/notes_oauth`, bypassing the editable source:

   ```bash
   OTEL_OTLP_ENDPOINT=http://localhost:4317 uv run --no-project \
     --with 'fastapi-gql-mcp[mcp]==X.Y.Z' \
     --with fastapi --with uvicorn --with python-dotenv --with asgi-lifespan \
     --with opentelemetry-sdk --with opentelemetry-exporter-otlp \
     python -c "import fastapi_gql_mcp; assert 'site-packages' in fastapi_gql_mcp.__file__; from app.__main__ import main; main()"
   ```

7. **After release**: new work goes under a fresh `## Unreleased` heading in
   the CHANGELOG (Added / Fixed / Changed subsections).

## Attention points

- **Release only from `master`.** Feature branches merge (fast-forward when
  linear) before any version bump.
- **The tag is the trigger.** Pushing the tag publishes to PyPI — a broken
  release cannot be unpublished, only superseded by the next version. Never
  move or delete a published tag.
- **PyPI token**: `PYPI_PUBLISHER` in repo secrets. First release of a new
  project name needs an account-wide token; afterwards rotate to a
  project-scoped one. Also claim the project on PyPI after first publish.
- **`__version__` consistency** is part of the release definition of done
  (step 2) — the wheel's runtime string must match the tag.
- **CHANGELOG discipline**: entries land in Unreleased as they ship, not in
  a batch at release time; each entry says *what changed and why*, breaking
  changes are marked *(breaking)*.
- **Commit hygiene**: conventional commits (`feat:`, `fix:`, `docs:`,
  `chore(release):`); CHANGELOG hunks split into the commit they describe
  when a release groups several commits.
- **Locks**: the repo-root `uv.lock` is committed; `examples/*` and
  `comparison/bench/*` locks are ignored (they carry local path sources).
- **Known trap**: port 8020 — check `lsof -nP -iTCP:8020 -sTCP:LISTEN`
  before "the server won't start" debugging; yesterday's instance may still
  be holding it (`python -c`-launched processes dodge `pkill -f "python -m app"`).
