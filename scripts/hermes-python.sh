#!/bin/sh
# Run a Python file inside the installed Hermes runtime: the same interpreter and bootstrap as the hermes CLI,
# taken from `hermes --print-runtime-command`.
set -e
CMD=$(hermes --print-runtime-command)
PY=$(printf '%s' "$CMD" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0])')
ROOT=$(printf '%s' "$CMD" | python3 -c 'import json,re,sys; print(re.search(r"sys\.path\.insert\(0, [\x27\"]([^\x27\"]+)", json.load(sys.stdin)[-1]).group(1))')
exec "$PY" -I -c 'import os,sys,runpy
root, script = sys.argv[1], sys.argv[2]
os.environ.pop("PYTHONHOME", None); os.environ.pop("PYTHONPATH", None)
sys.path.insert(0, root); sys._hermes_pin_default_home = True
import hermes_bootstrap
os.environ.get("HERMES_HOME") or os.environ.__setitem__("HERMES_HOME", str(__import__("hermes_constants").get_default_hermes_root()))
sys.argv = sys.argv[2:]
runpy.run_path(script, run_name="__main__")' "$ROOT" "$@"
