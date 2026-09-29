#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.config/opencode/agents" "$HOME/.config/opencode/commands" "$HOME/.local/bin"
chmod +x "$ROOT/bin/hx"
link() {
  target="$1"
  source="$2"
  if [ -e "$target" ] && [ ! -L "$target" ]; then
    backup="$target.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$target" "$backup"
    echo "backup: $backup"
  fi
  ln -sfn "$source" "$target"
}

link "$HOME/.local/bin/hx" "$ROOT/bin/hx"
link "$HOME/.config/opencode/agents/hunt.md" "$ROOT/opencode/agents/hunt.md"
link "$HOME/.config/opencode/agents/hunt-auto.md" "$ROOT/opencode/agents/hunt-auto.md"
link "$HOME/.config/opencode/commands/brief.md" "$ROOT/opencode/commands/brief.md"
link "$HOME/.config/opencode/commands/next.md" "$ROOT/opencode/commands/next.md"
link "$HOME/.config/opencode/commands/debrief.md" "$ROOT/opencode/commands/debrief.md"
echo "symlinks ok"
echo "adicione ao array plugin do ~/.config/opencode/opencode.jsonc:"
echo "\"file://$ROOT/opencode/plugin/hunt.ts\""
echo "para o runtime omp nada precisa ser instalado: o runner passa --hook $ROOT/runner/omp/guard-hook.ts"
echo "reinicie o opencode depois de editar o config (nao ha hot reload)"
