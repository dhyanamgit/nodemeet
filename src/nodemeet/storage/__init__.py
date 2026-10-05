"""Pluggable persistence - pick the family that matches your infrastructure.

Relational
    SQLiteStorage              stdlib, versioned schema, multi-process safe
    SQLAlchemyStorage          any SQLAlchemy dialect (Postgres, MySQL, MSSQL, Oracle, ...)
    DBAPIStorage               any PEP 249 driver, no ORM (see nodemeet.storage.dbapi)
Key-value
    KeyValueStorage(backend)   MemoryKV, DbmKV, JsonDirKV and (nodemeet.storage.kv_backends)
                               RedisKV, DynamoDBKV, CassandraKV, EtcdKV, ConsulKV, LMDBKV,
                               FoundationDBKV, S3KV
Document / graph / multi-model
    DocumentStorage(backend)   MemoryDocs and (nodemeet.storage.doc_backends) MongoDocs,
                               CouchDBDocs, FirestoreDocs, ElasticsearchDocs, Neo4jDocs, ArangoDocs
Other
    MemoryStorage              tests / demos
    SyncStorageAdapter         wrap a synchronous implementation (Django ORM...)
    Storage                    subclass for anything else
"""
from typing import Any

from .base import Storage
from .dbapi import DBAPIStorage
from .document import DocBackend, DocumentStorage, MemoryDocs
from .kv import DbmKV, JsonDirKV, KeyValueStorage, KVBackend, MemoryKV
from .memory import MemoryStorage
from .sqlite import SQLiteStorage
from .sync import SyncStorageAdapter

_LAZY = {
    "SQLAlchemyStorage": "sql",
    **{n: "kv_backends" for n in ("RedisKV", "DynamoDBKV", "CassandraKV", "EtcdKV", "ConsulKV",
                                  "LMDBKV", "FoundationDBKV", "S3KV")},
    **{n: "doc_backends" for n in ("MongoDocs", "CouchDBDocs", "FirestoreDocs", "ElasticsearchDocs",
                                   "Neo4jDocs", "ArangoDocs")},
}

__all__ = ["Storage", "MemoryStorage", "SQLiteStorage", "SyncStorageAdapter", "DBAPIStorage",
           "KeyValueStorage", "KVBackend", "MemoryKV", "DbmKV", "JsonDirKV", "DocumentStorage",
           "DocBackend", "MemoryDocs", *_LAZY]


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        import importlib
        return getattr(importlib.import_module(f".{_LAZY[name]}", __name__), name)
    raise AttributeError(name)
