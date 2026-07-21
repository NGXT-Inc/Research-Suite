#!/bin/sh
set -eu

verify_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
verify_python=${MERV_VERIFY_PYTHON:-python}
catalog_expected=45e46fac9ea0a4d97fa12d1fc9b111e1088f862992288ffca01a735b70ee2420
phase_3_brain_loc=41265
# Phase 4 preserves wire behavior while moving guidance/projection ownership.
# Phase 7 must reclaim this temporary high watermark to <=40,600.
brain_loc_max=41700
surface_orchestration_max=100
http_views_max=470
http_app_max=125

cd "$verify_dir"
brain_loc=$(find src/merv/brain -name '*.py' | xargs wc -l | tail -1 | awk '{print $1}')
test "$((brain_loc_max - phase_3_brain_loc))" -eq 435
test "$brain_loc" -le "$brain_loc_max"
test "$(wc -l < src/merv/brain/mlflow/exhibit.py)" -le 15
test "$(wc -l < src/merv/brain/surface/tools/tool_handlers.py)" \
  -le "$surface_orchestration_max"
test "$(wc -l < src/merv/brain/surface/transport/api/views.py)" \
  -le "$http_views_max"
test "$(wc -l < src/merv/brain/surface/transport/api/app.py)" \
  -le "$http_app_max"
"$verify_python" -m pytest tests/structure -q
"$verify_python" scripts/regen_tool_catalog.py --check
catalog_actual=$(shasum -a 256 src/merv/proxy/_tool_catalog.json | awk '{print $1}')
test "$catalog_actual" = "$catalog_expected"
MERV_REQUIRE_POSTGRES_TESTS=1 \
  "$verify_python" -m pytest tests/state/test_postgres_dialect.py -q
"$verify_python" -m pytest -q
