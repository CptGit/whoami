# Repository Guidelines

## Project Structure & Architecture

- `main.py`: FastAPI routes, WebSocket rooms, reconnect handling, and authoritative timers. Rooms live in memory; production uses one worker.
- `static/index.html`: browser UI with inline CSS/JavaScript; no frontend build pipeline.
- `data/presets/`: tracked JSON character libraries. `data/custom/`: ignored writable libraries.
- `scripts/`: setup and deployment. `deploy/`: systemd/sudoers templates and deployment documentation.
- `tests/`: Python/browser regression checks. `.github/workflows/ci-deploy.yml`: CI and deployment.

## Development Commands

Run from the repository root:

- `bash scripts/setup.sh`: install pinned uv, Python, and Node; synchronize `.venv` using `uv.lock`.
- `.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --reload`: start the development server.
- `.venv/bin/python3 -m unittest discover -s tests -v`: run Python checks.
- `.tools/node/bin/node tests/client.test.js`: run browser checks.
- `bash -n scripts/setup.sh scripts/deploy.sh scripts/release.sh`: check shell syntax.

Production setup uses `bash scripts/setup.sh --production` and skips Node. Use project executables, not system Python or Conda.

## Coding Style & Naming

Use four spaces in Python/Bash and preserve surrounding HTML/JavaScript indentation. Use `snake_case` for Python functions, `PascalCase` for classes, `camelCase` for JavaScript, and uppercase environment variables. Preserve UTF-8 Chinese content. No formatter is configured; avoid unrelated reformatting. Update `pyproject.toml` and `uv.lock` together.

## Testing Guidelines

Python uses `unittest` and `IsolatedAsyncioTestCase`; browser checks use `node:test`. Name Python files `test_*.py` and methods `test_*`. No numeric coverage target is enforced. Prioritize reconnect ordering, stale messages, server skip deadlines, rollback, and shared-data preservation. Add meaningful regression cases without duplicating implementation. Documentation-only edits do not require test suites.

## Commits & Pull Requests

History uses short descriptive English subjects; Conventional Commits are not enforced. Keep commits focused. PRs should explain behavior changes, link relevant issues, summarize validation, and include screenshots for UI changes. Mention restart or data implications.

## Deployment & Private Configuration

`scripts/deploy.sh` runs locally and streams `scripts/release.sh` over SSH. `DEPLOY_AS_USER` is mandatory; SSH and publishing accounts may differ. The service runs as `APP_USER`.

Keep deployment-specific hosts, usernames, keys, known-host records, `.deploy.env`, and migration notes out of Git. Use placeholder examples and ignored `.deploy/` for local artifacts. Administrator setup is manual; never collect or transmit sudo passwords.
