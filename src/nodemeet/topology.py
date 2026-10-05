"""Media topology selection.

* ``p2p``     -- full mesh WebRTC between browsers. Zero server media load.
* ``sfu``     -- every browser sends once to the server (aiortc), which relays.
* ``auto``    -- mesh for small rooms (<= ``p2p_max``, default 4), SFU above.
                 Drops back to mesh only when the room shrinks to half of
                 ``p2p_max`` to avoid flapping.
* ``webinar`` -- only hosts (or promoted users) publish; everyone else watches.
                 Uses the SFU when available, otherwise one-way mesh.
"""
from __future__ import annotations

from typing import Optional

P2P = "p2p"
SFU = "sfu"
DEFAULT_P2P_MAX = 4


def select_topology(mode: str, participants: int, current: Optional[str] = None, *,
                    p2p_max: int = DEFAULT_P2P_MAX, sfu_available: bool = True) -> str:
    if mode == "p2p" or not sfu_available:
        return P2P
    if mode in ("sfu", "webinar"):
        return SFU
    if mode != "auto":
        raise ValueError(f"unknown mode {mode!r}")
    if participants > p2p_max:
        return SFU
    if current == SFU and participants > max(1, p2p_max // 2):
        return SFU
    return P2P
