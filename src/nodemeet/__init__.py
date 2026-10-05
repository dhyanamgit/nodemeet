"""nodemeet: embeddable video meetings + scheduling for Python apps (PolyForm Noncommercial 1.0.0).

Quick start::

    from nodemeet import NodeMeet
    meet = NodeMeet()                       # db="meet.db", email="smtp://...", secret=... when ready
    print(meet.room("standup").host_link("Ada"))
    meet.run()

Forgot something? ``nodemeet.help()`` prints the one-page cheat sheet.

Nothing here imports aiohttp at import time: use ``meet.asgi()`` with
FastAPI/Starlette/Django, ``meet.mount()`` with aiohttp, or just the pure
Python parts (tokens, scheduling, storage, email, webhooks).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._version import __version__
from .exceptions import (AvailabilityNotFound, BookingNotFound, ExpiredToken, InvalidToken,
                         JoinRejected, NodeMeetError, RoomFull, RoomNotFound, SFUUnavailable,
                         SlotUnavailable)
from .hooks import Hooks, JoinContext
from .integrations import (EmailContext, EmailMessage, EmailTemplates, LoggingMailer, Mailer,
                           MemoryMailer, SMTPMailer, WebhookDispatcher, WebhookEndpoint,
                           sign_payload, verify_signature)
from .models import Booking, BookingStatus, ChatMessage, RoomConfig
from .rooms import Participant, Room, RoomManager
from .scheduling import (Availability, BookingService, ReminderScheduler, Slot, build_ics,
                         calendar_links, find_slots, google_calendar_link, outlook_calendar_link)
from .storage import MemoryStorage, SQLiteStorage, Storage
from .tokens import ROLE_PERMISSIONS, Role, TokenClaims, TokenSigner
from .topology import select_topology
from .broker import Broker, LockTimeout, MemoryBroker, MemoryHub, RedisBroker
from .tenancy import SCOPES, ApiKey, Principal
from .storage import SyncStorageAdapter
from .server import NodeMeet, create_app, create_asgi
from .sfu import AiortcSFU, MediaBackend, NullBackend

if TYPE_CHECKING:  # pragma: no cover
    from .storage.sql import SQLAlchemyStorage

_LAZY = {"SQLAlchemyStorage": "storage.sql"}


def __getattr__(name: str) -> Any:  # PEP 562: import aiohttp-backed parts lazily
    if name in _LAZY:
        import importlib
        module = importlib.import_module(f".{_LAZY[name]}", __name__)
        return getattr(module, name)
    raise AttributeError(f"module 'nodemeet' has no attribute {name!r}")


__all__ = [
    "__version__", "NodeMeet", "create_app", "create_asgi", "AiortcSFU", "MediaBackend",
    "NullBackend", "Broker", "MemoryBroker", "MemoryHub", "RedisBroker", "LockTimeout",
    "ApiKey", "Principal", "SCOPES", "SyncStorageAdapter", "SQLAlchemyStorage",
    "TokenSigner", "TokenClaims", "Role", "ROLE_PERMISSIONS", "Hooks", "JoinContext",
    "Room", "RoomManager", "Participant", "RoomConfig", "ChatMessage", "select_topology",
    "Availability", "Slot", "find_slots", "BookingService", "ReminderScheduler", "Booking",
    "BookingStatus", "build_ics", "calendar_links", "google_calendar_link",
    "outlook_calendar_link", "Storage", "MemoryStorage", "SQLiteStorage", "Mailer",
    "SMTPMailer", "MemoryMailer", "LoggingMailer", "EmailMessage", "EmailTemplates",
    "EmailContext", "WebhookDispatcher", "WebhookEndpoint", "sign_payload", "verify_signature",
    "NodeMeetError", "InvalidToken", "ExpiredToken", "JoinRejected", "RoomNotFound", "RoomFull",
    "SlotUnavailable", "BookingNotFound", "AvailabilityNotFound", "SFUUnavailable",
]

# v0.3: roles, permissions and branding
from .permissions import PERMISSIONS, RoleDefinition, RoleRegistry  # noqa: E402
from .branding import BrandingResolver  # noqa: E402

# v0.5: the easy layer
from .quick import BookingPage, RoomHandle, load_dotenv, quickstart  # noqa: E402
from .easy import PRESETS, mailer_from_url, storage_from_url  # noqa: E402


def help() -> None:  # noqa: A001 - deliberate: `nodemeet.help()`
    """Print the one-page cheat sheet."""
    from .quick import CHEATSHEET
    print(CHEATSHEET)


__all__ += ["PERMISSIONS", "RoleDefinition", "RoleRegistry", "BrandingResolver", "quickstart",
            "RoomHandle", "BookingPage", "load_dotenv", "PRESETS", "storage_from_url",
            "mailer_from_url", "help"]
