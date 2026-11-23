#!/usr/bin/env bash
# Copy the project-memory skills into a skills directory.
# Usage: install.sh [--codex | --target DIR | --config-dir DIR ...] [--link-cli] [--wire]
#   (no option)       ${CLAUDE_CONFIG_DIR:-~/.claude}/skills
#   --codex           ${CODEX_HOME:-~/.codex}/skills
#   --target DIR      DIR itself (any agent that reads SKILL.md folders)
#   --config-dir DIR  DIR/skills; repeat once per Claude config dir (account)
#   --link-cli        symlink ~/.local/bin/pmem to the installed pmem.py (first target)
#   --wire            add the project-memory block to the global instruction file:
#                     AGENTS.md for --codex, CLAUDE.md of each Claude config dir
# An existing skill with the same name is moved to <target>.bak/<name>-<time>.
set -euo pipefail

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }
usage_error() { echo "install.sh: $1" >&2; echo "Try: install.sh --help" >&2; exit 2; }
fail() { echo "install.sh: $1" >&2; exit 1; }

# Absolute path without trailing slashes; a quoted leading ~ is expanded too.
abspath() {
  local p=$1
  case $p in
    \~) p=$HOME ;;
    \~/*) p="$HOME/${p#\~/}" ;;
  esac
  case $p in
    /*) ;;
    *) p="$PWD/$p" ;;
  esac
  while [ "${#p}" -gt 1 ] && [ "${p%/}" != "$p" ]; do p=${p%/}; done
  printf '%s\n' "$p"
}

: "${HOME:?HOME is not set}"
unset CDPATH
src=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

mode=default            # default | codex | target | config
target_dir=""
config_dirs=()
link_cli=0
wire=0

set_mode() {
  if [ "$mode" != default ] && [ "$mode" != "$1" ]; then
    usage_error "--codex, --target and --config-dir cannot be combined"
  fi
  mode=$1
}

while [ $# -gt 0 ]; do
  case $1 in
    --codex) set_mode codex ;;
    --target)
      [ $# -ge 2 ] && [ -n "$2" ] || usage_error "--target needs a directory"
      [ -z "$target_dir" ] || usage_error "--target can be given only once"
      set_mode target
      target_dir=$(abspath "$2")
      shift ;;
    --config-dir)
      [ $# -ge 2 ] && [ -n "$2" ] || usage_error "--config-dir needs a directory"
      set_mode config
      dir=$(abspath "$2")
      seen=0
      for known in ${config_dirs[@]+"${config_dirs[@]}"}; do
        if [ "$known" = "$dir" ]; then seen=1; fi
      done
      if [ "$seen" = 0 ]; then config_dirs+=("$dir"); fi
      shift ;;
    --link-cli) link_cli=1 ;;
    --wire) wire=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage_error "unknown option: $1" ;;
  esac
  shift
done

targets=()
claude_dirs=()
case $mode in
  default)
    claude_dirs=("$(abspath "${CLAUDE_CONFIG_DIR:-$HOME/.claude}")")
    targets=("${claude_dirs[0]}/skills") ;;
  codex)
    targets=("$(abspath "${CODEX_HOME:-$HOME/.codex}")/skills") ;;
  target)
    targets=("$target_dir") ;;
  config)
    claude_dirs=("${config_dirs[@]}")
    for dir in "${claude_dirs[@]}"; do targets+=("$dir/skills"); done ;;
esac

if [ "$wire" = 1 ] && [ "$mode" = target ]; then
  usage_error "--wire needs --codex or a Claude config dir; for another agent run 'pmem wire FILE' yourself"
fi

python_ok=0
if command -v python3 >/dev/null 2>&1 &&
   python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
  python_ok=1
fi
if [ "$python_ok" = 0 ]; then
  [ "$wire" = 0 ] || fail "--wire needs python3 >= 3.9 on PATH"
  echo "warning: pmem needs python3 >= 3.9 on PATH; the skills are installed anyway" >&2
fi

skills=()
for dir in "$src"/skills/*/; do
  if [ -f "${dir}SKILL.md" ]; then skills+=("$(basename "$dir")"); fi
