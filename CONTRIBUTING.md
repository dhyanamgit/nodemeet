# Contributing to nodemeet

Thanks for helping! Fixes, docs, tests, storage backends and integrations are all welcome.

## Before you start
- Pick an issue (`good first issue` ones are small) and comment on it so nobody else builds the same thing.
- For something new, open an issue first so we agree on the shape.

## Set up
```bash
git clone https://github.com/dhyanamgit/nodemeet && cd nodemeet
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e . -r tests/requirements-test.txt
pytest tests -q -rs --ignore=tests/integration --ignore=tests/e2e     # ~10 s, no services needed
```
Database tests: `docker compose -f tests/integration/docker-compose.yml --profile full up -d --wait`, then
`pytest tests/integration`. Browser tests: `pip install -r tests/e2e/requirements-e2e.txt`,
`python -m playwright install chromium`, `pytest tests/e2e`. See [TESTING.md](TESTING.md).

## Pull requests
1. Fork, branch from `main`, keep one PR to one thing.
2. Add a test (a bug fix starts with a test that fails without it).
3. Update the docs and add a line to `CHANGELOG.md`.
4. The core (tokens, scheduling, storage, ASGI adapter) must not import aiohttp or any optional extra.
5. No new required dependency without a reason in the PR.

## Contributor agreement (required)
nodemeet is licensed by its author under the PolyForm Noncommercial licence and is also offered under commercial
licences. So that this stays possible, every contributor agrees to the following by opening a pull request, and
confirms it by adding this line to the PR description:

> I agree to the nodemeet Contributor Agreement in CONTRIBUTING.md.

**nodemeet Contributor Agreement.** For each contribution you submit to this project:
1. You confirm you wrote it, or otherwise have the right to submit it, and it contains nothing you don't have the
   right to give.
2. You keep the copyright in your contribution, and you grant Dhyanam Shah and anyone he authorises a perpetual,
   worldwide, non-exclusive, royalty-free, irrevocable licence to use, copy, change, distribute, sublicense and
   relicense it, including under commercial terms, plus a matching patent licence for any of your patent claims
   that the contribution needs.
3. You understand the contribution and this agreement are public, and that you may be credited by your GitHub name.

## Be kind
See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Security problems go to [SECURITY.md](SECURITY.md), not issues.
