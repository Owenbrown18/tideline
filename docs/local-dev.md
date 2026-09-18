# Running Tideline locally

## One-time setup
Needs Docker (OrbStack or Docker Desktop) and `uv`. Python 3.12 is fetched by uv.

```bash
uv sync                               # creates .venv with app + dev tools
cp sites.example.yaml sites.yaml      # then list the real sites (git-ignored)
```

## Run the whole thing
```bash
docker compose up --build
```
Starts Postgres 16, runs `tideline migrate` and `tideline seed` once, then the
worker. Every check result is one JSON log line (`"event": "check_result"`).

## The incident demo (the M1 "Done when" check)
```bash
docker compose -f compose.yaml -f compose.demo.yaml up --build -d
docker compose logs -f worker | grep -E 'incident_|"alert"'
```
In a second terminal:
```bash
docker compose stop fakesite     # within ~30 s: incident_open + alert (open)
docker compose start fakesite    # within ~10 s: incident_resolve + alert (resolved)
```
Confirm it landed in Postgres:
```bash
docker compose exec postgres psql -U sitewatch -c "select id, opened_at, resolved_at, resolved_at - opened_at as duration, severity from incidents;"
```
Reset everything, including the database volume: `docker compose down -v`.

## Tests, lint, types
```bash
docker compose up -d postgres
TIDELINE_TEST_DATABASE_URL=postgresql+psycopg://sitewatch:sitewatch@localhost:5432/sitewatch_test uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
Without `TIDELINE_TEST_DATABASE_URL` the integration tests are skipped and
only the unit tests run. The test database is emptied by the tests: never
point it at real data.

## Useful commands
```bash
uv run tideline check davesbakery.ca --expected "Daves' Bakery"   # run checks 1-4 once, no DB
uv run tideline seed sites.yaml                                   # re-load the site list
uv run alembic revision --autogenerate -m "describe the change"    # after editing db/models.py
```
