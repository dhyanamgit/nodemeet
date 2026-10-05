"""The "don't make me memorise anything" layer.

Everything here turns short strings into the real objects, so app code can say::

    meet = NodeMeet(db="postgres://u:p@host/app", email="smtp://u:p@smtp.host:587")

instead of importing and wiring storage/mailer classes by hand. All helpers are also
usable on their own (``storage_from_url``, ``mailer_from_url`` ...).
"""
from __future__ import annotations

import difflib
import importlib
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union
from urllib.parse import parse_qs, unquote, urlsplit

__all__ = ["storage_from_url", "mailer_from_url", "recording_store_from_url", "parse_minutes",
           "parse_hours", "parse_price", "suggest", "did_you_mean", "PRESETS", "room_settings",
           "need", "STORAGE_SCHEMES"]


# -- friendly errors ----------------------------------------------------------------------
def suggest(word: str, choices: Iterable[str]) -> Optional[str]:
    hits = difflib.get_close_matches(str(word), list(choices), n=1, cutoff=0.6)
    return hits[0] if hits else None


def did_you_mean(word: str, choices: Iterable[str]) -> str:
    choices = list(choices)
    hit = suggest(word, choices)
    return f" Did you mean {hit!r}?" if hit else f" Choose from: {', '.join(sorted(choices))}."


def need(module: str, extra: str, what: str) -> Any:
    """Import an optional dependency or explain exactly what to install."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(f"{what} needs the '{module.split('.')[0]}' package. "
                          f"Install it with:  pip install \"nodemeet[{extra}]\"") from exc


# -- small parsers ------------------------------------------------------------------------
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)?\s*$", re.I)


def parse_minutes(value: Union[int, float, str, None], default: int = 0) -> int:
    """``30`` / ``"30m"`` / ``"2h"`` / ``"1d"`` / ``"1 week"`` -> minutes."""
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    m = _DUR.match(str(value))
    if not m:
        raise ValueError(f"can't read duration {value!r}; try 30, '45m', '2h' or '1d'")
    n, unit = float(m.group(1)), (m.group(2) or "m").lower()[0]
    return int(n * {"m": 1, "h": 60, "d": 1440, "w": 10080}[unit])


_CLOCK = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)


def _clock(text: str) -> str:
    m = _CLOCK.match(text)
    if not m:
        raise ValueError(f"can't read time {text!r}; try '09:00', '9', '9am' or '5:30pm'")
    h, mins, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    if not (0 <= h <= 24 and 0 <= mins < 60):
        raise ValueError(f"time out of range: {text!r}")
    if h == 24:
        return "23:59"
    return f"{h:02d}:{mins:02d}"


def _windows(text: str) -> str:
    out = []
    for part in re.split(r",|&|\band\b", text):
        part = part.strip()
        if not part:
            continue
        bits = re.split(r"\s*(?:-|\bto\b)\s*", part, maxsplit=1)
        if len(bits) != 2 or not bits[1].strip():
            raise ValueError(f"time range {part!r} needs a start and an end, e.g. '9-17' or '09:00-12:30'")
        out.append(f"{_clock(bits[0])}-{_clock(bits[1])}")
    return ", ".join(out)


_DAYS = r"(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*"
_DAYSPEC = re.compile(rf"^\s*((?:{_DAYS}|weekdays|weekends|daily|everyday|every day)"
                      rf"(?:\s*[-,]\s*{_DAYS})*)\s*:?\s*(.*)$", re.I)


def parse_hours(spec: Union[str, Dict[str, Any]]) -> Dict[str, str]:
    """Friendly weekly hours -> the dict ``Availability.from_hours`` takes.

    ``"mon-fri 9-17"``, ``"mon-fri 9am-12pm, 2pm-6pm; sat 10-13"``, ``"weekdays 09:00-17:00"``
    or a dict ``{"mon-fri": "9-17"}``.
    """
    if isinstance(spec, dict):
        return {str(k): (v if isinstance(v, str) and re.search(r"\d:\d\d-\d", v) else _windows(str(v)))
                for k, v in spec.items()}
    out: Dict[str, str] = {}
    for chunk in re.split(r"[;\n]", str(spec)):
        if not chunk.strip():
            continue
        m = _DAYSPEC.match(chunk)
        if not m or not m.group(2).strip():
            raise ValueError(f"can't read hours {chunk.strip()!r}; try 'mon-fri 9-17' or "
                             "'mon,wed 10am-2pm; sat 10-13'")
        days = m.group(1).lower().replace(" ", "")
        days = {"weekdays": "mon-fri", "weekends": "sat,sun", "daily": "mon-sun",
                "everyday": "mon-sun"}.get(days, days)
        days = re.sub(rf"({_DAYS})", lambda d: d.group(1)[:3], days)
        out[days] = (out[days] + ", " if days in out else "") + _windows(m.group(2))
    return out


_CURRENCY_SIGNS = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP", "¥": "JPY"}
_ZERO_DECIMAL = {"JPY", "KRW", "VND", "CLP", "ISK", "UGX"}


def parse_price(value: Union[int, float, str, None], currency: Optional[str] = None) -> Tuple[int, Optional[str]]:
    """``"499 INR"``, ``"₹499"``, ``"$19.99"`` or ``19.99`` -> (smallest unit, currency).

    Integers are taken as-is (already in cents/paise) for backwards compatibility.
    """
    if value is None or value == "" or value == 0:
        return 0, currency
    if isinstance(value, int):
        return value, currency
    text = str(value).strip()
    cur = currency
    for sign, code in _CURRENCY_SIGNS.items():
        if text.startswith(sign):
            cur, text = cur or code, text[1:]
    m = re.match(r"^\s*([0-9][0-9,]*(?:\.\d+)?)\s*([A-Za-z]{3})?\s*$", text)
    if not m:
        raise ValueError(f"can't read price {value!r}; try '499 INR', '$19.99' or 19.99")
    cur = (m.group(2) or cur or "USD").upper()
    amount = float(m.group(1).replace(",", ""))
    return int(round(amount * (1 if cur in _ZERO_DECIMAL else 100))), cur


# -- database URLs ------------------------------------------------------------------------
def _parts(url: str) -> Any:
    u = urlsplit(url)
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    path = unquote(u.path.lstrip("/"))
    return u, q, path, (unquote(u.username) if u.username else None), \
        (unquote(u.password) if u.password else None)


def _http_url(u: Any, secure_schemes: Sequence[str]) -> str:
    proto = "https" if u.scheme.lower() in secure_schemes or "ssl=true" in (u.query or "").lower() else "http"
    host = u.hostname or "localhost"
    return f"{proto}://{host}{':' + str(u.port) if u.port else ''}"


def _file_path(url: str, default: str) -> str:
    """``sqlite:///rel.db`` -> rel.db, ``sqlite:////abs.db`` -> /abs.db, ``sqlite://`` -> default."""
    rest = url.split("://", 1)[1] if "://" in url else url
    if rest.startswith("/"):
        rest = rest[1:]
    rest = rest.split("?", 1)[0]
    return unquote(rest) or default


