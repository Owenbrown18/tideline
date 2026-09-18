# Running Tideline locally

No Docker and no database server needed: the database is one SQLite file.

## One-time setup
Needs `uv`. Python 3.12 is fetched by uv.

```bash
uv sync                               # creates .venv with app + dev tools
cp sites.example.yaml sites.yaml      # then list the real sites (git-ignored)
uv run tideline migrate               # creates tideline.db in this folder
```

## Run the checks, like production does on the 1st and 15th
```bash
uv run tideline run --sites sites.yaml
```
Every check result is one JSON log line (`"event": "check_result"`). Locally,
emails are not sent: the run summary and reports are written to the log
instead (`TIDELINE_NOTIFY_CHANNEL=log`, the default). Narrow a run with
`--kind dns` or `--site davesbakery.ca`; add `--reports` to also build last
month's reports.

## The dashboard
```bash
TIDELINE_API_TOKEN=dev TIDELINE_DASHBOARD_PASSWORD=dev uv run tideline api
```
Then http://127.0.0.1:8000/ and sign in as `owen` / `dev`.

## The incident demo (about a minute)
```bash
scripts/demo.sh
```
Serves a fake client site on the laptop, runs Tideline against it, stops the
site (an incident opens and a "1 new problem" summary is logged), starts it
again (it resolves, "1 fixed"), and prints the incident row. This is what the
CI workflow runs too.

## The showcase and photos
```bash
uv run tideline showcase --out showcase.db     # six months of invented businesses (a minute)
TIDELINE_DEMO_MODE=true TIDELINE_DATABASE_URL=sqlite+aiosqlite:///showcase.db uv run tideline api
scripts/photos.sh                              # every product photo, into the content folder
```

## Tests, lint, types
```bash
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
The integration tests build a fresh SQLite file through the real migrations in
a temporary folder, and use moto for fake S3, SSM and SES. Nothing touches AWS
or the network beyond localhost.

## The Lambda image
```bash
docker build --platform linux/arm64 -t tideline:lambda .
docker run --rm --entrypoint python tideline:lambda -c "import tideline.aws_lambda; print('ok')"
```

## Useful commands
```bash
uv run tideline check davesbakery.ca --expected "Daves' Bakery"   # run checks 1-4 once, no database
uv run tideline seed sites.yaml                                   # re-load the site list
uv run tideline report --month 2026-09 --site davesbakery.ca      # print a report
uv run alembic revision --autogenerate -m "describe the change"    # after editing db/models.py
sqlite3 tideline.db "select * from incidents where resolved_at is null;"
```
