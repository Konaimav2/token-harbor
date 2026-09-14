#!/usr/bin/env bash
# P9 one-command smoke suite: compile/help/dry-run only, no browsers; network required for enable_free_models --dry-run.
set -euo pipefail
trap 'echo "FAIL: aborted at line $LINENO"' ERR
cd "$(dirname "${BASH_SOURCE[0]}")/.."
if [[ -x .venv/bin/python ]]; then PY=".venv/bin/python"; else PY="python3"; fi
FAIL=0
step() { local d="$1"; shift; if "$@" </dev/null; then echo "PASS: $d"; else local rc=$?; echo "FAIL: $d (rc=$rc)"; FAIL=1; fi; }
step "compile entry points" "$PY" -m py_compile th-tui.py th-webshare.py th-flamingo.py import/import_tokenharbor.py tools/enable_free_models.py keystore.py
# th-tui is interactive TUI with no --help (times out rc=124); expected skip.
echo "SKIP: th-tui --help (interactive TUI has no --help flag)"
step "th-webshare --help" "$PY" th-webshare.py --help
step "th-flamingo --help" "$PY" th-flamingo.py --help
step "import --help" "$PY" import/import_tokenharbor.py --help
step "enable_free_models --help" "$PY" tools/enable_free_models.py --help
step "import --dry-run" "$PY" import/import_tokenharbor.py --dry-run
step "enable_free_models --dry-run (requires network)" timeout 90 "$PY" tools/enable_free_models.py --dry-run
step "keystore record-count" "$PY" keystore.py
exit "$FAIL"