def _sqlalchemy(url: str, driver: str, extra: str, prefix: str = "nm_") -> Any:
    need("sqlalchemy", extra, "This database URL")
    scheme, rest = url.split("://", 1)
    if "+" not in scheme:
        url = f"{driver}://{rest}"
    from .storage.sql import SQLAlchemyStorage
    return SQLAlchemyStorage(url, table_prefix=prefix)


def _ns_kv(backend: Any, ns: str) -> Any:
    if not ns:
        return backend
    from .storage.kv import NamespacedKV
    return NamespacedKV(backend, ns)


def _dbapi(module: str, extra: str, dialect: str, connect: Callable[[Any], Any], prefix: str = "nm_") -> Any:
    mod = need(module, extra, f"The {dialect} database")
    from .storage.dbapi import DBAPIStorage
    return DBAPIStorage(lambda: connect(mod), dialect=dialect, table_prefix=prefix)


def _sql_or_dbapi(url: str, driver: str, extra: str, module: str, dialect: str,
                  connect: Callable[[Any], Any], prefix: str = "nm_") -> Any:
    """Prefer SQLAlchemy (async, pooled); fall back to a plain DB-API driver if that's all you have."""
    scheme = url.split("://", 1)[0]
    if "+" in scheme:
        return _sqlalchemy(url, driver, extra, prefix)
    try:
        importlib.import_module("sqlalchemy")
        importlib.import_module(driver.split("+", 1)[1] if "+" in driver else driver)
        return _sqlalchemy(url, driver, extra, prefix)
    except ImportError:
        pass
    try:
        importlib.import_module(module)
    except ImportError:
        raise ImportError(f"{dialect} needs a driver. Install one:  pip install \"nodemeet[{extra}]\"  "
                          f"(or pip install {module})") from None
    return _dbapi(module, extra, dialect, connect, prefix)


