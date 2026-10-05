"""Post meeting & booking events to team chat: Slack, Discord, Microsoft Teams,
Google Chat, Telegram, Mattermost / Rocket.Chat (Slack-compatible) or any webhook.

    meet.add_chat("slack", "https://hooks.slack.com/services/...")
    meet.add_chat("discord", "https://discord.com/api/webhooks/...", events=["booking.*"])
"""
from __future__ import annotations

import fnmatch
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional

from .http_client import HTTPClient
from .messages import describe

if TYPE_CHECKING:
    from ..server import NodeMeet

log = logging.getLogger("nodemeet.chat")
DEFAULT_EVENTS = ("booking.created", "booking.rescheduled", "booking.cancelled", "booking.paid",
                  "participant.blocked_attempt", "recording.ready")
Message = Dict[str, Any]


class ChatChannel:
    name = "chat"

    def __init__(self, http: Optional[HTTPClient] = None) -> None:
        self.http = http or HTTPClient()

    async def send(self, msg: Message) -> None:
        raise NotImplementedError


class SlackChat(ChatChannel):
    """Incoming webhook URL, or a bot token + channel (chat.postMessage). Mattermost and
    Rocket.Chat incoming webhooks accept the same payload."""

    name = "slack"

    def __init__(self, webhook_url: Optional[str] = None, *, token: Optional[str] = None,
                 channel: Optional[str] = None, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        if not webhook_url and not (token and channel):
            raise ValueError("Slack needs a webhook URL, or token= and channel=")
        self.webhook_url, self.token, self.channel = webhook_url, token, channel

    def payload(self, msg: Message) -> Dict[str, Any]:
        head = f"*{msg['title']}*" + (f"  <{msg['url']}|Open>" if msg.get("url") else "")
        blocks: List[Dict[str, Any]] = [{"type": "section", "text": {"type": "mrkdwn", "text": f"{head}\n{msg['text']}"}}]
        if msg.get("fields"):
            blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"}
                                                          for k, v in msg["fields"].items()][:10]})
        return {"text": f"{msg['title']}: {msg['text']}", "blocks": blocks}

    async def send(self, msg: Message) -> None:
        body = self.payload(msg)
        if self.webhook_url:
            await self.http.request("POST", self.webhook_url, json_body=body, expect_ok=True, what="slack webhook")
            return
        res = await self.http.request("POST", "https://slack.com/api/chat.postMessage", bearer=self.token,
                                      json_body={"channel": self.channel, **body}, expect_ok=True, what="slack")
        data = res.json() or {}
        if not data.get("ok"):
            raise RuntimeError(f"slack: {data.get('error', 'unknown error')}")


