"""Media backends.

The default :class:`AiortcSFU` is a small selective forwarding unit built on
aiortc (``pip install "nodemeet[sfu]"``). aiortc relays decoded frames, so it
is CPU-bound: comfortable for rooms of ~5-25 people on a modern server. For
very large rooms, implement :class:`MediaBackend` on top of an external
open-source SFU (mediasoup, Janus, LiveKit OSS, ...) and pass it as
``NodeMeet(sfu=MyBackend())``.

Protocol: each publisher sends one ``sendonly`` offer; each subscriber opens
one ``recvonly`` connection per publisher it wants to watch. No server-side
renegotiation is needed, which keeps the SFU simple and robust.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("nodemeet.sfu")

try:  # optional dependency
    from aiortc import (RTCConfiguration, RTCIceServer, RTCPeerConnection,
                        RTCSessionDescription)
    from aiortc.contrib.media import MediaBlackhole, MediaRelay
    AIORTC_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    AIORTC_AVAILABLE = False

SDP = Dict[str, str]  # {"sdp": "...", "type": "offer"|"answer"}


class MediaBackend:
    """Interface for server-side media. The base class is a no-op (mesh only)."""

    available = False

    async def publish(self, room_id: str, peer_id: str, offer: SDP) -> SDP:
        raise NotImplementedError

    async def subscribe(self, room_id: str, subscriber_id: str, publisher_id: str,
                        offer: SDP, *, kinds: Optional[set] = None) -> SDP:
        """``kinds`` = media the subscriber may receive ({"audio", "video"})."""
        raise NotImplementedError

    async def unsubscribe(self, room_id: str, subscriber_id: str, publisher_id: str) -> None:
        return None

    async def unpublish(self, room_id: str, peer_id: str) -> None:
        return None

    def publishers(self, room_id: str) -> List[str]:
        return []

    async def remove_peer(self, room_id: str, peer_id: str) -> None:
        return None

    async def remove_room(self, room_id: str) -> None:
        return None

    async def close(self) -> None:
        return None


class NullBackend(MediaBackend):
    """Used when SFU is disabled or aiortc is missing: rooms stay peer-to-peer."""


@dataclass
class _Publisher:
    pc: Any
    tracks: Dict[str, Any] = field(default_factory=dict)
    sinks: List[Any] = field(default_factory=list)


class AiortcSFU(MediaBackend):
    available = AIORTC_AVAILABLE

    def __init__(self, ice_servers: Optional[List[Dict[str, Any]]] = None) -> None:
        if not AIORTC_AVAILABLE:
            from .exceptions import SFUUnavailable
            raise SFUUnavailable('install the SFU extra: pip install "nodemeet[sfu]"')
        servers = []
        for s in ice_servers or []:
            servers.append(RTCIceServer(urls=s["urls"], username=s.get("username"),
                                        credential=s.get("credential")))
        self._config = RTCConfiguration(iceServers=servers)
        self._relay = MediaRelay()
        self._pubs: Dict[Tuple[str, str], _Publisher] = {}
        self._subs: Dict[Tuple[str, str, str], Any] = {}

    @staticmethod
    def _desc(offer: SDP) -> "RTCSessionDescription":
        return RTCSessionDescription(sdp=offer["sdp"], type=offer.get("type", "offer"))

    async def publish(self, room_id: str, peer_id: str, offer: SDP) -> SDP:
        await self.unpublish(room_id, peer_id)
        pc = RTCPeerConnection(configuration=self._config)
        pub = _Publisher(pc=pc)
        self._pubs[(room_id, peer_id)] = pub

        @pc.on("track")
        def on_track(track: Any) -> None:
            pub.tracks[track.kind] = track
            # Drain the track so aiortc's receive queue never grows unbounded.
            sink = MediaBlackhole()
            sink.addTrack(self._relay.subscribe(track))
            pub.sinks.append(sink)
            asyncio.ensure_future(sink.start())

        @pc.on("connectionstatechange")
        async def on_state() -> None:
            if pc.connectionState in ("failed", "closed"):
                log.debug("publisher %s/%s %s", room_id, peer_id, pc.connectionState)

        await pc.setRemoteDescription(self._desc(offer))
        await pc.setLocalDescription(await pc.createAnswer())
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    async def subscribe(self, room_id: str, subscriber_id: str, publisher_id: str,
                        offer: SDP, *, kinds: Optional[set] = None) -> SDP:
        pub = self._pubs.get((room_id, publisher_id))
        if pub is None:
            raise ValueError(f"{publisher_id} is not publishing")
        await self.unsubscribe(room_id, subscriber_id, publisher_id)
        pc = RTCPeerConnection(configuration=self._config)
        self._subs[(room_id, subscriber_id, publisher_id)] = pc
        await pc.setRemoteDescription(self._desc(offer))
        for kind in ("audio", "video"):
            if kinds is not None and kind not in kinds:
                continue
            track = pub.tracks.get(kind)
            if track is not None:
                pc.addTrack(self._relay.subscribe(track))
        await pc.setLocalDescription(await pc.createAnswer())
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    async def unsubscribe(self, room_id: str, subscriber_id: str, publisher_id: str) -> None:
        pc = self._subs.pop((room_id, subscriber_id, publisher_id), None)
        if pc is not None:
            await pc.close()

    async def unpublish(self, room_id: str, peer_id: str) -> None:
        pub = self._pubs.pop((room_id, peer_id), None)
        if pub is None:
            return
        for key in [k for k in self._subs if k[0] == room_id and k[2] == peer_id]:
            await self._subs.pop(key).close()
        for sink in pub.sinks:
            await sink.stop()
        await pub.pc.close()

    def publishers(self, room_id: str) -> List[str]:
        return [peer for (room, peer) in self._pubs if room == room_id]

    async def remove_peer(self, room_id: str, peer_id: str) -> None:
        await self.unpublish(room_id, peer_id)
        for key in [k for k in self._subs if k[0] == room_id and k[1] == peer_id]:
            await self._subs.pop(key).close()

    async def remove_room(self, room_id: str) -> None:
        for room, peer in [k for k in self._pubs if k[0] == room_id]:
            await self.unpublish(room, peer)
        for key in [k for k in self._subs if k[0] == room_id]:
            await self._subs.pop(key).close()

    async def close(self) -> None:
        for room in {k[0] for k in self._pubs} | {k[0] for k in self._subs}:
            await self.remove_room(room)


def default_backend(ice_servers: Optional[List[Dict[str, Any]]] = None) -> MediaBackend:
    if AIORTC_AVAILABLE:
        return AiortcSFU(ice_servers)
    log.info("aiortc not installed: rooms will use peer-to-peer mesh only")
    return NullBackend()