def storage_from_url(url: Union[str, Any, None], namespace: Optional[str] = None) -> Any:
    """Build a storage backend from a URL (or pass a Storage straight through).

    ======================================  =========================================
    ``memory`` / ``None``                   in-process (tests, demos)
    ``meet.db`` / ``sqlite:///meet.db``     SQLite file (stdlib)
    ``postgres://u:p@host/db``              PostgreSQL (SQLAlchemy+asyncpg, or psycopg)
    ``mysql://`` / ``mariadb://``           MySQL / MariaDB (aiomysql, or PyMySQL)
    ``mssql://`` ``oracle://``              SQL Server, Oracle
    ``duckdb:///file.duckdb``               DuckDB
    ``snowflake://`` ``clickhouse://``      warehouses (DB-API drivers)
    ``db2://`` ``hana://`` ``firebird://``  other relational databases
    ``dialect+driver://...``                any SQLAlchemy async URL, passed through
    ``redis://`` / ``rediss://``            Redis (as the database)
    ``mongodb://`` / ``mongodb+srv://``     MongoDB (database = path, default nodemeet)
    ``couchdb://`` ``elasticsearch://``     CouchDB, Elasticsearch / OpenSearch
    ``neo4j://`` / ``bolt://``              Neo4j
    ``arangodb://``                         ArangoDB
    ``firestore://PROJECT``                 Google Firestore
    ``dynamodb://TABLE?region=..``          AWS DynamoDB
    ``s3://BUCKET/prefix``                  any S3-compatible object store
    ``cassandra://h1,h2/keyspace``          Cassandra / ScyllaDB
    ``etcd://`` ``consul://``               etcd, Consul KV
    ``lmdb:///path`` ``dbm:///path``        embedded key-value files
    ``fdb://`` / ``foundationdb://``        FoundationDB
    ``dir:///path``                         one JSON file per record (no database at all)
    ======================================  =========================================
    """
    from .storage import (DbmKV, DocumentStorage, JsonDirKV, KeyValueStorage, MemoryStorage,
                          SQLiteStorage, Storage)
    if url is None or url == "" or url is False:
        return MemoryStorage()
    if isinstance(url, Storage) or (not isinstance(url, str) and hasattr(url, "save_booking")):
        return url
    url = str(url).strip()
    m_ns = re.search(r"([?&])(?:namespace|ns)=([A-Za-z0-9_]+)&?", url)
    if m_ns:  # ?namespace=app1 keeps several apps / test runs apart in one database
        namespace = namespace or m_ns.group(2)
        url = (url[:m_ns.start()] + (m_ns.group(1) if url[m_ns.end():] else "") + url[m_ns.end():]).rstrip("?&")
    ns = re.sub(r"[^a-z0-9_]", "_", namespace.lower()) if namespace else ""
    tp = f"{ns}_" if ns else "nm_"
    low = url.lower()
    if low in ("memory", "mem", "memory://", ":memory:"):
        return MemoryStorage()
    if "://" not in url:
        if low.endswith((".db", ".sqlite", ".sqlite3")):
            return SQLiteStorage(url, table_prefix=tp)
        if low.endswith(".duckdb"):
            url = "duckdb:///" + url
        else:
            raise ValueError(f"can't tell what database {url!r} is. Use a URL such as "
                             "'meet.db', 'postgres://user:pass@host/app' or 'mongodb://host/app'.")
    scheme = url.split("://", 1)[0].lower()
    base = scheme.split("+", 1)[0]
    u, q, path, user, pw = _parts(url)

    if base == "sqlite":
        if "+" in scheme:
            return _sqlalchemy(url, "sqlite+aiosqlite", "sql", tp)
        return SQLiteStorage(_file_path(url, "nodemeet.sqlite3"), table_prefix=tp)
    if base in ("postgres", "postgresql", "pg", "cockroachdb", "cockroach", "timescale", "yugabyte",
                "redshift", "neon", "supabase"):
        rest = url.split("://", 1)[1]
        norm = url if "+" in scheme else "postgresql://" + rest
        return _sql_or_dbapi(norm, "postgresql+asyncpg", "postgres", "psycopg", "postgres",
                             lambda m: m.connect(norm), tp)
    if base in ("mysql", "mariadb", "tidb", "aurora", "planetscale", "singlestore", "memsql"):
        norm = url if "+" in scheme else "mysql://" + url.split("://", 1)[1]
        return _sql_or_dbapi(norm, "mysql+aiomysql", "mysql", "pymysql", "mysql",
                             lambda m: m.connect(host=u.hostname or "localhost", port=u.port or 3306,
                                                 user=user, password=pw or "", database=path or None,
                                                 autocommit=False), tp)
    if base in ("mssql", "sqlserver", "azuresql"):
        norm = url if "+" in scheme else "mssql://" + url.split("://", 1)[1]
        return _sql_or_dbapi(norm, "mssql+aioodbc", "mssql", "pymssql", "mssql",
                             lambda m: m.connect(server=u.hostname or "localhost", port=u.port or 1433,
                                                 user=user, password=pw, database=path or None), tp)
    if base == "oracle":
        dsn = f"{u.hostname or 'localhost'}:{u.port or 1521}/{path or q.get('service', 'FREEPDB1')}"
        if "+" in scheme:
            return _sqlalchemy(url, "oracle+oracledb_async", "sql", tp)
        return _dbapi("oracledb", "oracle", "oracle", lambda m: m.connect(user=user, password=pw, dsn=dsn), tp)
    if base == "duckdb":
        file = _file_path(url, ":memory:")
        return _dbapi("duckdb", "duckdb", "duckdb", lambda m: m.connect(file), tp)
    if base == "snowflake":
        db, _, schema = path.partition("/")
        return _dbapi("snowflake.connector", "snowflake", "snowflake", lambda m: m.connect(
            account=u.hostname, user=user, password=pw, database=db or None, schema=schema or None,
            warehouse=q.get("warehouse"), role=q.get("role")), tp)
    if base == "clickhouse":
        return _dbapi("clickhouse_driver.dbapi", "clickhouse", "clickhouse", lambda m: m.connect(
            host=u.hostname or "localhost", port=u.port or 9000, user=user or "default",
            password=pw or "", database=path or "default"), tp)
    if base in ("db2", "ibm_db"):
        conn = (f"DATABASE={path};HOSTNAME={u.hostname or 'localhost'};PORT={u.port or 50000};"
                f"PROTOCOL=TCPIP;UID={user};PWD={pw};")
        return _dbapi("ibm_db_dbi", "db2", "db2", lambda m: m.connect(conn, "", ""), tp)
    if base in ("hana", "saphana"):
        return _dbapi("hdbcli.dbapi", "hana", "hana", lambda m: m.connect(
            address=u.hostname or "localhost", port=u.port or 30015, user=user, password=pw), tp)
    if base == "firebird":
        return _dbapi("firebird.driver", "firebird", "firebird", lambda m: m.connect(
            f"{u.hostname or 'localhost'}{'/' + str(u.port) if u.port else ''}:{path}", user=user, password=pw), tp)
    if "+" in scheme:  # any other SQLAlchemy async URL
        return _sqlalchemy(url, scheme, "sql", tp)

    # key-value
    if base in ("redis", "rediss", "valkey", "keydb", "dragonfly"):
        need("redis", "redis", "Redis storage")
        from .storage.kv_backends import RedisKV
        real = url if base in ("redis", "rediss") else "redis://" + url.split("://", 1)[1]
        return KeyValueStorage(RedisKV(real, prefix=q.get("prefix") or (f"{ns}:" if ns else "nm:")))
    if base == "dynamodb":
        need("boto3", "aws", "DynamoDB")
        from .storage.kv_backends import DynamoDBKV
        kw = {k: v for k, v in (("region_name", q.get("region")), ("endpoint_url", q.get("endpoint"))) if v}
        return KeyValueStorage(DynamoDBKV((f"{ns}_" if ns else "") + (u.hostname or path or "nodemeet"), **kw))
    if base == "s3":
        need("boto3", "aws", "S3 storage")
        from .storage.kv_backends import S3KV
        kw = {k: v for k, v in (("region_name", q.get("region")), ("endpoint_url", q.get("endpoint"))) if v}
        return KeyValueStorage(S3KV(u.hostname or "", prefix=((path.rstrip("/") + "/") if path else "nodemeet/") + (f"{ns}/" if ns else ""), **kw))
    if base in ("cassandra", "scylla", "scylladb"):
        cluster = need("cassandra.cluster", "cassandra", "Cassandra")
        from .storage.kv_backends import CassandraKV
        hosts = [h for h in (url.split("://", 1)[1].split("/", 1)[0].split("@")[-1]).split(",") if h]
        port = int(hosts[0].split(":")[1]) if hosts and ":" in hosts[0] else 9042
        hosts = [h.split(":")[0] for h in hosts] or ["127.0.0.1"]
        auth = None
        if user:
            auth = need("cassandra.auth", "cassandra", "Cassandra").PlainTextAuthProvider(user, pw)
        session = cluster.Cluster(hosts, port=port, auth_provider=auth).connect()
        return KeyValueStorage(CassandraKV(session, keyspace=path or "nodemeet", table=f"{tp}kv"))
    if base == "etcd":
        from .storage.kv_backends import EtcdKV
        return KeyValueStorage(EtcdKV(_http_url(u, ("etcds", "https")), root="/" + (path or "nodemeet") + (f"/{ns}" if ns else "")))
    if base == "consul":
        from .storage.kv_backends import ConsulKV
        return KeyValueStorage(ConsulKV(_http_url(u, ("consuls", "https")), root=(path or "nodemeet") + (f"/{ns}" if ns else ""),
                                        token=q.get("token") or pw))
    if base == "lmdb":
        need("lmdb", "lmdb", "LMDB")
        from .storage.kv_backends import LMDBKV
        return KeyValueStorage(_ns_kv(LMDBKV(_file_path(url, "nodemeet.lmdb")), ns))
    if base in ("fdb", "foundationdb"):
        need("fdb", "foundationdb", "FoundationDB")
        from .storage.kv_backends import FoundationDBKV
        return KeyValueStorage(_ns_kv(FoundationDBKV(cluster_file=_file_path(url, "") or None), ns))
    if base == "dbm":
        return KeyValueStorage(_ns_kv(DbmKV(_file_path(url, "nodemeet.dbm")), ns))
    if base in ("dir", "json", "file", "folder"):
        return KeyValueStorage(_ns_kv(JsonDirKV(_file_path(url, "nodemeet-data")), ns))

    # document / graph
    if base in ("mongodb", "mongo", "documentdb", "cosmosdb"):
        need("pymongo", "mongo", "MongoDB")
        from .storage.doc_backends import MongoDocs
        return DocumentStorage(MongoDocs(url.split("?")[0] if ns else url, database=ns or path.split("/")[0] or "nodemeet"))
    if base in ("couchdb", "couchdbs", "pouchdb"):
        from .storage.doc_backends import CouchDBDocs
        return DocumentStorage(CouchDBDocs(_http_url(u, ("couchdbs",)), user=user, password=pw,
                                           prefix=q.get("prefix") or (f"{ns}_" if ns else "")))
    if base in ("elasticsearch", "elastic", "es", "opensearch", "elasticsearchs", "opensearchs"):
        from .storage.doc_backends import ElasticsearchDocs
        return DocumentStorage(ElasticsearchDocs(_http_url(u, ("elasticsearchs", "opensearchs", "https")),
                                                 user=user, password=pw, api_key=q.get("api_key"),
                                                 prefix=q.get("prefix") or (f"{ns}_" if ns else "")))
    if base in ("neo4j", "neo4j+s", "bolt", "memgraph"):
        need("neo4j", "neo4j", "Neo4j")
        from .storage.doc_backends import Neo4jDocs
        real = url.split("@")[-1] if "@" in url else url.split("://", 1)[1]
        proto = "bolt" if base in ("bolt", "memgraph") else scheme
        return DocumentStorage(Neo4jDocs(f"{proto}://{real.split('/')[0]}", auth=(user, pw) if user else None,
                                         database=path or None, prefix=f"{ns}_" if ns else ""))
    if base in ("arangodb", "arango", "arangodbs"):
        from .storage.doc_backends import ArangoDocs
        return DocumentStorage(ArangoDocs(_http_url(u, ("arangodbs",)), database=path or "_system",
                                          user=user or "root", password=pw or "", prefix=f"{ns}_" if ns else ""))
    if base == "firestore":
        need("google.cloud.firestore", "firestore", "Firestore")
        from .storage.doc_backends import FirestoreDocs
        kw = {"project": u.hostname} if u.hostname else {}
        return DocumentStorage(FirestoreDocs(prefix=path + (f"{ns}_" if ns else ""), **kw))
    raise ValueError(f"unknown database URL scheme {scheme!r}." + did_you_mean(scheme, STORAGE_SCHEMES))


