"""Email: an SMTP mailer (stdlib only) plus overridable booking templates."""
from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage as _MIMEMessage
from email.utils import formataddr, make_msgid
from typing import List, Optional, Protocol, Tuple, Union, runtime_checkable

log = logging.getLogger("nodemeet.email")

Attachment = Tuple[str, Union[str, bytes], str]  # (filename, content, "type/subtype")


@dataclass
class EmailMessage:
    to: List[str]
    subject: str
    text: str
    html: Optional[str] = None
    attachments: List[Attachment] = field(default_factory=list)
    reply_to: Optional[str] = None
    calendar_method: Optional[str] = None  # set to add an inline text/calendar part


@runtime_checkable
class Mailer(Protocol):
    async def send(self, message: EmailMessage) -> None: ...


class MemoryMailer:
    """Collects messages in ``self.outbox``. Use in tests or during development."""

    def __init__(self) -> None:
        self.outbox: List[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.outbox.append(message)


class LoggingMailer:
    """Logs emails instead of sending them."""

    async def send(self, message: EmailMessage) -> None:
        log.info("EMAIL to=%s subject=%r\n%s", message.to, message.subject, message.text)


class SMTPMailer:
    """Send mail through any SMTP server (Postfix, your provider, Gmail SMTP...).

    ``security``: ``"starttls"`` (port 587, default), ``"ssl"`` (port 465) or ``"none"``.
    """

    def __init__(self, host: str, port: int = 587, *, username: Optional[str] = None,
                 password: Optional[str] = None, sender: str = "nodemeet@localhost",
                 sender_name: str = "", security: str = "starttls", timeout: float = 20.0,
                 ssl_context: Optional[ssl.SSLContext] = None) -> None:
        if security not in ("starttls", "ssl", "none"):
            raise ValueError("security must be starttls, ssl or none")
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.sender, self.sender_name = sender, sender_name
        self.security, self.timeout = security, timeout
        self.ssl_context = ssl_context or ssl.create_default_context()

    def build(self, message: EmailMessage) -> _MIMEMessage:
        msg = _MIMEMessage()
        msg["From"] = formataddr((self.sender_name, self.sender)) if self.sender_name else self.sender
        msg["To"] = ", ".join(message.to)
        msg["Subject"] = message.subject
        msg["Message-ID"] = make_msgid(domain=self.sender.split("@")[-1] or "nodemeet")
        if message.reply_to:
            msg["Reply-To"] = message.reply_to
        msg.set_content(message.text)
        if message.html:
            msg.add_alternative(message.html, subtype="html")
        for filename, content, mimetype in message.attachments:
            maintype, subtype = mimetype.split("/", 1)
            data = content.encode("utf-8") if isinstance(content, str) else content
            params = {}
            if maintype == "text" and subtype == "calendar" and message.calendar_method:
                params["method"] = message.calendar_method
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename,
                               params=params)
        return msg

    def _send_sync(self, msg: _MIMEMessage) -> None:
        if self.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout,
                                                   context=self.ssl_context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        try:
            server.ehlo()
            if self.security == "starttls":
                server.starttls(context=self.ssl_context)
                server.ehlo()
            if self.username:
                server.login(self.username, self.password or "")
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass

    async def send(self, message: EmailMessage) -> None:
        await asyncio.to_thread(self._send_sync, self.build(message))
