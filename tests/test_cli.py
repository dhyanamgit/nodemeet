from nodemeet import TokenSigner
from nodemeet.cli import build_parser, main


def test_token_command():
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["token", "standup", "u1", "--role", "host", "--name", "Ada",
                     "--secret", "cli-secret-long-enough", "--base-url", "https://m.test/meet"])
    assert code == 0
    token, url = buf.getvalue().strip().splitlines()
    claims = TokenSigner("cli-secret-long-enough").verify(token, room="standup")
    assert claims.role == "host" and claims.name == "Ada"
    assert url == f"https://m.test/meet/r/standup#token={token}"


def test_token_requires_secret():
    import contextlib
    import io
    import os

    os.environ.pop("NODEMEET_SECRET", None)
    with contextlib.redirect_stderr(io.StringIO()):
        assert main(["token", "r", "u"]) == 2


def test_serve_parser_defaults():
    args = build_parser().parse_args(["serve", "--port", "9000", "--no-sfu", "--cors", "*"])
    assert args.port == 9000 and args.no_sfu and args.cors == ["*"]


def test_keys_and_db_commands():
    import contextlib
    import io
    import json
    import os
    import tempfile

    db = os.path.join(tempfile.mkdtemp(), "cli.sqlite3")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        assert main(["db", "upgrade", "--db", db]) == 0
        assert main(["keys", "create", "--tenant", "acme", "--scopes", "rooms:read,tokens:create",
                     "--db", db]) == 0
    created = json.loads(out.getvalue().split("up to date\n", 1)[1])
    assert created["key"].startswith("nmk_") and created["scopes"] == ["rooms:read", "tokens:create"]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["keys", "revoke", created["id"], "--db", db]) == 0
        assert main(["keys", "list", "--db", db]) == 0
    assert "revoked" in out.getvalue().splitlines()[-1]
