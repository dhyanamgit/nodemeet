"""Key-value backends for external databases. Each imports its client lazily.

Usage: ``NodeMeet(secret, storage=KeyValueStorage(RedisKV("redis://...")))``
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

from .kv import KVBackend, Pair, ThreadedKV


def _need(module: str, pip: str) -> Any:
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        raise RuntimeError(f"this backend needs `pip install {pip}`") from None


# -- Redis family -----------------------------------------------------------------
_DEL_IF = "if redis.call('hget', KEYS[1], ARGV[1]) == ARGV[2] then return redis.call('hdel', KEYS[1], ARGV[1]) else return 0 end"


class RedisKV(KVBackend):
    """Redis / Valkey / KeyDB / Dragonfly / Garnet. One hash per space.

    Enable persistence (AOF) if Redis is your system of record."""

    def __init__(self, url: str = "redis://localhost:6379/0", *, prefix: str = "nm:",
                 client: Any = None) -> None:
        self.url, self.prefix, self.r = url, prefix, client

    async def setup(self) -> None:
        if self.r is None:
            self.r = _need("redis.asyncio", "redis").from_url(self.url, decode_responses=True)

    async def close(self) -> None:
        if self.r is not None:
            await (self.r.aclose() if hasattr(self.r, "aclose") else self.r.close())

    def _h(self, space: str) -> str:
        return self.prefix + space

    async def get(self, space: str, key: str) -> Optional[str]:
        return await self.r.hget(self._h(space), key)

    async def put(self, space: str, key: str, value: str) -> None:
        await self.r.hset(self._h(space), key, value)

    async def delete(self, space: str, key: str) -> None:
        await self.r.hdel(self._h(space), key)

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        out, cursor = [], 0
        while True:
            cursor, chunk = await self.r.hscan(self._h(space), cursor, count=500)
            out.extend((k, v) for k, v in chunk.items() if k.startswith(prefix))
            if not cursor:
                return sorted(out)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        return bool(await self.r.hsetnx(self._h(space), key, value))

    async def delete_if_equals(self, space: str, key: str, value: str) -> bool:
        return bool(await self.r.eval(_DEL_IF, 1, self._h(space), key, value))


# -- AWS DynamoDB (also ScyllaDB Alternator, LocalStack) ----------------------------------
class DynamoDBKV(ThreadedKV):
    """Single table: partition key ``space`` (S), sort key ``k`` (S), attribute ``v``."""

    def __init__(self, table: str = "nodemeet", *, client: Any = None, create_table: bool = True,
                 **client_kwargs: Any) -> None:
        super().__init__()
        self.table, self.client, self.create = table, client, create_table
        self.client_kwargs = client_kwargs  # region_name=..., endpoint_url=... (Alternator/LocalStack)

    async def setup(self) -> None:
        if self.client is None:
            self.client = _need("boto3", "boto3").client("dynamodb", **self.client_kwargs)
        if self.create:
            def ensure() -> None:
                try:
                    self.client.describe_table(TableName=self.table)
                except self.client.exceptions.ResourceNotFoundException:
                    self.client.create_table(
                        TableName=self.table, BillingMode="PAY_PER_REQUEST",
                        AttributeDefinitions=[{"AttributeName": "space", "AttributeType": "S"},
                                              {"AttributeName": "k", "AttributeType": "S"}],
                        KeySchema=[{"AttributeName": "space", "KeyType": "HASH"},
                                   {"AttributeName": "k", "KeyType": "RANGE"}])
                    self.client.get_waiter("table_exists").wait(TableName=self.table)
            await self._run(ensure)

    def _key(self, space: str, key: str) -> Dict[str, Any]:
        return {"space": {"S": space}, "k": {"S": key}}

    async def get(self, space: str, key: str) -> Optional[str]:
        def op() -> Optional[str]:
            item = self.client.get_item(TableName=self.table, Key=self._key(space, key),
                                        ConsistentRead=True).get("Item")
            return item["v"]["S"] if item else None
        return await self._run(op)

    async def put(self, space: str, key: str, value: str) -> None:
        await self._run(lambda: self.client.put_item(
            TableName=self.table, Item={**self._key(space, key), "v": {"S": value}}))

    async def delete(self, space: str, key: str) -> None:
        await self._run(lambda: self.client.delete_item(TableName=self.table, Key=self._key(space, key)))

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        def op() -> List[Pair]:
            out, start = [], None
            while True:
                kw: Dict[str, Any] = dict(TableName=self.table, ConsistentRead=True,
                                          ExpressionAttributeValues={":s": {"S": space}})
                if prefix:
                    kw["KeyConditionExpression"] = "#s = :s AND begins_with(k, :p)"
                    kw["ExpressionAttributeValues"][":p"] = {"S": prefix}
                else:
                    kw["KeyConditionExpression"] = "#s = :s"
                kw["ExpressionAttributeNames"] = {"#s": "space"}
                if start:
                    kw["ExclusiveStartKey"] = start
                page = self.client.query(**kw)
                out.extend((i["k"]["S"], i["v"]["S"]) for i in page.get("Items", []))
                start = page.get("LastEvaluatedKey")
                if not start:
                    return out  # already sorted by the sort key
        return await self._run(op)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            try:
                self.client.put_item(TableName=self.table, Item={**self._key(space, key), "v": {"S": value}},
                                     ConditionExpression="attribute_not_exists(k)")
                return True
            except self.client.exceptions.ConditionalCheckFailedException:
                return False
        return await self._run(op)

    async def delete_if_equals(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            try:
                self.client.delete_item(TableName=self.table, Key=self._key(space, key),
                                        ConditionExpression="v = :v",
                                        ExpressionAttributeValues={":v": {"S": value}})
                return True
            except self.client.exceptions.ConditionalCheckFailedException:
                return False
        return await self._run(op)


# -- Cassandra / ScyllaDB / DataStax Astra ------------------------------------------------
class CassandraKV(ThreadedKV):
    """Table ``(space text, k text, v text, PRIMARY KEY (space, k))``; lightweight
    transactions (``IF NOT EXISTS``) give atomic creates."""

    def __init__(self, session: Any, keyspace: str = "nodemeet", table: str = "nm_kv",
                 create: bool = True, replication: str = "{'class': 'SimpleStrategy', 'replication_factor': 1}") -> None:
        super().__init__()
        self.s, self.ks, self.table, self.create, self.replication = session, keyspace, table, create, replication

    async def setup(self) -> None:
        def ensure() -> None:
            if self.create:
                self.s.execute(f"CREATE KEYSPACE IF NOT EXISTS {self.ks} WITH replication = {self.replication}")
                self.s.execute(f"CREATE TABLE IF NOT EXISTS {self.ks}.{self.table} "
                               "(space text, k text, v text, PRIMARY KEY (space, k))")
        await self._run(ensure)

    @property
    def t(self) -> str:
        return f"{self.ks}.{self.table}"

    async def get(self, space: str, key: str) -> Optional[str]:
        def op() -> Optional[str]:
            row = self.s.execute(f"SELECT v FROM {self.t} WHERE space=%s AND k=%s", (space, key)).one()
            return row[0] if row else None
        return await self._run(op)

    async def put(self, space: str, key: str, value: str) -> None:
        await self._run(self.s.execute, f"INSERT INTO {self.t} (space, k, v) VALUES (%s, %s, %s)",
                        (space, key, value))

    async def delete(self, space: str, key: str) -> None:
        await self._run(self.s.execute, f"DELETE FROM {self.t} WHERE space=%s AND k=%s", (space, key))

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        def op() -> List[Pair]:
            if prefix:
                rows = self.s.execute(f"SELECT k, v FROM {self.t} WHERE space=%s AND k >= %s AND k < %s",
                                      (space, prefix, prefix + "\uffff"))
            else:
                rows = self.s.execute(f"SELECT k, v FROM {self.t} WHERE space=%s", (space,))
            return [(r[0], r[1]) for r in rows]
        return await self._run(op)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            r = self.s.execute(f"INSERT INTO {self.t} (space, k, v) VALUES (%s, %s, %s) IF NOT EXISTS",
                               (space, key, value))
            return bool(r.was_applied)
        return await self._run(op)

    async def delete_if_equals(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            r = self.s.execute(f"DELETE FROM {self.t} WHERE space=%s AND k=%s IF v = %s", (space, key, value))
            return bool(r.was_applied)
        return await self._run(op)


# -- HTTP helpers (etcd, Consul) -------------------------------------------------------------
def _http(method: str, url: str, body: Any = None, headers: Optional[Dict[str, str]] = None,
          timeout: float = 10.0) -> Any:
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


def _unb64(s: str) -> str:
    return base64.b64decode(s).decode()


class EtcdKV(ThreadedKV):
    """etcd v3 through its JSON gateway (``http://host:2379``); no client library needed."""

    def __init__(self, url: str = "http://localhost:2379", *, root: str = "/nodemeet",
                 headers: Optional[Dict[str, str]] = None) -> None:
        super().__init__()
        self.url, self.root, self.headers = url.rstrip("/"), root.rstrip("/"), headers or {}

    def _k(self, space: str, key: str) -> str:
        return f"{self.root}/{space}/{key}"

    def _call(self, path: str, body: Dict[str, Any]) -> Any:
        return _http("POST", f"{self.url}/v3/{path}", body, {"Content-Type": "application/json", **self.headers})

    async def get(self, space: str, key: str) -> Optional[str]:
        def op() -> Optional[str]:
            kvs = (self._call("kv/range", {"key": _b64(self._k(space, key))}) or {}).get("kvs") or []
            return _unb64(kvs[0].get("value", "")) if kvs else None
        return await self._run(op)

    async def put(self, space: str, key: str, value: str) -> None:
        await self._run(self._call, "kv/put", {"key": _b64(self._k(space, key)), "value": _b64(value)})

    async def delete(self, space: str, key: str) -> None:
        await self._run(self._call, "kv/deleterange", {"key": _b64(self._k(space, key))})

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        def op() -> List[Pair]:
            start = self._k(space, prefix).encode()
            end = start[:-1] + bytes([start[-1] + 1])
            res = self._call("kv/range", {"key": base64.b64encode(start).decode(),
                                          "range_end": base64.b64encode(end).decode()}) or {}
            head = len(self._k(space, ""))
            return sorted((_unb64(kv["key"])[head:], _unb64(kv.get("value", ""))) for kv in res.get("kvs") or [])
        return await self._run(op)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        k = _b64(self._k(space, key))
        body = {"compare": [{"key": k, "target": "CREATE", "result": "EQUAL", "create_revision": "0"}],
                "success": [{"request_put": {"key": k, "value": _b64(value)}}]}
        return bool((await self._run(self._call, "kv/txn", body) or {}).get("succeeded"))

    async def delete_if_equals(self, space: str, key: str, value: str) -> bool:
        k = _b64(self._k(space, key))
        body = {"compare": [{"key": k, "target": "VALUE", "result": "EQUAL", "value": _b64(value)}],
                "success": [{"request_delete_range": {"key": k}}]}
        return bool((await self._run(self._call, "kv/txn", body) or {}).get("succeeded"))


class ConsulKV(ThreadedKV):
    """HashiCorp Consul KV (``http://host:8500``); uses check-and-set for atomicity."""

    def __init__(self, url: str = "http://localhost:8500", *, root: str = "nodemeet",
                 token: Optional[str] = None) -> None:
        super().__init__()
        self.url, self.root = url.rstrip("/"), root.strip("/")
        self.headers = {"X-Consul-Token": token} if token else {}

    def _u(self, space: str, key: str, query: str = "") -> str:
        return f"{self.url}/v1/kv/{self.root}/{quote(space, safe='')}/{quote(key, safe='')}{query}"

    async def get(self, space: str, key: str) -> Optional[str]:
        def op() -> Optional[str]:
            res = _http("GET", self._u(space, key), headers=self.headers)
            return _unb64(res[0]["Value"]) if res and res[0].get("Value") is not None else ("" if res else None)
        return await self._run(op)

    async def put(self, space: str, key: str, value: str) -> None:
        await self._run(_http, "PUT", self._u(space, key), value.encode(), self.headers)

    async def delete(self, space: str, key: str) -> None:
        await self._run(_http, "DELETE", self._u(space, key), None, self.headers)

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        def op() -> List[Pair]:
            base = f"{self.root}/{quote(space, safe='')}/"
            res = _http("GET", f"{self.url}/v1/kv/{base}{quote(prefix, safe='')}?recurse=true",
                        headers=self.headers) or []
            return sorted((unquote(item["Key"][len(base):]), _unb64(item["Value"] or ""))
                          for item in res)
        return await self._run(op)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        return bool(await self._run(_http, "PUT", self._u(space, key, "?cas=0"), value.encode(), self.headers))

    async def delete_if_equals(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            res = _http("GET", self._u(space, key), headers=self.headers)
            if not res or _unb64(res[0].get("Value") or "") != value:
                return False
            return bool(_http("DELETE", self._u(space, key, f"?cas={res[0]['ModifyIndex']}"),
                              headers=self.headers))
        return await self._run(op)


# -- LMDB (embedded, memory-mapped, safe across processes) ------------------------------------
class LMDBKV(ThreadedKV):
    def __init__(self, path: str = "nodemeet.lmdb", *, map_size: int = 1 << 30) -> None:
        super().__init__()
        self.path, self.map_size, self.env = path, map_size, None

    async def setup(self) -> None:
        if self.env is None:
            lmdb = _need("lmdb", "lmdb")
            self.env = await self._run(lambda: lmdb.open(self.path, map_size=self.map_size, subdir=True))

    async def close(self) -> None:
        if self.env is not None:
            await self._run(self.env.close)
            self.env = None

    @staticmethod
    def _k(space: str, key: str) -> bytes:
        return f"{space}\x00{key}".encode()

    async def get(self, space: str, key: str) -> Optional[str]:
        def op() -> Optional[str]:
            with self.env.begin() as txn:
                v = txn.get(self._k(space, key))
                return bytes(v).decode() if v is not None else None
        return await self._run(op)

    async def put(self, space: str, key: str, value: str) -> None:
        def op() -> None:
            with self.env.begin(write=True) as txn:
                txn.put(self._k(space, key), value.encode())
        await self._run(op)

    async def delete(self, space: str, key: str) -> None:
        def op() -> None:
            with self.env.begin(write=True) as txn:
                txn.delete(self._k(space, key))
        await self._run(op)

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        head = self._k(space, prefix)
        cut = len(space) + 1

        def op() -> List[Pair]:
            out = []
            with self.env.begin() as txn:
                cur = txn.cursor()
                if cur.set_range(head):
                    for k, v in cur:
                        if not bytes(k).startswith(head):
                            break
                        out.append((bytes(k).decode()[cut:], bytes(v).decode()))
            return out
        return await self._run(op)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            with self.env.begin(write=True) as txn:
                return bool(txn.put(self._k(space, key), value.encode(), overwrite=False))
        return await self._run(op)

    async def delete_if_equals(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            with self.env.begin(write=True) as txn:
                k = self._k(space, key)
                cur = txn.get(k)
                if cur is not None and bytes(cur).decode() == value:
                    return bool(txn.delete(k))
                return False
        return await self._run(op)


# -- FoundationDB ----------------------------------------------------------------------------
class FoundationDBKV(ThreadedKV):
    """Keys are tuple-packed ``(root, space, key)``; every op is a serializable transaction."""

    def __init__(self, *, cluster_file: Optional[str] = None, root: str = "nodemeet",
                 api_version: int = 710) -> None:
        super().__init__()
        self.cluster_file, self.root, self.api_version, self.db = cluster_file, root, api_version, None

    async def setup(self) -> None:
        fdb = _need("fdb", "foundationdb")
        self.fdb = fdb

        def open_db() -> Any:
            try:
                fdb.api_version(self.api_version)
            except RuntimeError:
                pass  # already selected
            return fdb.open(self.cluster_file) if self.cluster_file else fdb.open()
        self.db = await self._run(open_db)

    def _k(self, space: str, key: str) -> bytes:
        return self.fdb.tuple.pack((self.root, space, key))

    async def get(self, space: str, key: str) -> Optional[str]:
        def op() -> Optional[str]:
            v = self.db[self._k(space, key)]
            return bytes(v).decode() if v.present() else None
        return await self._run(op)

    async def put(self, space: str, key: str, value: str) -> None:
        await self._run(self.db.__setitem__, self._k(space, key), value.encode())

    async def delete(self, space: str, key: str) -> None:
        await self._run(self.db.__delitem__, self._k(space, key))

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        def op() -> List[Pair]:
            rng = self.fdb.tuple.range((self.root, space))
            out = []
            for kv in self.db.get_range(rng.start, rng.stop):
                key = self.fdb.tuple.unpack(kv.key)[2]
                if key.startswith(prefix):
                    out.append((key, bytes(kv.value).decode()))
            return out
        return await self._run(op)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        k = self._k(space, key)

        def op() -> bool:
            @self.fdb.transactional
            def txn(tr: Any) -> bool:
                if tr[k].present():
                    return False
                tr[k] = value.encode()
                return True
            return txn(self.db)
        return await self._run(op)

    async def delete_if_equals(self, space: str, key: str, value: str) -> bool:
        k = self._k(space, key)

        def op() -> bool:
            @self.fdb.transactional
            def txn(tr: Any) -> bool:
                cur = tr[k]
                if cur.present() and bytes(cur).decode() == value:
                    del tr[k]
                    return True
                return False
            return txn(self.db)
        return await self._run(op)


# -- S3-compatible object storage (AWS S3, MinIO, Cloudflare R2, Ceph, Wasabi...) ---------------
class S3KV(ThreadedKV):
    """One object per record. Atomic creates use conditional writes (``If-None-Match: *``),
    supported by AWS S3 (2024+), MinIO, Cloudflare R2 and Ceph RGW."""

    def __init__(self, bucket: str, *, prefix: str = "nodemeet/", client: Any = None,
                 **client_kwargs: Any) -> None:
        super().__init__()
        self.bucket, self.prefix, self.client, self.client_kwargs = bucket, prefix, client, client_kwargs

    async def setup(self) -> None:
        if self.client is None:
            self.client = _need("boto3", "boto3").client("s3", **self.client_kwargs)

    def _o(self, space: str, key: str) -> str:
        return f"{self.prefix}{quote(space, safe='')}/{quote(key, safe='')}"

    async def get(self, space: str, key: str) -> Optional[str]:
        def op() -> Optional[str]:
            try:
                return self.client.get_object(Bucket=self.bucket, Key=self._o(space, key))["Body"].read().decode()
            except self.client.exceptions.NoSuchKey:
                return None
        return await self._run(op)

    async def put(self, space: str, key: str, value: str) -> None:
        await self._run(lambda: self.client.put_object(Bucket=self.bucket, Key=self._o(space, key),
                                                       Body=value.encode(), ContentType="application/json"))

    async def delete(self, space: str, key: str) -> None:
        await self._run(lambda: self.client.delete_object(Bucket=self.bucket, Key=self._o(space, key)))

    async def scan(self, space: str, prefix: str = "") -> List[Pair]:
        def op() -> List[Pair]:
            base = f"{self.prefix}{quote(space, safe='')}/"
            out = []
            for page in self.client.get_paginator("list_objects_v2").paginate(
                    Bucket=self.bucket, Prefix=base + quote(prefix, safe="")):
                for obj in page.get("Contents", []):
                    body = self.client.get_object(Bucket=self.bucket, Key=obj["Key"])["Body"].read().decode()
                    out.append((unquote(obj["Key"][len(base):]), body))
            return sorted(out)
        return await self._run(op)

    async def put_if_absent(self, space: str, key: str, value: str) -> bool:
        def op() -> bool:
            try:
                self.client.put_object(Bucket=self.bucket, Key=self._o(space, key), Body=value.encode(),
                                       IfNoneMatch="*")
                return True
            except self.client.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
                    return False
                raise
        return await self._run(op)
