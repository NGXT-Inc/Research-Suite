#!/bin/sh
set -eu

verify_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
verify_python=${MERV_VERIFY_PYTHON:-python}
catalog_expected=92a9b82946adf3aec6762d448bdfd01d45b733619a2f23e0a436e898eefd7aae

cd "$verify_dir"
test ! -e src/merv/brain/mlflow/exhibit.py
"$verify_python" -m pytest tests/structure -q
"$verify_python" scripts/regen_tool_catalog.py --check
catalog_actual=$(shasum -a 256 src/merv/proxy/_tool_catalog.json | awk '{print $1}')
test "$catalog_actual" = "$catalog_expected"
MERV_REQUIRE_POSTGRES_TESTS=1 \
  "$verify_python" -m pytest tests/state/test_postgres_dialect.py -q
"$verify_python" -m pytest -q
