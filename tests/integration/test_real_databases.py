"""The storage contract against REAL database servers (start them with docker compose).

    docker compose -f tests/integration/docker-compose.yml up -d --wait
    python -m pytest tests/integration -v -rs

Each database gets a fresh ``?namespace=`` per run, so reruns never clash. Point a test at
your own server with NM_DB_<NAME>=url (e.g. NM_DB_POSTGRES=postgres://me:pw@db.internal/app).
Only run some: NM_DBS=postgres,redis
"""
import asyncio
import os
import socket
import tempfile
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from helpers import run
from test_storage_contract import T0, contract

from nodemeet.easy import storage_from_url
from nodemeet.models import Booking

TMP = tempfile.mkdtemp()
URLS = {
    # core (docker compose up)
    "postgres": "postgres://postgres:nm@localhost:5432/nodemeet",
    "mysql": "mysql://root:nm@localhost:3306/nodemeet",
    "mariadb": "mariadb://root:nm@localhost:3307/nodemeet",
    "mongodb": "mongodb://localhost:27017/nodemeet",
    "redis": "redis://localhost:6379/0",
    "couchdb": "couchdb://admin:nm@localhost:5984",
    "elasticsearch": "elasticsearch://localhost:9200",
    # full (docker compose --profile full up)
    "cockroachdb": "cockroachdb://root@localhost:26257/defaultdb",
    "mssql": "mssql://sa:Nodemeet_Passw0rd@localhost:1433/master",
    "oracle": "oracle://nm:nm@localhost:1521/FREEPDB1",
    "clickhouse": "clickhouse://default:nm@localhost:9009/default",
    "neo4j": "neo4j://neo4j:nodemeet-pass@localhost:7687",
    "arangodb": "arangodb://root:nm@localhost:8529/_system",
    "etcd": "etcd://localhost:2379",
    "consul": "consul://localhost:8500",
    "s3-minio": "s3://nodemeet/tests?endpoint=http://localhost:9000&region=us-east-1",
    "dynamodb-local": "dynamodb://nodemeet?endpoint=http://localhost:8000&region=us-east-1",
    "cassandra": "cassandra://localhost:9042/nodemeet",
    # no server needed
    "sqlite": f"sqlite:///{TMP}/nm.db",
    "duckdb": f"duckdb:///{TMP}/nm.duckdb",
    "lmdb": f"lmdb:///{TMP}/nm.lmdb",
    "dbm": f"dbm:///{TMP}/nm.dbm",
    "json-files": f"dir:///{TMP}/files",
}
CREDS = {  # S3 / DynamoDB local need *some* AWS credentials
    "s3-minio": {"AWS_ACCESS_KEY_ID": "nodemeet", "AWS_SECRET_ACCESS_KEY": "nodemeet-secret"},
    "dynamodb-local": {"AWS_ACCESS_KEY_ID": "local", "AWS_SECRET_ACCESS_KEY": "local"},
}
ONLY = {x.strip() for x in os.environ.get("NM_DBS", "").split(",") if x.strip()}
NAMES = [n for n in URLS if not ONLY or n in ONLY]


def url_of(name):
    return os.environ.get("NM_DB_" + name.upper().replace("-", "_"), URLS[name])


def reachable(url):
    u = urlsplit(url)
    if u.scheme in ("sqlite", "duckdb", "lmdb", "dbm", "dir"):
        return True
    q = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
    target = urlsplit(q["endpoint"]) if "endpoint" in q else u
    host = (target.hostname or "localhost").split(",")[0]
    port = target.port or {"postgres": 5432, "mysql": 3306, "mongodb": 27017, "redis": 6379}.get(u.scheme, 80)
    try:
        socket.create_connection((host, port), timeout=1.5).close()
        return True
    except OSError:
        return False


def storage(name, ns=None):
    for k, v in CREDS.get(name, {}).items():
        os.environ.setdefault(k, v)
    url = url_of(name)
    if not reachable(url):
        pytest.skip(f"{name}: no server at {url} (start it with docker compose)")
    try:
        return storage_from_url(url, namespace=ns or "t" + uuid.uuid4().hex[:10])
    except ImportError as exc:
        pytest.skip(f"{name}: driver missing - {exc}")


@pytest.mark.parametrize("name", NAMES)
def test_contract(name):
    """Rooms, availability, bookings (atomic + buffers), reminders, chat, keys, webhooks,
    roles, branding, generic records - the same checks the built-in backends pass."""
    run(contract(storage(name)))


@pytest.mark.parametrize("name", NAMES)
def test_double_booking_race_across_servers(name):
    """4 'servers' (separate connections) x 10 people grab the same slot: exactly one wins."""
    if name in ("dbm", "lmdb"):
        pytest.skip(f"{name} is a single-process file database (one server only)")
    ns = "r" + uuid.uuid4().hex[:10]
    stores = [storage(name, ns) for _ in range(4)]

    async def go():
        try:
            for s in stores:
                await s.setup()
            racers = [Booking(host_id="race", start=T0, end=T0 + timedelta(minutes=30), attendee_name=f"R{i}",
                              attendee_email=f"r{i}@x.com") for i in range(40)]
            wins = await asyncio.gather(*(stores[i % 4].insert_booking_if_free(b) for i, b in enumerate(racers)))
            assert sum(bool(w) for w in wins) == 1, f"{sum(bool(w) for w in wins)} bookings for one slot"
        finally:
            for s in stores:
                await s.close()

    run(go())


@pytest.mark.parametrize("name", NAMES)
def test_full_app_survives_a_restart(name):
    """Book through the REST API, 'restart' (new NodeMeet on the same database), data is still there."""
    from asgi_client import ASGIClient
    from nodemeet import MemoryMailer, NodeMeet
    ns = "a" + uuid.uuid4().hex[:10]
    first = storage(name, ns)
    second = storage(name, ns)

    def app(st):
        return NodeMeet("integration-secret-long-enough!", db=st, api_key="k", mailer=MemoryMailer(),
                        reminders=False, sfu=False, base_url="http://test")

    async def go():
        m1 = app(first)
        m1.hours("ada", "mon-sun 0-23:59", timezone="UTC")
        m1.room("standup", preset="open", join_list=["ravi@x.com"])
        m1.role("teacher", can="moderate")
        await m1.startup()
        c = ASGIClient(m1.asgi())
        slots = (await c.request("GET", "/api/hosts/ada/slots?days=3")).json()["slots"]
        body = {"host_id": "ada", "start": slots[3]["start"], "name": "Ravi", "email": "ravi@x.com"}
        r1 = await c.request("POST", "/api/bookings", json=body)
        r2 = await c.request("POST", "/api/bookings", json=body)
        assert r1.status == 201 and r2.status == 409
        bid = (r1.json().get("booking") or r1.json())["id"]
        await m1.shutdown()
        m2 = app(second)
        await m2.startup()
        assert (await m2.bookings.get(bid)).attendee_email == "ravi@x.com"
        assert (await m2.storage.get_room("standup")).allowed_users == ["ravi@x.com"]
        assert "moderate.kick" in (await m2.roles.get("teacher")).permissions
        await m2.shutdown()

    run(go())
