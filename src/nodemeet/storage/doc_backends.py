"""Document backends for external databases (clients imported lazily).

``NodeMeet(secret, storage=DocumentStorage(MongoDocs("mongodb://...", "app")))``
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from .document import COLLECTIONS, DocBackend, Doc, Range


def _need(module: str, pip: str) -> Any:
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError:
        raise RuntimeError(f"this backend needs `pip install {pip}`") from None


# -- MongoDB (also Azure Cosmos DB for MongoDB, AWS DocumentDB, FerretDB, Percona) ---------------
class MongoDocs(DocBackend):
    def __init__(self, url: str = "mongodb://localhost:27017", database: str = "nodemeet", *,
                 db: Any = None, create_indexes: bool = True) -> None:
        self.url, self.database, self.mdb, self.create_indexes = url, database, db, create_indexes
        self._client: Any = None

    async def setup(self) -> None:
        if self.mdb is None:
            try:  # PyMongo >= 4.9 ships a native async client
                from pymongo import AsyncMongoClient
                self._client = AsyncMongoClient(self.url)
            except ImportError:
                self._client = _need("motor.motor_asyncio", "motor").AsyncIOMotorClient(self.url)
            self.mdb = self._client[self.database]
        if self.create_indexes:
            for coll, fields in COLLECTIONS.items():
                for f in fields:
                    await self.mdb[coll].create_index(f)
            await self.mdb["nm_bookings"].create_index([("host_id", 1), ("start_ms", 1)])

    async def close(self) -> None:
        if self._client is not None:
            res = self._client.close()
            if asyncio.iscoroutine(res):
                await res

    async def insert(self, coll: str, doc_id: str, doc: Doc) -> bool:
        from pymongo.errors import DuplicateKeyError
        try:
            await self.mdb[coll].insert_one({**doc, "_id": doc_id})
            return True
        except DuplicateKeyError:
            return False

    async def upsert(self, coll: str, doc_id: str, doc: Doc) -> None:
        await self.mdb[coll].replace_one({"_id": doc_id}, {**doc, "_id": doc_id}, upsert=True)

    async def get(self, coll: str, doc_id: str) -> Optional[Doc]:
        return await self.mdb[coll].find_one({"_id": doc_id})

    async def delete(self, coll: str, doc_id: str) -> None:
        await self.mdb[coll].delete_one({"_id": doc_id})

    async def delete_if(self, coll: str, doc_id: str, field: str, value: Any) -> bool:
        res = await self.mdb[coll].delete_one({"_id": doc_id, field: value})
        return res.deleted_count == 1

    async def find(self, coll: str, where: Optional[Doc] = None, *, range: Optional[Range] = None,
                   order_by: Optional[str] = None, desc: bool = False,
                   limit: Optional[int] = None) -> List[Doc]:
        q: Doc = dict(where or {})
        if range:
            f, lo, hi = range
            cond = {}
            if lo is not None:
                cond["$gte"] = lo
            if hi is not None:
                cond["$lt"] = hi
            if cond:
                q[f] = cond
        cur = self.mdb[coll].find(q)
        if order_by:
            cur = cur.sort(order_by, -1 if desc else 1)
        if limit:
            cur = cur.limit(limit)
        return [d async for d in cur]


# -- small HTTP helper for CouchDB / Elasticsearch / ArangoDB ---------------------------------------
class _HTTP:
    def __init__(self, url: str, user: Optional[str] = None, password: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None) -> None:
        self.url = url.rstrip("/")
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        if user is not None:
            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"

    def call(self, method: str, path: str, body: Any = None) -> tuple:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw) if raw else None
            except ValueError:
                return exc.code, None

    async def __call__(self, method: str, path: str, body: Any = None) -> tuple:
        return await asyncio.to_thread(self.call, method, path, body)


# -- Apache CouchDB / IBM Cloudant / PouchDB Server ----------------------------------------------
class CouchDBDocs(DocBackend):
    """One CouchDB database per collection (``{prefix}nm_bookings`` ...). Uses Mango queries."""

    def __init__(self, url: str = "http://localhost:5984", *, user: Optional[str] = None,
                 password: Optional[str] = None, prefix: str = "") -> None:
        self.http = _HTTP(url, user, password)
        self.prefix = prefix

    def _db(self, coll: str) -> str:
        return "/" + quote(self.prefix + coll, safe="")

    async def setup(self) -> None:
        for coll, fields in COLLECTIONS.items():
            await self.http("PUT", self._db(coll))  # 412 if it exists: fine
            for f in fields:
                await self.http("POST", self._db(coll) + "/_index", {"index": {"fields": [f]}, "name": f"nm_{f}"})
        await self.http("POST", self._db("nm_bookings") + "/_index",
                        {"index": {"fields": ["host_id", "start_ms"]}, "name": "nm_host_start"})

    @staticmethod
    def _clean(doc: Optional[Doc]) -> Optional[Doc]:
        if not doc:
            return None
        doc = dict(doc)
        doc.pop("_rev", None)
        return doc

    async def insert(self, coll: str, doc_id: str, doc: Doc) -> bool:
        status, _ = await self.http("PUT", f"{self._db(coll)}/{quote(doc_id, safe='')}", doc)
        return status in (200, 201, 202)

    async def upsert(self, coll: str, doc_id: str, doc: Doc) -> None:
        path = f"{self._db(coll)}/{quote(doc_id, safe='')}"
        for _ in range(5):
            status, cur = await self.http("GET", path)
            body = dict(doc)
            if status == 200 and cur:
                body["_rev"] = cur["_rev"]
            status, _ = await self.http("PUT", path, body)
            if status in (200, 201, 202):
                return
            if status != 409:
                raise RuntimeError(f"CouchDB PUT failed with {status}")
        raise RuntimeError("CouchDB update conflict")

    async def get(self, coll: str, doc_id: str) -> Optional[Doc]:
        status, doc = await self.http("GET", f"{self._db(coll)}/{quote(doc_id, safe='')}")
        return self._clean(doc) if status == 200 else None

    async def delete(self, coll: str, doc_id: str) -> None:
        path = f"{self._db(coll)}/{quote(doc_id, safe='')}"
        status, cur = await self.http("GET", path)
        if status == 200 and cur:
            await self.http("DELETE", f"{path}?rev={cur['_rev']}")

    async def delete_if(self, coll: str, doc_id: str, field: str, value: Any) -> bool:
        path = f"{self._db(coll)}/{quote(doc_id, safe='')}"
        status, cur = await self.http("GET", path)
        if status != 200 or not cur or cur.get(field) != value:
            return False
        status, _ = await self.http("DELETE", f"{path}?rev={cur['_rev']}")  # rev = compare-and-delete
        return status in (200, 202)

    async def find(self, coll: str, where: Optional[Doc] = None, *, range: Optional[Range] = None,
                   order_by: Optional[str] = None, desc: bool = False,
                   limit: Optional[int] = None) -> List[Doc]:
        selector: Doc = dict(where or {})
        if range:
            f, lo, hi = range
            cond = {}
            if lo is not None:
                cond["$gte"] = lo
            if hi is not None:
                cond["$lt"] = hi
            if cond:
                selector[f] = cond
        if order_by and order_by not in selector:
            selector[order_by] = {"$gt": None}
        body: Doc = {"selector": selector or {"_id": {"$gt": None}}, "limit": limit or 100000}
        if order_by:
            body["sort"] = [{order_by: "desc" if desc else "asc"}]
        status, res = await self.http("POST", self._db(coll) + "/_find", body)
        if status != 200 and order_by:  # no matching index: sort client-side
            body.pop("sort", None)
            status, res = await self.http("POST", self._db(coll) + "/_find", body)
            docs = [self._clean(d) for d in (res or {}).get("docs", [])]
            docs.sort(key=lambda d: d.get(order_by), reverse=desc)
            return docs[:limit] if limit else docs
        return [self._clean(d) for d in (res or {}).get("docs", [])]


# -- Google Cloud Firestore -----------------------------------------------------------------
class FirestoreDocs(DocBackend):
    """Needs composite indexes for (host_id, status, start_ms) and (room, ts); Firestore's
    error message links straight to their creation page."""

    def __init__(self, client: Any = None, *, prefix: str = "", **client_kwargs: Any) -> None:
        self.client, self.prefix, self.client_kwargs = client, prefix, client_kwargs

    async def setup(self) -> None:
        if self.client is None:
            fs = _need("google.cloud.firestore", "google-cloud-firestore")
            self.client = fs.AsyncClient(**self.client_kwargs)

    def _c(self, coll: str) -> Any:
        return self.client.collection(self.prefix + coll)

    async def insert(self, coll: str, doc_id: str, doc: Doc) -> bool:
        from google.api_core.exceptions import AlreadyExists
        try:
            await self._c(coll).document(doc_id).create(doc)
            return True
        except AlreadyExists:
            return False

    async def upsert(self, coll: str, doc_id: str, doc: Doc) -> None:
        await self._c(coll).document(doc_id).set(doc)

    async def get(self, coll: str, doc_id: str) -> Optional[Doc]:
        snap = await self._c(coll).document(doc_id).get()
        return {**snap.to_dict(), "_id": snap.id} if snap.exists else None

    async def delete(self, coll: str, doc_id: str) -> None:
        await self._c(coll).document(doc_id).delete()

    async def delete_if(self, coll: str, doc_id: str, field: str, value: Any) -> bool:
        fs = _need("google.cloud.firestore", "google-cloud-firestore")
        ref = self._c(coll).document(doc_id)
        transaction = self.client.transaction()

        @fs.async_transactional
        async def run(tx: Any) -> bool:
            snap = await ref.get(transaction=tx)
            if snap.exists and snap.to_dict().get(field) == value:
                tx.delete(ref)
                return True
            return False
        return await run(transaction)

    async def find(self, coll: str, where: Optional[Doc] = None, *, range: Optional[Range] = None,
                   order_by: Optional[str] = None, desc: bool = False,
                   limit: Optional[int] = None) -> List[Doc]:
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = self._c(coll)
        for k, v in (where or {}).items():
            q = q.where(filter=FieldFilter(k, "==", v))
        if range:
            f, lo, hi = range
            if lo is not None:
                q = q.where(filter=FieldFilter(f, ">=", lo))
            if hi is not None:
                q = q.where(filter=FieldFilter(f, "<", hi))
        if order_by:
            q = q.order_by(order_by, direction="DESCENDING" if desc else "ASCENDING")
        if limit:
            q = q.limit(limit)
        return [{**s.to_dict(), "_id": s.id} async for s in q.stream()]


# -- Elasticsearch / OpenSearch ---------------------------------------------------------------
class ElasticsearchDocs(DocBackend):
    """One index per collection; writes use ``refresh=wait_for`` so reads see them."""

    def __init__(self, url: str = "http://localhost:9200", *, user: Optional[str] = None,
                 password: Optional[str] = None, api_key: Optional[str] = None, prefix: str = "") -> None:
        headers = {"Authorization": f"ApiKey {api_key}"} if api_key else None
        self.http = _HTTP(url, user, password, headers)
        self.prefix = prefix

    def _i(self, coll: str) -> str:
        return "/" + quote((self.prefix + coll).lower(), safe="")

    async def setup(self) -> None:
        mapping = {"mappings": {"dynamic_templates": [{"strings": {"match_mapping_type": "string",
                                                                   "mapping": {"type": "keyword"}}}],
                                "properties": {"data": {"type": "object", "enabled": False}}}}
        for coll in COLLECTIONS:
            await self.http("PUT", self._i(coll), mapping)  # 400 if it exists: fine

    async def insert(self, coll: str, doc_id: str, doc: Doc) -> bool:
        status, _ = await self.http("PUT", f"{self._i(coll)}/_create/{quote(doc_id, safe='')}?refresh=wait_for", doc)
        return status in (200, 201)

    async def upsert(self, coll: str, doc_id: str, doc: Doc) -> None:
        status, res = await self.http("PUT", f"{self._i(coll)}/_doc/{quote(doc_id, safe='')}?refresh=wait_for", doc)
        if status not in (200, 201):
            raise RuntimeError(f"index failed: {status} {res}")

    async def get(self, coll: str, doc_id: str) -> Optional[Doc]:
        status, res = await self.http("GET", f"{self._i(coll)}/_doc/{quote(doc_id, safe='')}")
        return {**res["_source"], "_id": res["_id"]} if status == 200 and res.get("found") else None

    async def delete(self, coll: str, doc_id: str) -> None:
        await self.http("DELETE", f"{self._i(coll)}/_doc/{quote(doc_id, safe='')}?refresh=wait_for")

    async def delete_if(self, coll: str, doc_id: str, field: str, value: Any) -> bool:
        status, res = await self.http("GET", f"{self._i(coll)}/_doc/{quote(doc_id, safe='')}")
        if status != 200 or not res.get("found") or res["_source"].get(field) != value:
            return False
        path = (f"{self._i(coll)}/_doc/{quote(doc_id, safe='')}?refresh=wait_for"
                f"&if_seq_no={res['_seq_no']}&if_primary_term={res['_primary_term']}")
        status, _ = await self.http("DELETE", path)
        return status == 200

    async def find(self, coll: str, where: Optional[Doc] = None, *, range: Optional[Range] = None,
                   order_by: Optional[str] = None, desc: bool = False,
                   limit: Optional[int] = None) -> List[Doc]:
        filters: List[Doc] = [{"term": {k: v}} for k, v in (where or {}).items() if v is not None]
        must_not = [{"exists": {"field": k}} for k, v in (where or {}).items() if v is None]
        if range:
            f, lo, hi = range
            cond = {}
            if lo is not None:
                cond["gte"] = lo
            if hi is not None:
                cond["lt"] = hi
            if cond:
                filters.append({"range": {f: cond}})
        body: Doc = {"query": {"bool": {"filter": filters, "must_not": must_not}}, "size": limit or 10000}
        if order_by:
            body["sort"] = [{order_by: {"order": "desc" if desc else "asc"}}]
        status, res = await self.http("POST", f"{self._i(coll)}/_search", body)
        if status != 200:
            return []
        return [{**h["_source"], "_id": h["_id"]} for h in res["hits"]["hits"]]


# -- Neo4j (graph) -----------------------------------------------------------------------------
_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Neo4jDocs(DocBackend):
    """Documents become nodes labelled with the collection name; nested values are
    stored as JSON strings (Neo4j properties can't hold maps). Also works with
    Memgraph and other Bolt/Cypher databases."""

    def __init__(self, url: str = "bolt://localhost:7687", *, auth: Any = None, driver: Any = None,
                 database: Optional[str] = None, prefix: str = "") -> None:
        self.url, self.auth, self.driver, self.database = url, auth, driver, database
        self.p = prefix

    async def setup(self) -> None:
        if self.driver is None:
            self.driver = _need("neo4j", "neo4j").AsyncGraphDatabase.driver(self.url, auth=self.auth)
        for coll, fields in COLLECTIONS.items():
            coll = self._label(coll)
            await self._q(f"CREATE CONSTRAINT {coll}_id IF NOT EXISTS FOR (n:{coll}) REQUIRE n._id IS UNIQUE")
            for f in fields:
                await self._q(f"CREATE INDEX {coll}_{f} IF NOT EXISTS FOR (n:{coll}) ON (n.{f})")

    async def close(self) -> None:
        if self.driver is not None:
            await self.driver.close()

    async def _q(self, cypher: str, **params: Any) -> List[Doc]:
        async with self.driver.session(database=self.database) as s:
            res = await s.run(cypher, **params)
            return [r.data() async for r in res]

    def _label(self, coll: str) -> str:
        coll = self.p + coll
        if not _LABEL.match(coll):
            raise ValueError("bad collection name")
        return coll

    @staticmethod
    def _enc(doc: Doc) -> Doc:
        return {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in doc.items()}

    @staticmethod
    def _dec(props: Doc) -> Doc:
        out = dict(props)
        if isinstance(out.get("data"), str):
            out["data"] = json.loads(out["data"])
        return out

    async def insert(self, coll: str, doc_id: str, doc: Doc) -> bool:
        rows = await self._q(f"MERGE (n:{self._label(coll)} {{_id: $id}}) ON CREATE SET n += $props, n._new = true "
                             "WITH n, coalesce(n._new, false) AS created REMOVE n._new RETURN created",
                             id=doc_id, props=self._enc(doc))
        return bool(rows and rows[0]["created"])

    async def upsert(self, coll: str, doc_id: str, doc: Doc) -> None:
        await self._q(f"MERGE (n:{self._label(coll)} {{_id: $id}}) SET n = $props",
                      id=doc_id, props={**self._enc(doc), "_id": doc_id})

    async def get(self, coll: str, doc_id: str) -> Optional[Doc]:
        rows = await self._q(f"MATCH (n:{self._label(coll)} {{_id: $id}}) RETURN properties(n) AS p", id=doc_id)
        return self._dec(rows[0]["p"]) if rows else None

    async def delete(self, coll: str, doc_id: str) -> None:
        await self._q(f"MATCH (n:{self._label(coll)} {{_id: $id}}) DETACH DELETE n", id=doc_id)

    async def delete_if(self, coll: str, doc_id: str, field: str, value: Any) -> bool:
        if not _LABEL.match(field):
            raise ValueError("bad field name")
        rows = await self._q(f"MATCH (n:{self._label(coll)} {{_id: $id}}) WHERE n.{field} = $v "
                             "DETACH DELETE n RETURN count(*) AS c", id=doc_id, v=value)
        return bool(rows and rows[0]["c"])

    async def find(self, coll: str, where: Optional[Doc] = None, *, range: Optional[Range] = None,
                   order_by: Optional[str] = None, desc: bool = False,
                   limit: Optional[int] = None) -> List[Doc]:
        conds, params = [], {}
        for i, (k, v) in enumerate((where or {}).items()):
            if not _LABEL.match(k):
                raise ValueError("bad field name")
            conds.append(f"n.{k} IS NULL" if v is None else f"n.{k} = $w{i}")
            params[f"w{i}"] = v
        if range:
            f, lo, hi = range
            if lo is not None:
                conds.append(f"n.{f} >= $lo")
                params["lo"] = lo
            if hi is not None:
                conds.append(f"n.{f} < $hi")
                params["hi"] = hi
        q = f"MATCH (n:{self._label(coll)})" + (" WHERE " + " AND ".join(conds) if conds else "")
        q += " RETURN properties(n) AS p"
        if order_by:
            q += f" ORDER BY n.{order_by} {'DESC' if desc else 'ASC'}"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [self._dec(r["p"]) for r in await self._q(q, **params)]


# -- ArangoDB (multi-model: document + graph + key-value) ----------------------------------------
class ArangoDocs(DocBackend):
    def __init__(self, url: str = "http://localhost:8529", database: str = "_system", *,
                 user: str = "root", password: str = "", prefix: str = "") -> None:
        self.http = _HTTP(url, user, password)
        self.p = prefix
        self.base = f"/_db/{quote(database, safe='')}/_api"

    async def setup(self) -> None:
        for coll, fields in COLLECTIONS.items():
            coll = self.p + coll
            await self.http("POST", f"{self.base}/collection", {"name": coll})
            if fields:
                await self.http("POST", f"{self.base}/index?collection={coll}",
                                {"type": "persistent", "fields": fields})

    @staticmethod
    def _key(doc_id: str) -> str:  # Arango keys allow a limited charset
        return base64.urlsafe_b64encode(doc_id.encode()).decode().rstrip("=")

    @staticmethod
    def _out(doc: Optional[Doc]) -> Optional[Doc]:
        if not doc:
            return None
        doc = {k: v for k, v in doc.items() if k not in ("_key", "_rev", "_id")}
        doc["_id"] = doc.pop("nm_id", None)
        return doc

    async def insert(self, coll: str, doc_id: str, doc: Doc) -> bool:
        coll = self.p + coll
        status, _ = await self.http("POST", f"{self.base}/document/{coll}",
                                    {**doc, "_key": self._key(doc_id), "nm_id": doc_id})
        return status in (201, 202)

    async def upsert(self, coll: str, doc_id: str, doc: Doc) -> None:
        coll = self.p + coll
        await self.http("POST", f"{self.base}/document/{coll}?overwriteMode=replace",
                        {**doc, "_key": self._key(doc_id), "nm_id": doc_id})

    async def get(self, coll: str, doc_id: str) -> Optional[Doc]:
        coll = self.p + coll
        status, doc = await self.http("GET", f"{self.base}/document/{coll}/{self._key(doc_id)}")
        return self._out(doc) if status == 200 else None

    async def delete(self, coll: str, doc_id: str) -> None:
        coll = self.p + coll
        await self.http("DELETE", f"{self.base}/document/{coll}/{self._key(doc_id)}")

    async def find(self, coll: str, where: Optional[Doc] = None, *, range: Optional[Range] = None,
                   order_by: Optional[str] = None, desc: bool = False,
                   limit: Optional[int] = None) -> List[Doc]:
        coll = self.p + coll
        aql, bind = f"FOR d IN {coll}", {}
        for i, (k, v) in enumerate((where or {}).items()):
            aql += f" FILTER d.@f{i} == @v{i}"
            bind[f"f{i}"], bind[f"v{i}"] = k, v
        if range:
            f, lo, hi = range
            bind["rf"] = f
            if lo is not None:
                aql += " FILTER d.@rf >= @lo"
                bind["lo"] = lo
            if hi is not None:
                aql += " FILTER d.@rf < @hi"
                bind["hi"] = hi
        if order_by:
            aql += f" SORT d.@ob {'DESC' if desc else 'ASC'}"
            bind["ob"] = order_by
        if limit:
            aql += f" LIMIT {int(limit)}"
        status, res = await self.http("POST", f"{self.base}/cursor",
                                      {"query": aql + " RETURN d", "bindVars": bind, "batchSize": 1000})
        docs = list((res or {}).get("result", []))
        while res and res.get("hasMore"):
            status, res = await self.http("POST", f"{self.base}/cursor/{res['id']}")
            docs += (res or {}).get("result", [])
        return [self._out(d) for d in docs]