STORAGE_SCHEMES = (
    "memory", "sqlite", "postgres", "postgresql", "mysql", "mariadb", "mssql", "oracle", "duckdb",
    "snowflake", "clickhouse", "db2", "hana", "firebird", "cockroachdb", "redis", "rediss", "dynamodb",
    "s3", "cassandra", "scylla", "etcd", "consul", "lmdb", "fdb", "dbm", "dir", "mongodb", "couchdb",
    "elasticsearch", "opensearch", "neo4j", "bolt", "arangodb", "firestore")


# -- email --------------------------------------------------------------------------------
def mailer_from_url(url: Union[str, Any, None]) -> Any:
    """``smtp://user:pass@smtp.host:587?from=me@x.com&name=Acme`` -> SMTPMailer.

    ``smtps://`` = implicit TLS (465), ``smtp://...?security=none`` = plain.
    ``console`` / ``log`` prints mail to the log, ``memory`` keeps it in ``mailer.outbox``.
    Gmail: ``smtp://you@gmail.com:APP_PASSWORD@smtp.gmail.com:587``.
    """
    from .integrations import LoggingMailer, MemoryMailer, SMTPMailer
    if url is None or url == "":
        return None
    if not isinstance(url, str):
        return url
    low = url.strip().lower()
    if low in ("console", "log", "logging", "print", "stdout"):
        return LoggingMailer()
    if low in ("memory", "test"):
        return MemoryMailer()
    if "://" not in low:
        raise ValueError(f"email should look like 'smtp://user:pass@host:587?from=me@you.com' "
                         f"or 'console', got {url!r}")
    scheme = low.split("://", 1)[0]
    if scheme not in ("smtp", "smtps", "smtp+ssl", "smtp+starttls", "smtp+tls"):
        raise ValueError(f"unknown email scheme {scheme!r}; use smtp:// or smtps://")
    # usernames are often email addresses: allow an unescaped '@' in them
    rest = url.split("://", 1)[1]
    creds, _, hostpart = rest.rpartition("@")
    hostport, _, query = hostpart.partition("?")
    hostport = hostport.rstrip("/")
    q = {k: v[-1] for k, v in parse_qs(query).items()}
    user, _, pw = creds.partition(":") if creds else ("", "", "")
    host, _, port = hostport.partition(":")
    security = q.get("security") or ("ssl" if scheme in ("smtps", "smtp+ssl") else "starttls")
    port_n = int(port) if port else (465 if security == "ssl" else 587)
    if port_n == 25 and "security" not in q:
        security = "none"
    sender = q.get("from") or q.get("sender") or (unquote(user) if "@" in unquote(user) else f"nodemeet@{host}")
    return SMTPMailer(host, port_n, username=unquote(user) or None, password=unquote(pw) or None,
                      sender=sender, sender_name=q.get("name", ""), security=security)


