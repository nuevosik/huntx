#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.config/opencode/agents" "$HOME/.config/opencode/commands" "$HOME/.local/bin"
chmod +x "$ROOT/bin/hx"
ln -sf "$ROOT/bin/hx" "$HOME/.local/bin/hx"
ln -sf "$ROOT/opencode/agents/hunt.md" "$HOME/.config/opencode/agents/hunt.md"
ln -sf "$ROOT/opencode/commands/brief.md" "$HOME/.config/opencode/commands/brief.md"
ln -sf "$ROOT/opencode/commands/next.md" "$HOME/.config/opencode/commands/next.md"
ln -sf "$ROOT/opencode/commands/debrief.md" "$HOME/.config/opencode/commands/debrief.md"
echo "symlinks ok"
echo "adicione ao array plugin do ~/.config/opencode/opencode.jsonc:"
echo "\"file://$ROOT/opencode/plugin/hunt.ts\""
echo "reinicie o opencode depois de editar o config (nao ha hot reload)"
