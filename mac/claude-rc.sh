#!/bin/zsh
# Usage: claude-rc.sh <folder> [name] [mode]   (called over SSH by Home Assistant, script.claude_rc_start)
# Home Assistant unlocks the login keychain first (security unlock-keychain), then this starts
# Claude Code Remote Control in a detached tmux session "claude-<name>" (the folder's *host*).
#
# Claude Code allows ONE `claude remote-control` per folder per device, but that one host serves up
# to 32 sessions: more sessions in the same folder are created from the app / claude.ai/code through
# the host's environment link (https://claude.ai/code?environment=env_...). When the host already
# runs, this prints that link instead of failing, so the HA notification offers "New session".
#
# mode = permission mode for new sessions (auto | acceptEdits | plan | default), or "resume"
# (claude remote-control --continue: reattach the last session recorded for this folder).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
log="$HOME/Library/Logs/claude-rc.log"
folder="${1:?folder required}"; name="${2:-$(basename "$folder")}"; mode="${3:-auto}"
[ -d "$folder" ] || { echo "no such folder: $folder"; exit 1; }
sess="claude-${name//[^a-zA-Z0-9_-]/_}"

strip() { LC_ALL=C sed 's/\x1b\[[0-9;]*[a-zA-Z]//g'; }
links() {  # prints "[Open session](...)" and "[New session in this folder](...)" from the host's screen
  local pane; pane=$(tmux capture-pane -p -e -J -S -2000 -t "$sess" 2>/dev/null)
  local l e; l=$(echo "$pane" | LC_ALL=C grep -o 'https://claude.ai/code/session_[A-Za-z0-9]*' | awk '!seen[$0]++' | head -1)
  e=$(echo "$pane" | LC_ALL=C grep -o 'https://claude.ai/code?environment=env_[A-Za-z0-9]*' | tail -1)
  [ -n "$l" ] && echo "[Open session]($l)"
  [ -n "$e" ] && echo "[New session in this folder]($e)"
  [ -n "$l$e" ] || echo "(no link yet - the dashboard shows it once the host connects)"
  return 0
}

if tmux has-session -t "$sess" 2>/dev/null; then
  echo "$sess already running (one host per folder, it serves up to 32 sessions)"
  links; exit 0
fi

case "$mode" in
  resume) args=(remote-control --chrome -c --name "$name") ;;
  auto|acceptEdits|plan|default|dontAsk|bypassPermissions) args=(remote-control --chrome --spawn same-dir --permission-mode "$mode" --name "$name") ;;
  *) echo "unknown mode: $mode"; exit 1 ;;
esac

# Drop the SSH marker vars: claude treats SSH_CONNECTION/SSH_CLIENT/SSH_TTY as "remote" and hides Claude in Chrome.
cmd="claude ${(j: :)${(q)args[@]}}"
tmux new-session -d -s "$sess" -c "$folder" \
  "env -u SSH_CONNECTION -u SSH_CLIENT -u SSH_TTY $cmd 2>&1 | tee -a '$log'; sleep 30"

# Wait for the direct session link (opens that session in the app without the same-dir/worktree question).
link=""
for i in {1..25}; do
  sleep 1
  pane=$(tmux capture-pane -p -e -J -S -2000 -t "$sess" 2>/dev/null)
  link=$(echo "$pane" | LC_ALL=C grep -o 'https://claude.ai/code/session_[A-Za-z0-9]*' | tail -1)
  [ -n "$link" ] && break
  echo "$pane" | LC_ALL=C grep -q 'already served\|Error:' && break
done
if echo "$pane" | LC_ALL=C grep -q 'already served'; then
  tmux kill-session -t "$sess" 2>/dev/null
  echo "this folder is already served by another claude remote-control (not started from here). Stop it first."; exit 1
fi
if tmux has-session -t "$sess" 2>/dev/null; then
  echo "started $sess in $folder ($mode)"
  links
else
  echo "FAILED: $sess exited"; tail -3 "$log" 2>/dev/null | strip
fi