def recording_store_from_url(url: Union[str, Any, None]) -> Any:
    """``"recordings/"`` (a folder) or ``"s3://bucket/prefix?region=..&endpoint=.."``."""
    from .recordings import FileRecordingStore, S3RecordingStore
    if url is None or url == "":
        return None
    if not isinstance(url, str):
        return url
    if url.lower().startswith("s3://"):
        need("boto3", "aws", "S3 recordings")
        u, q, path, _, _ = _parts(url)
        kw = {k: v for k, v in (("region_name", q.get("region")), ("endpoint_url", q.get("endpoint"))) if v}
        return S3RecordingStore(u.hostname or "", prefix=(path.rstrip("/") + "/") if path else "recordings/", **kw)
    return FileRecordingStore(_file_path(url, "recordings") if "://" in url else url)


# -- room presets -------------------------------------------------------------------------
PRESETS: Dict[str, Dict[str, Any]] = {
    # name           what you get
    "meeting": {},                                          # waiting room on, everyone can talk
    "open": {"waiting_room": False},                        # anyone with the link walks in
    "private": {"waiting_room": True, "lobby_message": "The host will let you in shortly."},
    "webinar": {"mode": "webinar", "default_role": "viewer", "waiting_room": False},
    "town-hall": {"mode": "webinar", "default_role": "viewer", "waiting_room": False},
    "classroom": {"waiting_room": True,
                  "role_overrides": {"participant": {"revoke": ["screen.publish", "chat.private"]}}},
    "interview": {"waiting_room": True, "mode": "p2p",
                  "role_overrides": {"participant": {"revoke": ["recording.start"]}}},
    "1on1": {"waiting_room": True, "mode": "p2p", "max_participants": 2},
    "support": {"waiting_room": True, "lobby_mode": "until_host"},
    "drop-in": {"waiting_room": True, "lobby_mode": "until_host"},
}