done
[ "${#skills[@]}" -gt 0 ] || fail "no skills found under $src/skills"
source_skills=$(cd "$src/skills" && pwd -P)

stamp=$(date +%Y%m%d%H%M%S)

# Move <dir>/<name> to <dir>.bak/<name>-<stamp>, outside the directory agents scan.
backup() {
  local bak="$1.bak" dest n=1
  mkdir -p "$bak"
  dest="$bak/$2-$stamp"
  while [ -e "$dest" ] || [ -L "$dest" ]; do
    n=$((n + 1))
    dest="$bak/$2-$stamp-$n"
  done
  mv "$1/$2" "$dest"
  echo "backed up: $dest"
}

install_into() {
  local target=$1 name dest
  mkdir -p "$target"
  if [ "$(cd "$target" && pwd -P)" = "$source_skills" ]; then
    fail "target is the source checkout itself: $target"
  fi
  for name in "${skills[@]}"; do
    dest="$target/$name"
    if [ -e "$dest" ] || [ -L "$dest" ]; then backup "$target" "$name"; fi
    cp -R "$src/skills/$name" "$dest"
    find "$dest" -name __pycache__ -type d -prune -exec rm -rf {} +
    echo "installed: $dest"
  done
  if [ -f "$target/project-memory/scripts/pmem.py" ]; then
    chmod 755 "$target/project-memory/scripts/pmem.py"
  fi
  echo "${#skills[@]} skills -> $target"
}

for target in "${targets[@]}"; do install_into "$target"; done

pmem_py="${targets[0]}/project-memory/scripts/pmem.py"
pmem="python3 '$pmem_py'"

if [ "$link_cli" = 1 ]; then
  [ -f "$pmem_py" ] || fail "pmem.py was not installed: $pmem_py"
  bindir="$HOME/.local/bin"
  link="$bindir/pmem"
  mkdir -p "$bindir"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    old="$link.bak-$stamp"
    n=1
    while [ -e "$old" ] || [ -L "$old" ]; do n=$((n + 1)); old="$link.bak-$stamp-$n"; done
    mv "$link" "$old"
    echo "backed up: $old"
  fi
  ln -sfn "$pmem_py" "$link"
  echo "linked: $link -> $pmem_py"
  pmem=pmem
  case ":$PATH:" in
    *":$bindir:"*) ;;
    *) echo "warning: $bindir is not on PATH; add it (export PATH=\"\$HOME/.local/bin:\$PATH\") or call $link directly" >&2 ;;
  esac
fi

if [ "$wire" = 1 ]; then
  if [ "$mode" = codex ]; then
    python3 "$pmem_py" wire codex || fail "pmem wire codex failed (see above)"
  else
    wire_args=()
    for dir in "${claude_dirs[@]}"; do wire_args+=(--config-dir "$dir"); done
    python3 "$pmem_py" wire claude "${wire_args[@]}" || fail "pmem wire claude failed (see above)"
  fi
fi

echo
echo "Next steps (restart Claude Code / Codex first so they load the skills):"
echo "  1. In each project:          cd <project> && $pmem init"
echo "  2. Bring old Claude memory:  $pmem import --claude   (add --config-dir DIR for other accounts)"
if [ "$mode" = codex ] && [ "$wire" = 1 ]; then
  echo "  3. Codex: done (AGENTS.md wired); launch Codex from the project root or add --add-dir <project root>"
else
  echo "  3. Let Codex find it:        $pmem wire codex   (once per CODEX_HOME)"
fi
if [ "$mode" = default ] || [ "$mode" = config ]; then
  echo "  Another Claude config dir or account? Rerun with --config-dir DIR (repeatable)."
fi
if [ "$link_cli" = 0 ]; then
  echo "  Shorter command: rerun with --link-cli to get 'pmem' in ~/.local/bin."
fi
