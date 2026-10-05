"""Meeting recordings.

Browser recording (default): the person who presses *Record* composites every
tile onto a canvas, mixes all audio with WebAudio and uploads WebM chunks here.
Everyone sees a REC indicator. Files land in a :class:`RecordingStore` (local
folder by default, S3-compatible optional) and a ``recording.ready`` webhook fires.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional

if TYPE_CHECKING:
    from .rooms import Participant, Room
    from .server import NodeMeet

MAX_CHUNK = 25 * 1024 * 1024


class RecordingStore:
    async def append(self, rec_id: str, seq: int, data: bytes) -> None: raise NotImplementedError
    async def finalize(self, rec_id: str) -> int:
        """Join the chunks into one file; returns its size in bytes."""
        raise NotImplementedError
    async def read(self, rec_id: str) -> bytes: raise NotImplementedError
    async def delete(self, rec_id: str) -> None: raise NotImplementedError

    def download_url(self, rec_id: str) -> Optional[str]:
        """A direct URL (e.g. presigned S3) if the store has one; else nodemeet serves it."""
        return None


class FileRecordingStore(RecordingStore):
    def __init__(self, root: str = "recordings") -> None:
        self.root = root

    def _dir(self, rec_id: str) -> str:
        if not rec_id.replace("_", "").isalnum():
            raise ValueError("bad recording id")
        return os.path.join(self.root, rec_id + ".parts")

    def path(self, rec_id: str) -> str:
        return os.path.join(self.root, rec_id + ".webm")

    async def append(self, rec_id: str, seq: int, data: bytes) -> None:
        def op() -> None:
            d = self._dir(rec_id)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"{int(seq):08d}.part"), "wb") as f:
                f.write(data)
        await asyncio.to_thread(op)

    async def finalize(self, rec_id: str) -> int:
        def op() -> int:
            d = self._dir(rec_id)
            parts = sorted(os.listdir(d)) if os.path.isdir(d) else []
            with open(self.path(rec_id), "wb") as out:
                for name in parts:
                    with open(os.path.join(d, name), "rb") as f:
                        out.write(f.read())
            for name in parts:
                os.remove(os.path.join(d, name))
            if os.path.isdir(d):
                os.rmdir(d)
            return os.path.getsize(self.path(rec_id))
        return await asyncio.to_thread(op)

    async def read(self, rec_id: str) -> bytes:
        return await asyncio.to_thread(lambda: open(self.path(rec_id), "rb").read())

    def iter_file(self, rec_id: str, size: int = 1 << 20) -> Iterator[bytes]:
        with open(self.path(rec_id), "rb") as f:
            while chunk := f.read(size):
                yield chunk

    async def delete(self, rec_id: str) -> None:
        def op() -> None:
            for p in (self.path(rec_id),):
                if os.path.exists(p):
                    os.remove(p)
        await asyncio.to_thread(op)


class S3RecordingStore(RecordingStore):
    """S3 / MinIO / R2: chunks are uploaded as parts, then composed into one object."""

    def __init__(self, bucket: str, *, prefix: str = "recordings/", client: Any = None,
                 url_ttl: int = 3600, **client_kwargs: Any) -> None:
        self.bucket, self.prefix, self.url_ttl = bucket, prefix, url_ttl
        if client is None:
            import boto3
            client = boto3.client("s3", **client_kwargs)
        self.s3 = client

    async def append(self, rec_id: str, seq: int, data: bytes) -> None:
        await asyncio.to_thread(self.s3.put_object, Bucket=self.bucket,
                                Key=f"{self.prefix}{rec_id}.parts/{int(seq):08d}", Body=data)

    async def finalize(self, rec_id: str) -> int:
        def op() -> int:
            keys = sorted(o["Key"] for page in self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=f"{self.prefix}{rec_id}.parts/") for o in page.get("Contents", []))
            body = b"".join(self.s3.get_object(Bucket=self.bucket, Key=k)["Body"].read() for k in keys)
            self.s3.put_object(Bucket=self.bucket, Key=f"{self.prefix}{rec_id}.webm", Body=body,
                               ContentType="video/webm")
            for k in keys:
                self.s3.delete_object(Bucket=self.bucket, Key=k)
            return len(body)
        return await asyncio.to_thread(op)

    async def read(self, rec_id: str) -> bytes:
        return await asyncio.to_thread(lambda: self.s3.get_object(
            Bucket=self.bucket, Key=f"{self.prefix}{rec_id}.webm")["Body"].read())

    async def delete(self, rec_id: str) -> None:
        await asyncio.to_thread(self.s3.delete_object, Bucket=self.bucket, Key=f"{self.prefix}{rec_id}.webm")

    def download_url(self, rec_id: str) -> Optional[str]:
        return self.s3.generate_presigned_url("get_object", Params={
            "Bucket": self.bucket, "Key": f"{self.prefix}{rec_id}.webm"}, ExpiresIn=self.url_ttl)


class RecordingService:
    def __init__(self, meet: "NodeMeet", store: Optional[RecordingStore] = None,
                 finalize_after: float = 90.0) -> None:
        self.meet = meet
        self.store = store or FileRecordingStore()
        self.finalize_after = finalize_after
        self._pending: Dict[str, asyncio.Task] = {}

    async def begin(self, room: "Room", by: "Participant") -> Dict[str, Any]:
        rec = {"id": "rec_" + secrets.token_hex(8), "room": room.id, "tenant_id": room.config.tenant_id,
               "by": by.name, "by_user": by.user_id, "started_at": time.time(), "status": "recording",
               "chunks": 0, "bytes": 0, "mime": "video/webm"}
        await self.meet.storage.put_record("recording", rec["id"], rec)
        await self.meet.emit_webhook("recording.started", rec, tenant_id=rec["tenant_id"])
        return rec

    async def get(self, rec_id: str) -> Optional[Dict[str, Any]]:
        return await self.meet.storage.get_record("recording", rec_id)

    async def list(self, room: Optional[str] = None) -> List[Dict[str, Any]]:
        recs = [r for _, r in await self.meet.storage.list_records("recording")]
        return sorted([r for r in recs if room is None or r["room"] == room],
                      key=lambda r: r["started_at"], reverse=True)

    async def add_chunk(self, rec_id: str, seq: int, data: bytes, *, final: bool = False) -> Dict[str, Any]:
        rec = await self.get(rec_id)
        if rec is None or rec["status"] not in ("recording", "stopping"):
            raise ValueError("recording is not accepting data")
        if len(data) > MAX_CHUNK:
            raise ValueError("chunk too large")
        await self.store.append(rec_id, seq, data)
        rec["chunks"] += 1
        rec["bytes"] += len(data)
        await self.meet.storage.put_record("recording", rec_id, rec)
        if final:
            return await self.finalize(rec_id)
        return rec

    async def stop(self, rec_id: str) -> None:
        rec = await self.get(rec_id)
        if rec is None or rec["status"] != "recording":
            return
        rec["status"], rec["stopped_at"] = "stopping", time.time()
        await self.meet.storage.put_record("recording", rec_id, rec)

        async def later() -> None:  # the browser normally sends its final chunk first
            await asyncio.sleep(self.finalize_after)
            await self.finalize(rec_id)
        self._pending[rec_id] = asyncio.ensure_future(later())

    async def finalize(self, rec_id: str) -> Dict[str, Any]:
        rec = await self.get(rec_id)
        if rec is None or rec["status"] == "ready":
            return rec or {}
        task = self._pending.pop(rec_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        rec["size"] = await self.store.finalize(rec_id)
        rec["status"], rec["ready_at"] = "ready", time.time()
        rec["duration_seconds"] = round(rec["ready_at"] - rec["started_at"])
        await self.meet.storage.put_record("recording", rec_id, rec)
        await self.meet.emit_webhook("recording.ready", rec, tenant_id=rec.get("tenant_id"))
        return rec

    async def delete(self, rec_id: str) -> None:
        await self.store.delete(rec_id)
        await self.meet.storage.delete_record("recording", rec_id)
