#!/bin/sh
set -eu
WORK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
STAGE="${1:-stage0}"
case "$STAGE" in
  stage0|stage1|stage2|stage3|stage4|stage5|stage6) ;;
  *) echo "用法：./run.sh stage0|stage1|stage2|stage3|stage4|stage5|stage6 [阶段参数]" >&2; exit 2 ;;
esac
if [ "$#" -gt 0 ]; then shift; fi
if [ ! -x "$WORK_DIR/.venv/bin/python" ]; then
  python3 -m venv "$WORK_DIR/.venv"
fi
if ! "$WORK_DIR/.venv/bin/python" -c 'import duckdb; assert duckdb.__version__ == "1.4.3"' 2>/dev/null; then
  "$WORK_DIR/.venv/bin/python" -m pip install -r "$WORK_DIR/requirements.txt"
fi
if { [ "$STAGE" = "stage2" ] || [ "$STAGE" = "stage3" ]; } && ! "$WORK_DIR/.venv/bin/python" -c 'import nbformat, nbclient, ipykernel, nbconvert, matplotlib, numpy' 2>/dev/null; then
  if [ -f "$WORK_DIR/requirements-$STAGE.lock.txt" ]; then
    "$WORK_DIR/.venv/bin/python" -m pip install -r "$WORK_DIR/requirements-$STAGE.lock.txt"
  else
    "$WORK_DIR/.venv/bin/python" -m pip install -r "$WORK_DIR/requirements-$STAGE.txt"
  fi
fi
exec "$WORK_DIR/.venv/bin/python" -B "$WORK_DIR/scripts/$STAGE.py" "$@"
