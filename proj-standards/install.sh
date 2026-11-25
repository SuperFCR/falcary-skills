#!/usr/bin/env bash
# Copy the proj-standards skills into an agent's skills directory.
# Usage: install.sh [--codex | --antigravity | --target DIR | --config-dir DIR ...] [--link-cli]
#   (no option)       ${CLAUDE_CONFIG_DIR:-~/.claude}/skills
#   --codex           ${CODEX_HOME:-~/.codex}/skills
#   --antigravity     ${ANTIGRAVITY_HOME:-~/.gemini/config}/skills
#   --target DIR      DIR itself (any agent that reads SKILL.md folders)
#   --config-dir DIR  DIR/skills; repeat once per Claude config dir (account)
#   --link-cli        symlink ~/.local/bin/repoguard to the installed repoguard.py (first target)
# An existing skill with the same name is moved to <target>.bak/<name>-<time>.
set -euo pipefail

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; }
usage_error() { echo "install.sh: $1" >&2; echo "Try: install.sh --help" >&2; exit 2; }
fail() { echo "install.sh: $1" >&2; exit 1; }

# Absolute path without trailing slashes; a quoted leading ~ is expanded too.
abspath() {
  local p
  p=$1
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

mode=default            # default | codex | antigravity | target | config
target_dir=""
config_dirs=()
link_cli=0

set_mode() {
  if [ "$mode" != default ] && [ "$mode" != "$1" ]; then
    usage_error "--codex, --antigravity, --target and --config-dir cannot be combined"
  fi
  mode=$1
}

while [ $# -gt 0 ]; do
  case $1 in
    --codex) set_mode codex ;;
    --antigravity) set_mode antigravity ;;
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
    -h|--help) usage; exit 0 ;;
    *) usage_error "unknown option: $1" ;;
  esac
  shift
done

targets=()
case $mode in
  default)
    targets=("$(abspath "${CLAUDE_CONFIG_DIR:-$HOME/.claude}")/skills") ;;
  codex)
    targets=("$(abspath "${CODEX_HOME:-$HOME/.codex}")/skills") ;;
  antigravity)
    targets=("$(abspath "${ANTIGRAVITY_HOME:-$HOME/.gemini/config}")/skills") ;;
  target)
    targets=("$target_dir") ;;
  config)
    for dir in "${config_dirs[@]}"; do targets+=("$dir/skills"); done ;;
esac

if ! command -v python3 >/dev/null 2>&1 ||
   ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
  echo "warning: repoguard needs python3 >= 3.9 on PATH; the skills are installed anyway" >&2
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
  if [ -f "$target/repo-hygiene/scripts/repoguard.py" ]; then
    chmod 755 "$target/repo-hygiene/scripts/repoguard.py"
  fi
  echo "${#skills[@]} skills -> $target"
}

for target in "${targets[@]}"; do install_into "$target"; done

repoguard_py="${targets[0]}/repo-hygiene/scripts/repoguard.py"
repoguard_cmd="python3 '$repoguard_py'"

if [ "$link_cli" = 1 ]; then
  [ -f "$repoguard_py" ] || fail "repoguard.py was not installed: $repoguard_py"
  bindir="$HOME/.local/bin"
  link="$bindir/repoguard"
  mkdir -p "$bindir"
  if [ -e "$link" ] && [ ! -L "$link" ]; then
    old="$link.bak-$stamp"
    n=1
    while [ -e "$old" ] || [ -L "$old" ]; do n=$((n + 1)); old="$link.bak-$stamp-$n"; done
    mv "$link" "$old"
    echo "backed up: $old"
  fi
  ln -sfn "$repoguard_py" "$link"
  echo "linked: $link -> $repoguard_py"
  repoguard_cmd=repoguard
  case ":$PATH:" in
    *":$bindir:"*) ;;
    *) echo "warning: $bindir is not on PATH; add it (export PATH=\"\$HOME/.local/bin:\$PATH\") or call $link directly" >&2 ;;
  esac
fi

echo
echo "Next steps (restart Claude Code / Codex / Antigravity to pick up skills):"
echo "  1. Validate a branch name:    $repoguard_cmd check-branch feat/my-feature"
echo "  2. Audit staged changes:      $repoguard_cmd check-stage"
echo "  3. Optional Git hooks (repo): $repoguard_cmd install-hooks"
if [ "$link_cli" = 0 ]; then
  echo "  Shorter command: rerun with --link-cli to link 'repoguard' into ~/.local/bin."
fi