ROOM_KEYS = ("name", "mode", "max_participants", "metadata", "tenant_id", "waiting_room", "join_list",
             "chat_enabled", "locked", "default_role", "role_overrides", "branding", "lobby_mode",
             "lobby_message", "sso_required")
ROOM_ALIASES = {"lobby": "waiting_room", "allowed_users": "join_list", "allow": "join_list",
                "guests": "join_list", "invitees": "join_list", "max": "max_participants",
                "limit": "max_participants", "capacity": "max_participants", "chat": "chat_enabled",
                "tenant": "tenant_id", "sso": "sso_required", "role": "default_role",
                "title": "name", "preset_name": "name"}


def room_settings(preset: Optional[str] = None, **settings: Any) -> Dict[str, Any]:
    """Merge a preset with explicit settings and normalise friendly aliases."""
    out: Dict[str, Any] = {}
    if preset:
        key = str(preset).lower().replace("_", "-").replace(" ", "-")
        if key not in PRESETS:
            raise ValueError(f"unknown room preset {preset!r}." + did_you_mean(key, PRESETS))
        out.update({k: (dict(v) if isinstance(v, dict) else v) for k, v in PRESETS[key].items()})
    for k, v in settings.items():
        if k in ("owner", "notify"):  # who gets "someone is waiting" push notifications
            out.setdefault("metadata", {})[k] = v if k == "owner" else list(v)
            continue
        k2 = ROOM_ALIASES.get(k, k)
        if k2 not in ROOM_KEYS:
            raise TypeError(f"unknown room setting {k!r}." + did_you_mean(k, list(ROOM_KEYS) + list(ROOM_ALIASES)))
        if k2 == "join_list" and isinstance(v, str):
            v = [x.strip() for x in v.split(",") if x.strip()]
        out[k2] = v
    return out


