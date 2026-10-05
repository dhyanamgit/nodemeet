"""v0.3 smoke tests: roles, permissions, branding and storage families import and wire up."""
import nodemeet
from nodemeet import permissions, branding


def test_version():
    assert nodemeet.__version__ == nodemeet._version.__version__ and nodemeet.__version__.count(".") == 2


def test_permission_catalogue_is_extensive():
    P = nodemeet.PERMISSIONS
    names = set()
    items = P.values() if isinstance(P, dict) else P
    for k, v in (P.items() if isinstance(P, dict) else []):
        names.add(k)
        if isinstance(v, (list, tuple, set, dict)):
            names.update(v)
    if not isinstance(P, dict):
        names.update(getattr(x, "name", x) for x in items)
    names = {n for n in names if isinstance(n, str) and "." in n}
    assert len(names) >= 40, len(names)
    for p in ("audio.publish", "video.publish", "screen.publish", "chat.send", "moderate.kick", "moderate.override_rank"):
        assert p in names


def test_builtin_roles_have_ranks():
    roles = {r.name: r for r in permissions.builtin_roles().values()} if isinstance(permissions.builtin_roles(), dict) \
        else {r.name: r for r in permissions.builtin_roles()}
    assert roles["host"].rank > roles["participant"].rank > roles["viewer"].rank


def test_branding_defaults_and_resolver_exist():
    assert isinstance(branding.DEFAULTS, dict) and "colors" in branding.DEFAULTS
    assert nodemeet.BrandingResolver


def test_storage_families_import():
    from nodemeet import storage
    for name in ("KeyValueStorage", "MemoryKV", "DocumentStorage", "MemoryDocs", "DBAPIStorage"):
        assert hasattr(storage, name), name
