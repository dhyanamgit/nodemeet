"""Live captions and transcripts.

Two engines:

* ``browser`` (default, nothing to install): each speaker's browser runs the Web
  Speech API and sends text; the server relays it and saves the transcript.
* ``server``: browsers send short audio chunks; a :class:`Transcriber` turns them
  into text. :class:`WhisperTranscriber` runs OpenAI's open-source Whisper locally
  (``pip install faster-whisper`` or ``openai-whisper``) - free, private, no API key.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from .rooms import Participant, Room
    from .server import NodeMeet


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, mime: str = "audio/webm",
                         language: Optional[str] = None) -> str: ...


class WhisperTranscriber:
    """Local Whisper. Uses faster-whisper when installed, else openai-whisper."""

    def __init__(self, model: str = "base", *, device: str = "auto", compute_type: str = "int8") -> None:
        self.model_name, self.device, self.compute_type = model, device, compute_type
        self._model: Any = None
        self._kind = ""
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
            self._kind = "faster"
        except ImportError:
            try:
                import whisper
            except ImportError:
                raise RuntimeError("install faster-whisper or openai-whisper for server captions") from None
            self._model = whisper.load_model(self.model_name)
            self._kind = "openai"

    def _run(self, audio: bytes, mime: str, language: Optional[str]) -> str:
        suffix = ".ogg" if "ogg" in mime else ".wav" if "wav" in mime else ".webm"
        with self._lock:
            self._load()
            fd, path = tempfile.mkstemp(suffix=suffix)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(audio)
                if self._kind == "faster":
                    segments, _ = self._model.transcribe(path, language=language, vad_filter=True)
                    return " ".join(s.text.strip() for s in segments).strip()
                return str(self._model.transcribe(path, language=language)["text"]).strip()
            finally:
                os.remove(path)

    async def transcribe(self, audio: bytes, mime: str = "audio/webm", language: Optional[str] = None) -> str:
        return await asyncio.to_thread(self._run, audio, mime, language)


class CaptionService:
    def __init__(self, meet: "NodeMeet", transcriber: Optional[Transcriber] = None) -> None:
        self.meet = meet
        self.transcriber = transcriber
        self._busy: Dict[str, float] = {}

    async def publish(self, room: "Room", p: "Participant", text: str, *, final: bool = True) -> None:
        line = {"id": "cap_" + secrets.token_hex(5), "peer_id": p.peer_id, "user_id": p.user_id,
                "name": p.name, "text": text, "final": final, "ts": time.time()}
        await room.broadcast({"type": "caption", **{k: v for k, v in line.items() if k != "user_id"}})
        if final:
            await self.meet.storage.put_record("transcript", f"{room.id}|{line['ts']:017.6f}|{line['id']}",
                                               {**line, "room": room.id})

    async def audio_chunk(self, room: "Room", p: "Participant", audio: bytes, mime: str) -> Optional[str]:
        if self.transcriber is None:
            raise RuntimeError("no transcriber configured")
        if not room.captions.get("enabled"):
            return None
        text = await self.transcriber.transcribe(audio, mime, room.captions.get("language"))
        if text:
            await self.publish(room, p, text, final=True)
        return text

    async def transcript(self, room_id: str) -> List[Dict[str, Any]]:
        return [v for _, v in await self.meet.storage.list_records("transcript", room_id + "|")]

    @staticmethod
    def render(lines: List[Dict[str, Any]], fmt: str = "txt") -> str:
        if fmt == "vtt":
            if not lines:
                return "WEBVTT\n"
            t0 = lines[0]["ts"]

            def ts(sec: float) -> str:
                h, rem = divmod(max(0.0, sec), 3600)
                m, s = divmod(rem, 60)
                return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"
            out = ["WEBVTT", ""]
            for i, line in enumerate(lines):
                start = line["ts"] - t0
                end = (lines[i + 1]["ts"] - t0) if i + 1 < len(lines) else start + 4
                out += [f"{ts(start)} --> {ts(max(end, start + 1))}", f"<v {line['name']}>{line['text']}", ""]
            return "\n".join(out)
        return "\n".join(time.strftime("%H:%M:%S", time.gmtime(line["ts"])) + f"  {line['name']}: {line['text']}"
                         for line in lines) + ("\n" if lines else "")