# -- permissions in plain words -----------------------------------------------------------
PERMISSION_ALIASES: Dict[str, List[str]] = {
    "mic": ["audio.publish", "audio.unmute_self"], "microphone": ["audio.publish", "audio.unmute_self"],
    "audio": ["audio.publish", "audio.unmute_self"], "talk": ["audio.publish", "audio.unmute_self"],
    "camera": ["video.publish", "video.start_self"], "video": ["video.publish", "video.start_self"],
    "screen": ["screen.publish", "screen.audio"], "share": ["screen.publish", "screen.audio"],
    "hd": ["media.hd"], "watch": ["audio.subscribe", "video.subscribe", "screen.subscribe"],
    "chat": ["chat.read", "chat.history", "chat.send"], "dm": ["chat.private"],
    "private_chat": ["chat.private"], "links": ["chat.links"], "react": ["reactions.send"],
    "reactions": ["reactions.send"], "hand": ["hand.raise"], "rename": ["profile.rename"],
    "polls": ["polls.vote", "polls.results"], "create_polls": ["polls.create"],
    "qa": ["qa.ask", "qa.upvote"], "answer": ["qa.answer"], "whiteboard": ["whiteboard.view", "whiteboard.draw"],
    "draw": ["whiteboard.draw"], "record": ["recording.start", "recording.download"],
    "recording": ["recording.start", "recording.download"], "captions": ["captions.view", "captions.enable"],
    "transcript": ["transcript.download"], "breakouts": ["breakout.manage"],
    "skip_waiting_room": ["room.bypass_lobby"], "bypass_lobby": ["room.bypass_lobby"],
    "mute": ["moderate.mute", "moderate.mute_all", "moderate.ask_unmute"], "kick": ["moderate.kick"],
    "ban": ["moderate.ban"], "block": ["moderate.ban"], "admit": ["moderate.lobby"],
    "lobby": ["moderate.lobby"], "spotlight": ["moderate.spotlight"], "lock": ["moderate.lock"],
    "end": ["moderate.end"], "assign_roles": ["moderate.assign_roles"], "roles": ["moderate.assign_roles"],
    "moderate": ["moderate.*"], "moderator": ["moderate.*"], "everything": ["*"], "all": ["*"],
}


def friendly_permissions(perms: Union[str, Iterable[str], None]) -> List[str]:
    """``"mic, camera, chat"`` or ``["mic", "moderate.kick"]`` -> real permission names."""
    if not perms:
        return []
    items = [p.strip() for p in perms.split(",")] if isinstance(perms, str) else [str(p).strip() for p in perms]
    out: List[str] = []
    for p in items:
        if not p:
            continue
        out.extend(PERMISSION_ALIASES.get(p.lower().replace(" ", "_").replace("-", "_"), [p]))
    return out


