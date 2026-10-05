#!/usr/bin/env bash
# nodemeet: run every test (macOS / Linux).  ./scripts/test-all.sh [--full] [--no-docker] [--no-browsers]
set -u
FULL=0; DOCKER=1; BROWSERS=1
for a in "$@"; do case $a in --full) FULL=1;; --no-docker) DOCKER=0;; --no-browsers) BROWSERS=0;; esac; done
REPORT=test-report.txt; echo "nodemeet test report $(date)" > $REPORT
python -m pip install -q -e . -r tests/requirements-test.txt -r tests/integration/requirements-db.txt -r tests/e2e/requirements-e2e.txt
python -m pytest tests -q -rs --ignore=tests/integration --ignore=tests/e2e 2>&1 | tee -a $REPORT
if [ $DOCKER = 1 ]; then
  C=tests/integration/docker-compose.yml
  if [ $FULL = 1 ]; then docker compose -f $C --profile full up -d --wait; else docker compose -f $C up -d --wait; fi
  python -m pytest tests/integration -v -rs 2>&1 | tee -a $REPORT
fi
if [ $BROWSERS = 1 ]; then
  python -m playwright install --with-deps chromium firefox webkit
  python -m pytest tests/e2e -v -rs 2>&1 | tee -a $REPORT
fi
echo "Done. Send $REPORT if anything failed."
