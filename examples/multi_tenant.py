"""Multi-tenant SaaS: each of YOUR customers gets their own nodemeet API key.

    python multi_tenant.py        # prints two tenant keys, then serves on :8080

Then, as tenant "acme" (from acme's backend):
    curl -X POST localhost:8080/api/rooms -H "Authorization: Bearer <acme key>" -d '{"id":"acme-standup"}'
    curl -X POST localhost:8080/api/tokens -H "Authorization: Bearer <acme key>" \\
         -d '{"room":"acme-standup","user_id":"u1","role":"host"}'
Globex's key can't see or touch acme's rooms, hosts, bookings or webhooks.
"""
from nodemeet import ApiKey, NodeMeet, SQLiteStorage
from nodemeet.tenancy import generate_key

meet = NodeMeet("change-me-to-a-long-random-secret", api_key="MASTER-KEY-keep-private",
                storage=SQLiteStorage("tenants.sqlite3"), base_url="http://localhost:8080")


async def create_tenants(app):
    for tenant, scopes in (("acme", ["*"]), ("globex", ["rooms:read", "rooms:write", "tokens:create"])):
        plaintext, prefix, digest = generate_key()
        await meet.storage.save_api_key(ApiKey(tenant_id=tenant, key_hash=digest, prefix=prefix,
                                               scopes=scopes, rate_limit_per_minute=600))
        print(f"{tenant:<7} key: {plaintext}   scopes={scopes}")

meet.app().on_startup.append(create_tenants)
meet.run(port=8080)