# -- branding in plain words ---------------------------------------------------------------
BRAND_ALIASES = {"logo": "logo_url", "dark_logo": "logo_dark_url", "favicon": "favicon_url",
                 "css": "custom_css", "css_url": "custom_css_url", "head_html": "custom_head_html",
                 "title": "page_title", "layout": "default_layout", "lang": "language",
                 "background_image": "background_image_url", "redirect": "leave_redirect_url",
                 "after_leave": "leave_redirect_url", "heading_font": "heading_font_family",
                 "font_size_px": "font_size"}
_COLOR_KEYS = ("primary", "primary_text", "background", "surface", "surface_2", "tile", "border", "text",
               "muted", "danger", "success", "warning", "focus", "overlay")


def brand_settings(**kw: Any) -> Dict[str, Any]:
    """``brand(name="Acme", color="#ff5a00", logo=URL, font="Inter", whiteboard=False)``
    -> a full branding dict. Any key of ``nodemeet.branding.DEFAULTS`` also works."""
    from .branding import DEFAULTS
    out: Dict[str, Any] = {}
    features = set(DEFAULTS["features"])
    for k, v in kw.items():
        key = BRAND_ALIASES.get(k, k)
        if key in ("color", "accent", "brand_color", "primary_color"):
            out.setdefault("colors", {})["primary"] = v
            out.setdefault("light_colors", {})["primary"] = v
            out.setdefault("email", {})["accent"] = v
            out.setdefault("booking", {})["accent"] = v
        elif key == "font":
            if str(v).startswith(("http://", "https://")):
                out["font_url"] = v
            else:
                out["font_family"] = f"{v}, system-ui, sans-serif" if "," not in str(v) else v
                if " " not in str(v).strip() and "," not in str(v):
                    out.setdefault("font_url", f"https://fonts.googleapis.com/css2?family={v}:wght@400;600&display=swap")
        elif key in ("powered_by", "show_powered_by"):
            out["show_powered_by"] = bool(v)
        elif key in ("hide_powered_by", "white_label", "whitelabel"):
            out["show_powered_by"] = not bool(v)
        elif key in _COLOR_KEYS:
            out.setdefault("colors", {})[key] = v
            out.setdefault("light_colors", {})[key] = v
        elif key in features:
            out.setdefault("features", {})[key] = bool(v)
        elif key in DEFAULTS:
            out[key] = v
        else:
            choices = list(DEFAULTS) + list(BRAND_ALIASES) + list(features) + ["color", "font"] + list(_COLOR_KEYS)
            raise TypeError(f"unknown branding option {k!r}." + did_you_mean(k, choices))
    return out


# -- hook names ------------------------------------------------------------------------------
EVENT_ALIASES = {"join": "on_join", "joined": "on_join", "leave": "on_leave", "left": "on_leave",
                 "chat": "on_chat", "message": "on_chat", "hand": "on_hand_raised",
                 "hand_raised": "on_hand_raised", "room_created": "on_room_created",
                 "room_closed": "on_room_closed", "meeting_ended": "on_room_closed",
                 "waiting": "on_lobby", "waiting_room": "on_lobby", "lobby": "on_lobby",
                 "admitted": "on_admitted", "moderation": "on_moderation", "role_changed": "on_role_changed",
                 "reaction": "on_reaction", "blocked": "on_blocked_attempt",
                 "blocked_attempt": "on_blocked_attempt", "booking": "on_booking_created",
                 "booked": "on_booking_created", "booking_created": "on_booking_created",
                 "cancelled": "on_booking_cancelled", "canceled": "on_booking_cancelled",
                 "booking_cancelled": "on_booking_cancelled", "rescheduled": "on_booking_rescheduled",
                 "booking_rescheduled": "on_booking_rescheduled", "reminder": "on_reminder",
                 "before_join": "before_join", "check_join": "before_join"}


def event_name(name: str, known: Sequence[str]) -> str:
    key = str(name).strip()
    if key in known:
        return key
    norm = key.lower().replace("-", "_").replace(" ", "_")
    if norm.startswith("on_") and norm in known:
        return norm
    alias = EVENT_ALIASES.get(norm[3:] if norm.startswith("on_") else norm)
    if alias:
        return alias
    raise ValueError(f"unknown event {name!r}." + did_you_mean(norm, list(known) + list(EVENT_ALIASES)))
