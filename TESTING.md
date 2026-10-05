# Testing nodemeet

There are three layers. Run them all with one script, or one at a time.

| Layer | What it proves | Needs |
|---|---|---|
| **Unit** (`tests/`) | every feature, integration and webhook, against fake servers | Python only |
| **Databases** (`tests/integration/`) | the storage contract, double-booking races across 4 connections, and a full app restart on **real** database servers | Docker Desktop |
| **Browsers** (`tests/e2e/`) | real Chromium, Firefox and WebKit: waiting room, admit, live WebRTC video, chat, the booking widget, phone layout | Playwright |

## Windows (PowerShell)

```powershell
cd nodemeet
powershell -ExecutionPolicy Bypass -File scripts\test-all.ps1          # unit + 7 core databases + 3 browsers
powershell -ExecutionPolicy Bypass -File scripts\test-all.ps1 -Full    # all 19 databases (give Docker ~7 GB RAM)
```

Everything is written to `test-report.txt`.

## One layer at a time

```powershell
python -m pip install -e . -r tests\requirements-test.txt
python -m pytest tests -q --ignore=tests\integration --ignore=tests\e2e      # unit

python -m pip install -r tests\integration\requirements-db.txt
docker compose -f tests\integration\docker-compose.yml up -d --wait          # add --profile full for all 19
python -m pytest tests\integration -v -rs                                    # databases
docker compose -f tests\integration\docker-compose.yml --profile full down -v

python -m pip install -r tests\e2e\requirements-e2e.txt
python -m playwright install chromium firefox webkit
python -m pytest tests\e2e -v -rs                                            # browsers
```

**Useful switches:**
- `$env:NM_DBS="postgres,redis"` runs only some databases.
- `$env:NM_DB_POSTGRES="postgres://me:pw@myserver/app"` tests against **your own** server instead of Docker.
- `$env:NM_BROWSERS="chromium"` runs only one browser.

## Databases covered

- **Core** (`docker compose up`): Postgres, MySQL, MariaDB, MongoDB, Redis, CouchDB, Elasticsearch.
- **Full** (`--profile full`): CockroachDB, SQL Server, Oracle Free, ClickHouse, Neo4j, ArangoDB, etcd, Consul, MinIO (S3), DynamoDB Local, Cassandra.
- **No server needed:** SQLite, DuckDB, LMDB, dbm, JSON files.
- **Not covered:** Snowflake, Db2, SAP HANA, Firebird and Firestore have no free local Docker image; point `NM_DB_<NAME>` at a real account to test them.

Every run uses a fresh `?namespace=` (a table, key or collection prefix), so reruns never clash and the tests never touch other data.

## CI

`.github/workflows/tests.yml` runs:
- the unit tests on Windows, macOS and Linux with Python 3.10, 3.12 and 3.13;
- all 19 databases;
- all 3 browsers.
