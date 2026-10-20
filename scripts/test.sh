#!/bin/sh
# Run the suite against a Hermes checkout (default ../hermes-test-0.22.2), never the live install.
cd "$(dirname "$0")/.." || exit 1
TREE=$(cd "${HERMES_TEST_TREE:-../hermes-test-0.22.2}" && pwd) || exit 1
HERMES_SOURCE="$TREE" HERMES_DISABLE_LAZY_INSTALLS=1 \
  exec "$TREE/.venv/bin/python" -m pytest -q -p no:cacheprovider "$@"
