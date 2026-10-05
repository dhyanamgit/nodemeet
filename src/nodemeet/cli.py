"""Command line interface.

    nodemeet serve   [--db FILE | --db-url URL] [--redis URL] ...
    nodemeet token   ROOM USER [--role host] [--tenant acme]
    nodemeet keys    create --tenant acme [--scopes rooms:read,tokens:create] [--rate-limit 600]
    nodemeet keys    list [--tenant acme] | revoke KEY_ID
    nodemeet db      upgrade
    nodemeet init    [DIR] [--template basic|fastapi|booking]   # starter project
    nodemeet link    ROOM [NAME] [--host]                       # print a join link
    nodemeet doctor                                             # check your setup
    nodemeet cheatsheet                                         # the one-page API summary
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
from typing import Any, List, Optional
from urllib.parse import quote

from ._version import __version__
from .scaffold import TEMPLATES


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(name, default)


def _db_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=_env("NODEMEET_DB"),
                   help="database: meet.db, postgres://..., mongodb://..., redis://... (any supported URL)")
    p.add_argument("--db-url", default=_env("NODEMEET_DB_URL"), help="same as --db (kept for compatibility)")


def _storage(args: argparse.Namespace, required: bool = False) -> Any:
    from .easy import storage_from_url
    url = args.db_url or args.db
    if not url and required:
        raise SystemExit("error: --db is required, e.g. --db meet.db or --db postgres://user:pass@host/app")
    return storage_from_url(url)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nodemeet", description="Self-hosted video meetings + scheduling")
    p.add_argument("--version", action="version", version=f"nodemeet {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the nodemeet server")
    s.add_argument("--config", default=_env("NODEMEET_CONFIG"),
                   help="nodemeet.toml / .json (used automatically if ./nodemeet.toml exists)")
    s.add_argument("--host", default=_env("NODEMEET_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(_env("NODEMEET_PORT", "8080") or 8080))
    s.add_argument("--secret", default=_env("NODEMEET_SECRET"), help="token signing secret")
    s.add_argument("--api-key", default=_env("NODEMEET_API_KEY"), help="master REST API key")
    s.add_argument("--base-url", default=_env("NODEMEET_BASE_URL"))
    _db_args(s)
    s.add_argument("--redis", default=_env("NODEMEET_REDIS_URL"), help="redis:// URL for clustering")
    s.add_argument("--server", choices=["auto", "aiohttp", "uvicorn", "dev"], default="auto",
                   help="dev = built-in zero-dependency server")
    s.add_argument("--trust-proxy", action="store_true", help="honour X-Forwarded-* headers")
    s.add_argument("--no-sfu", action="store_true", help="peer-to-peer only")
    s.add_argument("--no-waiting-room", action="store_true",
                   help="new rooms let everyone straight in (waiting room is on by default)")
    s.add_argument("--p2p-max", type=int, default=4, help="max mesh size in auto mode")
    s.add_argument("--stun", action="append", help="STUN/TURN url (repeatable)")
    s.add_argument("--cors", action="append", default=[], help="allowed origin (repeatable, or *)")
    s.add_argument("--smtp-host", default=_env("NODEMEET_SMTP_HOST"))
    s.add_argument("--smtp-port", type=int, default=int(_env("NODEMEET_SMTP_PORT", "587") or 587))
    s.add_argument("--smtp-user", default=_env("NODEMEET_SMTP_USER"))
    s.add_argument("--smtp-password", default=_env("NODEMEET_SMTP_PASSWORD"))
    s.add_argument("--smtp-sender", default=_env("NODEMEET_SMTP_SENDER", "nodemeet@localhost"))
    s.add_argument("--smtp-security", default=_env("NODEMEET_SMTP_SECURITY", "starttls"),
                   choices=["starttls", "ssl", "none"])
    s.add_argument("--webhook", action="append", default=[], help="webhook URL (repeatable)")
    s.add_argument("--webhook-secret", default=_env("NODEMEET_WEBHOOK_SECRET"))
    s.add_argument("--no-reminders", action="store_true", help="don't run the reminder loop here")
    s.add_argument("--demo", action="store_true", help="print ready-to-use host/guest links")
    s.add_argument("--log-level", default="INFO")

    t = sub.add_parser("token", help="mint a signed join token")
    t.add_argument("room")
    t.add_argument("user_id")
    t.add_argument("--role", default="participant",
                   help="host, participant, viewer or any custom role name")
    t.add_argument("--grant", action="append", default=[], help="extra permission (repeatable)")
    t.add_argument("--revoke", action="append", default=[], help="remove a permission (repeatable)")
    t.add_argument("--skip-waiting-room", action="store_true", help="this link bypasses the waiting room")
    t.add_argument("--name")
    t.add_argument("--tenant", help="bind the token to a tenant")
    t.add_argument("--ttl", type=int, default=3600, help="seconds (0 = never expires)")
    t.add_argument("--secret", default=_env("NODEMEET_SECRET"))
    t.add_argument("--base-url", default=_env("NODEMEET_BASE_URL"),
                   help="also print a join URL for this server")

    k = sub.add_parser("keys", help="manage tenant API keys")
    ksub = k.add_subparsers(dest="keys_command", required=True)
    kc = ksub.add_parser("create")
    kc.add_argument("--tenant", required=True)
    kc.add_argument("--name", default="")
    kc.add_argument("--scopes", default="*", help="comma separated, default *")
    kc.add_argument("--rate-limit", type=int, help="requests per minute")
    _db_args(kc)
    kl = ksub.add_parser("list")
    kl.add_argument("--tenant")
    _db_args(kl)
    kr = ksub.add_parser("revoke")
    kr.add_argument("key_id")
    _db_args(kr)

    i = sub.add_parser("init", help="create a starter project (app.py, nodemeet.toml, .env)")
    i.add_argument("dir", nargs="?", default=".")
    i.add_argument("--template", choices=sorted(TEMPLATES), default="basic")
    i.add_argument("--force", action="store_true", help="overwrite existing files")

    ln = sub.add_parser("link", help="print a join link (uses NODEMEET_SECRET / nodemeet.toml)")
    ln.add_argument("room")
    ln.add_argument("name", nargs="?")
    ln.add_argument("--host", action="store_true", help="host link")
    ln.add_argument("--role", help="any role name (default participant)")
    ln.add_argument("--skip-waiting-room", action="store_true")
    ln.add_argument("--ttl", default="1d", help="e.g. 3600, 2h, 7d")
    ln.add_argument("--config", default=_env("NODEMEET_CONFIG"))

    sub.add_parser("doctor", help="check your setup and optional packages")
    sub.add_parser("cheatsheet", help="print the one-page API summary")

    d = sub.add_parser("db", help="database schema")
    dsub = d.add_subparsers(dest="db_command", required=True)
    du = dsub.add_parser("upgrade", help="create/upgrade nodemeet tables")
    _db_args(du)
    return p


def cmd_token(args: argparse.Namespace) -> int:
    from .tokens import TokenSigner

    if not args.secret:
        print("error: --secret or NODEMEET_SECRET is required", file=sys.stderr)
        return 2
    signer = TokenSigner(args.secret)
    grant = list(args.grant) + (["room.bypass_lobby"] if args.skip_waiting_room else [])
    token = signer.create(args.room, args.user_id, args.role, name=args.name, ttl=args.ttl or 0,
                          tenant=args.tenant, grant=grant, revoke=args.revoke)
    print(token)
    if args.base_url:
        print(f"{args.base_url.rstrip('/')}/r/{quote(args.room, safe='')}#token={token}")
    return 0


async def _keys(args: argparse.Namespace) -> int:
    from .tenancy import ApiKey, generate_key
    storage = _storage(args, required=True)
    await storage.setup()
    try:
        if args.keys_command == "create":
            plaintext, prefix, digest = generate_key()
            key = ApiKey(tenant_id=args.tenant, key_hash=digest, prefix=prefix, name=args.name,
                         scopes=[s.strip() for s in args.scopes.split(",") if s.strip()],
                         rate_limit_per_minute=args.rate_limit)
            await storage.save_api_key(key)
            print(json.dumps({**key.to_dict(include_hash=False), "key": plaintext}, indent=2))
            print("Store the key now - it cannot be shown again.", file=sys.stderr)
        elif args.keys_command == "list":
            for key in await storage.list_api_keys(args.tenant):
                state = "revoked" if key.revoked else "active"
                print(f"{key.id}  {key.tenant_id:<16} {key.prefix}...  {','.join(key.scopes):<30} {state}")
        elif args.keys_command == "revoke":
            key = await storage.get_api_key(args.key_id)
            if key is None:
                print("error: no such key", file=sys.stderr)
                return 1
            key.revoked = True
            await storage.save_api_key(key)
            print(f"revoked {key.id}")
    finally:
        await storage.close()
    return 0


async def _db_upgrade(args: argparse.Namespace) -> int:
    storage = _storage(args, required=True)
    await storage.setup()
    await storage.close()
    print("database schema is up to date")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .integrations import LoggingMailer, SMTPMailer, WebhookDispatcher
    from .server import NodeMeet
    from pathlib import Path

    config = args.config or ("nodemeet.toml" if Path("nodemeet.toml").is_file() else None)
    if config:
        over: dict = {}
        if args.base_url:
            over["base_url"] = args.base_url
        elif not os.environ.get("NODEMEET_BASE_URL"):
            over["base_url"] = f"http://localhost:{args.port}"
        if args.no_sfu:
            over["sfu"] = False
        if args.no_waiting_room:
            over["waiting_room"] = False
        if args.trust_proxy:
            over["trust_proxy"] = True
        if args.cors:
            over["cors_origins"] = args.cors
        meet = NodeMeet.from_config(config, **over)
        print(f"nodemeet {__version__} on http://{args.host}:{args.port}  (config: {config})")
        _print_demo_links(meet, args)
        return _serve(meet, args)

    secret = args.secret
    if not secret:
        secret = secrets.token_urlsafe(32)
        print(f"! no --secret given; using a random one for this run: {secret}")
        if args.redis:
            print("! with --redis every server MUST share the same --secret", file=sys.stderr)
    api_key = args.api_key
    if not api_key:
        api_key = secrets.token_urlsafe(24)
        print(f"! no --api-key given; master API key for this run: {api_key}")
    base_url = args.base_url
    if not base_url and args.host in ("127.0.0.1", "localhost", "0.0.0.0"):
        base_url = f"http://localhost:{args.port}"
    mailer = (SMTPMailer(args.smtp_host, args.smtp_port, username=args.smtp_user,
                         password=args.smtp_password, sender=args.smtp_sender,
                         security=args.smtp_security) if args.smtp_host else LoggingMailer())
    hooks = WebhookDispatcher()
    if args.webhook:
        if not args.webhook_secret:
            print("error: --webhook-secret is required with --webhook", file=sys.stderr)
            return 2
        for url in args.webhook:
            hooks.add(url, args.webhook_secret)
    ice = [{"urls": u} for u in args.stun] if args.stun else None
    meet = NodeMeet(secret, storage=_storage(args), base_url=base_url, api_key=api_key,
                    mailer=mailer, webhooks=hooks, broker=args.redis or None,
                    sfu=not args.no_sfu, p2p_max=args.p2p_max, ice_servers=ice,
                    cors_origins=args.cors, trust_proxy=args.trust_proxy,
                    reminders=not args.no_reminders, waiting_room=not args.no_waiting_room)
    print(f"nodemeet {__version__} on http://{args.host}:{args.port}  "
          f"(SFU: {'on' if meet.sfu.available else 'off'}, cluster: {'redis' if args.redis else 'off'})")
    _print_demo_links(meet, args)
    return _serve(meet, args)


def _print_demo_links(meet: Any, args: argparse.Namespace) -> None:
    if args.demo:
        print("host link:  ", meet.host_link("demo", "Host", ttl=86400))
        print("guest link: ", meet.guest_link("demo", "Guest", ttl=86400),
              " (waits in the waiting room until the host admits them)")


def _serve(meet: Any, args: argparse.Namespace) -> int:
    if args.server == "uvicorn":
        import uvicorn
        uvicorn.run(meet.asgi(), host=args.host, port=args.port)
    elif args.server == "aiohttp":
        from aiohttp import web
        web.run_app(meet.app(), host=args.host, port=args.port, print=None)
    else:
        meet.run(host=args.host, port=args.port, server="dev" if args.server == "dev" else "auto")
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from .scaffold import init_project
    written = init_project(args.dir, args.template, args.force)
    for f in written:
        print("created", f)
    if not written:
        print("nothing to do (files exist; use --force to overwrite)")
    where = "" if args.dir in (".", "") else f"cd {args.dir}\n  "
    run = "uvicorn app:app --reload" if args.template == "fastapi" else "python app.py"
    print(f"\nnext:\n  {where}{run}")
    return 0


def _meet_for_cli(config: Optional[str]) -> Any:
    from .server import NodeMeet
    from pathlib import Path
    if config or Path("nodemeet.toml").is_file():
        return NodeMeet.from_config(config or "nodemeet.toml")
    return NodeMeet.from_env()


def cmd_link(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.ERROR)
    meet = _meet_for_cli(args.config)
    if meet.secret_generated:
        print("error: set NODEMEET_SECRET (or add a nodemeet.toml) so the server accepts this link",
              file=sys.stderr)
        return 2
    role = "host" if args.host else (args.role or "participant")
    print(meet.link(args.room, args.name, role, skip_waiting_room=args.skip_waiting_room, ttl=args.ttl))
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    import importlib.util
    import platform
    from pathlib import Path
    ok = lambda b: "ok  " if b else "--  "  # noqa: E731
    print(f"nodemeet {__version__}  Python {platform.python_version()}  ({platform.system()})")
    if sys.version_info < (3, 10):
        print("!! Python 3.10+ is required")
    cfg = Path("nodemeet.toml")
    print(f"{ok(cfg.is_file())}nodemeet.toml in this folder" + ("" if cfg.is_file() else "  (create with: nodemeet init)"))
    print(f"{ok(bool(os.environ.get('NODEMEET_SECRET')) or Path('.env').is_file())}NODEMEET_SECRET set "
          "(or a .env file)")
    try:
        meet = _meet_for_cli(None)
        print(f"ok  config loads: {meet!r}")
        if meet.secret_generated:
            print("!!  no secret: links break on restart. Set NODEMEET_SECRET.")
        try:
            asyncio.run(meet.storage.setup())
            asyncio.run(meet.storage.close())
            print(f"ok  database reachable ({type(meet.storage).__name__})")
        except Exception as exc:  # noqa: BLE001
            print(f"!!  database: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"!!  config error: {exc}")
    print("\noptional packages (install only what you use):")
    for mod, extra, what in (("aiohttp", "aiohttp", "production web server"), ("uvicorn", "asgi", "ASGI server"),
                             ("aiortc", "sfu", "SFU for big meetings (5+ people)"), ("redis", "redis", "clustering / Redis db"),
                             ("sqlalchemy", "sql", "Postgres/MySQL/MSSQL via SQLAlchemy"), ("asyncpg", "postgres", "Postgres driver"),
                             ("pymongo", "mongo", "MongoDB"), ("boto3", "aws", "DynamoDB / S3"),
                             ("faster_whisper", "captions", "live captions"), ("cryptography", "push", "Web Push / FCM notifications"), ("onelogin", "saml", "SAML SSO")):
        found = importlib.util.find_spec(mod) is not None
        print(f"  {ok(found)}{mod:<15} {what:<38}" + ("" if found else f'   pip install "nodemeet[{extra}]"'))
    print("\n(the built-in server works with no extras; 'nodemeet serve' uses it automatically)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "token":
        return cmd_token(args)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "init":
        return cmd_init(args)
    if args.command == "link":
        return cmd_link(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "cheatsheet":
        from .quick import CHEATSHEET
        print(CHEATSHEET)
        return 0
    if args.command == "keys":
        return asyncio.run(_keys(args))
    if args.command == "db":
        return asyncio.run(_db_upgrade(args))
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
