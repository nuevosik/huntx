#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
python3 -m unittest discover -s tests -v
if command -v node >/dev/null 2>&1; then
  node --test tests/
else
  echo "node ausente: pulando os testes do plugin/tripwire" >&2
fi
