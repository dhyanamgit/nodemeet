"""REST endpoints for recordings, captions/transcripts and other browser-facing uploads.

Browsers authenticate with their **join token** (``X-Join-Token`` header or
``?token=``); servers use the API key as everywhere else.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, Set, Tuple

from ._http import HTTPError, Request, Response, Router, json_response, text_response
from .tokens import TokenClaims

if TYPE_CHECKING:
    from .server import NodeMeet


class ExtrasAPI:
    def __init__(self, meet: "NodeMeet") -> None:
        self.meet = meet

    def register(self, r: Router) -> None:
        r.add("GET", "/api/recordings", self.list_recordings)
        r.add("GET", "/api/recordings/{rec_id}", self.get_recording)
        r.add("POST", "/api/recordings/{rec_id}/chunks", self.upload_chunk)
        r.add("GET", "/api/recordings/{rec_id}/download", self.download)
        r.add("DELETE", "/api/recordings/{rec_id}", self.delete_recording)
        r.add("POST", "/api/rooms/{room_id}/captions/audio", self.caption_audio)
        r.add("GET", "/api/rooms/{room_id}/transcript", self.transcript)

    # -- who is calling ---------------------------------------------------------------
    async def participant(self, req: Request, room_id: str) -> Tuple[TokenClaims, Set[str]]:
        """Verify a join token for ``room_id`` and return its *current* permissions."""
        token = req.header("x-join-token") or req.query.get("token", "")
        if not token:
            raise HTTPError(401, "unauthorized", "a join token is required")
        try:
            claims = self.meet.tokens.verify(token, room=room_id)
        except Exception:  # noqa: BLE001
            raise HTTPError(401, "unauthorized", "invalid join token") from None
        live = self.meet.rooms.get(room_id)
        if live is not None:
            for p in live.participants.values():
                if p.user_id == claims.user_id:
                    return claims, set(p.permissions)
        cfg = await self.meet.storage.get_room(room_id)
        role = await self.meet.roles.resolve(claims.role, cfg.tenant_id if cfg else None,
                                             room_overrides=cfg.role_overrides if cfg else None,
                                             grant=claims.grant, revoke=claims.revoke)
        return claims, set(role.permissions)

    async def _admin_or(self, req: Request, room_id: str, perm: str, scope: str) -> Optional[TokenClaims]:
        if self.meet.api._given_key(req):
            p = await self.meet.api.require(req, scope)
            cfg = await self.meet.storage.get_room(room_id)
            if cfg is not None and not p.owns(cfg.tenant_id):
                raise HTTPError(404, "not_found")
            return None
        claims, perms = await self.participant(req, room_id)
        if perm not in perms:
            raise HTTPError(403, "forbidden", f"your role does not allow: {perm}")
        return claims

    async def _rec(self, req: Request) -> Any:
        rec = await self.meet.recordings.get(req.match_info["rec_id"])
        if rec is None:
            raise HTTPError(404, "not_found", "no such recording")
        return rec

    # -- recordings -------------------------------------------------------------------------
    async def list_recordings(self, req: Request) -> Response:
        p = await self.meet.api.require(req, "rooms:read")
        recs = [r for r in await self.meet.recordings.list(req.query.get("room")) if p.owns(r.get("tenant_id"))]
        return json_response({"recordings": recs})

    async def get_recording(self, req: Request) -> Response:
        rec = await self._rec(req)
        await self._admin_or(req, rec["room"], "recording.download", "rooms:read")
        return json_response(rec)

    async def upload_chunk(self, req: Request) -> Response:
        rec = await self._rec(req)
        claims, perms = await self.participant(req, rec["room"])
        if claims.user_id != rec["by_user"] and "recording.start" not in perms:
            raise HTTPError(403, "forbidden", "only the recorder can upload")
        try:
            seq = int(req.query.get("seq", "0"))
            out = await self.meet.recordings.add_chunk(rec["id"], seq, req.body,
                                                       final=req.query.get("final") in ("1", "true"))
        except ValueError as exc:
            raise HTTPError(409, "not_recording", str(exc)) from None
        return json_response({"id": out["id"], "status": out["status"], "bytes": out.get("bytes")})

    async def download(self, req: Request) -> Response:
        rec = await self._rec(req)
        await self._admin_or(req, rec["room"], "recording.download", "rooms:read")
        if rec["status"] != "ready":
            raise HTTPError(409, "not_ready", "the recording is still being processed")
        url = self.meet.recordings.store.download_url(rec["id"])
        if url:
            return Response(302, b"", "text/plain", {"Location": url})
        data = await self.meet.recordings.store.read(rec["id"])
        return Response(200, data, rec.get("mime", "video/webm"),
                        {"Content-Disposition": f'attachment; filename="{rec["room"]}-{rec["id"]}.webm"'})

    async def delete_recording(self, req: Request) -> Response:
        rec = await self._rec(req)
        p = await self.meet.api.require(req, "rooms:write")
        if not p.owns(rec.get("tenant_id")):
            raise HTTPError(404, "not_found")
        await self.meet.recordings.delete(rec["id"])
        return Response(status=204)

    # -- captions & transcripts -------------------------------------------------------------
    async def caption_audio(self, req: Request) -> Response:
        room_id = req.match_info["room_id"]
        claims, perms = await self.participant(req, room_id)
        room = self.meet.rooms.get(room_id)
        me = next((p for p in room.participants.values() if p.user_id == claims.user_id), None) if room else None
        if me is None:
            raise HTTPError(409, "not_in_room", "join the meeting first")
        if self.meet.captions.transcriber is None:
            raise HTTPError(501, "no_transcriber", "server captions are not configured")
        text = await self.meet.captions.audio_chunk(room, me, req.body, req.header("content-type", "audio/webm"))
        return json_response({"text": text or ""})

    async def transcript(self, req: Request) -> Response:
        room_id = req.match_info["room_id"]
        await self._admin_or(req, room_id, "transcript.download", "rooms:read")
        lines = await self.meet.captions.transcript(room_id)
        fmt = req.query.get("format", "json")
        if fmt == "json":
            return json_response({"room": room_id, "lines": lines})
        body = self.meet.captions.render(lines, "vtt" if fmt == "vtt" else "txt")
        return text_response(body, "text/vtt" if fmt == "vtt" else "text/plain", headers={
            "Content-Disposition": f'attachment; filename="{room_id}-transcript.{"vtt" if fmt == "vtt" else "txt"}"'})