class DiscordChat(ChatChannel):
    name = "discord"

    def __init__(self, webhook_url: str, *, username: str = "nodemeet", http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.webhook_url, self.username = webhook_url, username

    async def send(self, msg: Message) -> None:
        embed: Dict[str, Any] = {"title": msg["title"][:256], "description": msg["text"][:4000],
                                 "color": int(msg.get("color", "#6366f1").lstrip("#"), 16),
                                 "fields": [{"name": k, "value": str(v)[:1024], "inline": True}
                                            for k, v in (msg.get("fields") or {}).items()][:25]}
        if msg.get("url"):
            embed["url"] = msg["url"]
        await self.http.request("POST", self.webhook_url, params={"wait": "true"},
                                json_body={"username": self.username, "embeds": [embed],
                                           "allowed_mentions": {"parse": []}}, expect_ok=True, what="discord")


class TeamsChat(ChatChannel):
    """Microsoft Teams: a Workflows ("Post to a channel when a webhook request is received")
    or legacy incoming-webhook URL. Sends an Adaptive Card."""

    name = "teams"

    def __init__(self, webhook_url: str, *, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.webhook_url = webhook_url

    def payload(self, msg: Message) -> Dict[str, Any]:
        body: List[Dict[str, Any]] = [
            {"type": "TextBlock", "text": msg["title"], "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "TextBlock", "text": msg["text"], "wrap": True}]
        if msg.get("fields"):
            body.append({"type": "FactSet", "facts": [{"title": k, "value": str(v)} for k, v in msg["fields"].items()]})
        card: Dict[str, Any] = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                                "type": "AdaptiveCard", "version": "1.4", "body": body}
        if msg.get("url"):
            card["actions"] = [{"type": "Action.OpenUrl", "title": "Open", "url": msg["url"]}]
        return {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}

    async def send(self, msg: Message) -> None:
        await self.http.request("POST", self.webhook_url, json_body=self.payload(msg), expect_ok=True, what="teams")


class GoogleChat(ChatChannel):
    name = "google_chat"

    def __init__(self, webhook_url: str, *, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.webhook_url = webhook_url

    async def send(self, msg: Message) -> None:
        text = f"*{msg['title']}*\n{msg['text']}" + (f"\n<{msg['url']}|Open>" if msg.get("url") else "")
        await self.http.request("POST", self.webhook_url, json_body={"text": text}, expect_ok=True,
                                what="google chat")


class TelegramChat(ChatChannel):
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, *, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.bot_token, self.chat_id = bot_token, chat_id

    async def send(self, msg: Message) -> None:
        import html
        text = f"<b>{html.escape(msg['title'])}</b>\n{html.escape(msg['text'])}"
        if msg.get("url"):
            text += f'\n<a href="{html.escape(msg["url"], quote=True)}">Open</a>'
        res = await self.http.request("POST", f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                                      json_body={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                                 "disable_web_page_preview": True}, expect_ok=True, what="telegram")
        if not (res.json() or {}).get("ok", True):
            raise RuntimeError(f"telegram: {res.text[:200]}")


class WebhookChat(ChatChannel):
    """POST the message as JSON to your own endpoint."""

    name = "webhook"

    def __init__(self, url: str, *, headers: Optional[Dict[str, str]] = None, http: Optional[HTTPClient] = None) -> None:
        super().__init__(http)
        self.url, self.headers = url, headers or {}

    async def send(self, msg: Message) -> None:
        await self.http.request("POST", self.url, json_body=msg, headers=self.headers, expect_ok=True, what="chat webhook")


class ChatService:
    """Listens to every nodemeet event and posts the interesting ones to chat channels."""

    def __init__(self, meet: "NodeMeet", channel: ChatChannel, *, events: Iterable[str] = DEFAULT_EVENTS,
                 tenant_id: Optional[str] = None, timezone: Optional[str] = None,
                 format: Optional[Callable[[str, Dict[str, Any]], Optional[Message]]] = None) -> None:
        self.meet, self.channel, self.events = meet, channel, list(events)
        self.tenant_id, self.timezone, self.format = tenant_id, timezone, format
        self.sent: List[Message] = []
        meet.webhooks.taps.append(self.on_event)

    def wants(self, event: str) -> bool:
        return any(fnmatch.fnmatch(event, pat) for pat in self.events)

    async def on_event(self, event: str, data: Dict[str, Any], tenant_id: Optional[str]) -> None:
        if event.startswith(("chat_app.", "integration.")) or not self.wants(event) or \
                (self.tenant_id and tenant_id != self.tenant_id):
            return  # never react to our own outcome events (no loops)
        msg = (self.format or (lambda e, d: describe(e, d, base_url=self.meet.base_url,
                                                     timezone=self.timezone)))(event, data)
        if msg is None:
            return
        msg = {"event": event, **msg}
        info = {"app": self.channel.name, "event": event, "title": msg.get("title")}
        try:
            await self.channel.send(msg)
        except Exception as exc:
            info["error"] = str(exc)[:300]
            await self.meet.emit_webhook("chat_app.failed", info, tenant_id=tenant_id)
            await self.meet.emit_webhook("integration.error", {"source": "chat_app", **info}, tenant_id=tenant_id)
            raise
        self.sent.append(msg)
        await self.meet.emit_webhook("chat_app.sent", info, tenant_id=tenant_id)


def make_channel(kind: str, *args: Any, **kw: Any) -> ChatChannel:
    kinds = {"slack": SlackChat, "mattermost": SlackChat, "rocketchat": SlackChat, "discord": DiscordChat,
             "teams": TeamsChat, "msteams": TeamsChat, "google_chat": GoogleChat, "googlechat": GoogleChat,
             "gchat": GoogleChat, "telegram": TelegramChat, "webhook": WebhookChat}
    key = kind.lower().replace("-", "_").replace(" ", "_")
    if key not in kinds:
        from ..easy import did_you_mean
        raise ValueError(f"unknown chat app {kind!r}." + did_you_mean(key, kinds))
    return kinds[key](*args, **kw)
