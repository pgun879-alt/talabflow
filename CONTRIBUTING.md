# Working on talabflow

This is a portfolio project, so it is not looking for feature contributions. This file exists for
a different reason: the README claims another developer could pick this code up, and that claim
should be checkable. If you are evaluating the project, reviewing it, or forking it for your own
use, everything you need is below.

## Setting up

Python 3.11 or newer. `.python-version` pins 3.13, which is what the tests were measured on.

```bash
make setup      # create .venv and install the package with dev extras
make migrate    # apply Alembic migrations to the SQLite database
make check      # run every gate the CI runs
make demo       # full offline demo: conversations, pipeline, notifications, export
```

`make help` lists every target. **Nothing in `make setup`, `make check` or `make demo` touches the
network or an external service.** No Telegram token, no API key and no paid service is needed to
install, test or demo this project, and the demo scripts talk only to in-process fakes.

## The gates

`make check` runs the same four things CI runs, and all four must be clean:

| Gate | Command | Standard |
|---|---|---|
| Format | `ruff format --check .` | 100-column lines, no exceptions |
| Lint | `ruff check .` | the rule set in `pyproject.toml`, zero findings |
| Types | `mypy` | `disallow_untyped_defs`, so every function is annotated |
| Tests | `pytest` | 324 passing, zero skipped |

CI additionally applies the migrations, runs `alembic check`, and runs
`scripts/check_repo_hygiene.py`, which fails the build if a database, a virtual environment or a
credential-shaped literal has been committed. Run it locally with
`python scripts/check_repo_hygiene.py` before any commit.

Ruff's rule selection and the few deliberate `ignore` entries are documented inline in
`pyproject.toml`, each with the reason it is there. Please do not add a bare `# noqa` — if a rule
genuinely does not apply, say why in a comment next to it, as the existing suppressions do.

## Changing the schema

Never hand-edit a migration that already exists. Generate one:

```bash
.venv/bin/alembic revision --autogenerate -m "describe the change"
```

Then read it. Autogenerate gets `server_default`, index names and type changes wrong often enough
that review is not optional, and `migrations/env.py` carries a `render_item` hook so a migration
never imports application code. Verify `upgrade`, `downgrade` and a second `upgrade` all work, and
that `alembic check` is clean afterwards.

## Tests

A behaviour change needs a test that fails before it and passes after. The suite is offline and
deterministic; if you need a clock or an external call, inject it rather than patching globals.

The most delicate areas are the outbox claim/lease logic and the authentication layer. Tests there
are written to fail if the fix they cover is reverted — if you touch either, confirm that property
still holds, because a test that passes with the bug reintroduced is worse than no test.

## Commits

[Conventional Commits](https://www.conventionalcommits.org/), as in the existing history:
`fix(outbox): …`, `feat(api): …`, `docs: …`. The body should explain *why*, since the diff already
shows *what*.

Do not commit a `.env`, a database, or anything under `data/` or `reports/`. See
[SECURITY.md](SECURITY.md) for how to report a vulnerability.
