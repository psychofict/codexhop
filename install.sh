#!/usr/bin/env bash
# Install codexhop from this clone and wire it into your shell.
#
#   ./install.sh              link ~/.local/bin/codexhop to this clone (edits here take effect at once)
#   ./install.sh --copy       copy the file instead
#   ./install.sh --rc FILE    use a specific shell rc instead of guessing
#   ./install.sh --no-rc      install the command only, don't touch any rc file
#   ./install.sh --uninstall  remove what this installed (never touches your saved accounts)
#
# The rc line is `eval "$(codexhop shell-init)"`, which adds the `chop` alias and
# tab-completion. A pip or pipx install needs only that line.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${CODEXHOP_BIN_DIR:-$HOME/.local/bin}"
BIN="$BIN_DIR/codexhop"
MARKER='codexhop shell-init'

MODE="link"
RC=""
NO_RC=0
UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --copy) MODE=copy ;;
    --rc) RC="${2:?--rc needs a file}"; shift ;;
    --no-rc) NO_RC=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

command -v python3 >/dev/null 2>&1 || {
  echo "codexhop needs python3 on your PATH." >&2; exit 1; }

# Guess the rc file. bash on macOS reads .bash_profile for login shells, which
# is what Terminal.app starts, so prefer that when it already exists.
if [ -z "$RC" ]; then
  case "${SHELL:-}" in
    */zsh)  RC="${ZDOTDIR:-$HOME}/.zshrc" ;;
    */fish) RC="" ;;
    */bash)
      if [ "$(uname -s)" = "Darwin" ] && [ -f "$HOME/.bash_profile" ]; then
        RC="$HOME/.bash_profile"
      else
        RC="$HOME/.bashrc"
      fi ;;
    *) RC="$HOME/.profile" ;;
  esac
fi
if [ -z "$RC" ] && [ "$NO_RC" = 0 ]; then
  echo "note: ${SHELL:-your shell} isn't a POSIX shell I can wire up automatically." >&2
  echo "      Put $BIN_DIR on your PATH by hand; the alias is optional." >&2
  NO_RC=1
fi

# Our block in an rc file: a comment line and the eval line. Written and removed
# through python, not sed, so the edit means the same on GNU and BSD systems.
rc_edit() {  # rc_edit <rc file> <add|drop> <block>
  python3 - "$@" <<'PY'
import shutil, sys
rc, mode, block = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    src = open(rc).read().splitlines()
except FileNotFoundError:
    src = []
ours = lambda s: "codexhop shell-init" in s or s.strip() == "# codexhop: hop OpenAI Codex between accounts"
out = [s for s in src if not ours(s)]
while out and not out[-1].strip():
    out.pop()
if mode == "add":
    out += ["", *block.splitlines()]
if src:
    shutil.copy2(rc, rc + ".pre-codexhop.bak")
open(rc, "w").write("\n".join(out).lstrip("\n") + "\n")
PY
}

if [ "$UNINSTALL" = 1 ]; then
  rm -f "$BIN"
  if [ "$NO_RC" = 0 ] && [ -f "$RC" ] && grep -q -- "$MARKER" "$RC"; then
    rc_edit "$RC" drop ""
    echo "cleaned $RC (backup at $RC.pre-codexhop.bak)"
  fi
  echo "uninstalled. Your saved accounts in \${CODEX_HOME:-~/.codex}/codexhop are untouched."
  exit 0
fi

mkdir -p "$BIN_DIR"
chmod 755 "$ROOT/codexhop.py"
rm -f "$BIN"
if [ "$MODE" = copy ]; then cp "$ROOT/codexhop.py" "$BIN"; else ln -s "$ROOT/codexhop.py" "$BIN"; fi
echo "installed $BIN"

if [ "$NO_RC" = 0 ]; then
  # Exactly one block in the rc file, whatever state it was in.
  rc_edit "$RC" add '# codexhop: hop OpenAI Codex between accounts
command -v codexhop >/dev/null 2>&1 && eval "$(codexhop shell-init)"'
  echo "added the shell glue to $RC; open a new terminal, or: source $RC"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: $BIN_DIR is not on your PATH yet. Add it in $RC." >&2 ;;
esac
