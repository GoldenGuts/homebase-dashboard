#!/bin/zsh
# Usage: mac-action.sh <action> [arg1] [arg2]
# Called over SSH by Home Assistant (shell_command.mac_* in packages/mac.yaml). Fixed actions only.
# Starting Claude needs the keychain, so that goes through claude-rc.sh after an unlock.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
action="${1:?action required}"; a1="$2"; a2="$3"
apps=(Antigravity Cursor Zed "Google Chrome" Ghostty Finder)   # allowed GUI apps (mac-status.py reports the first five)
cache="$HOME/Library/Caches/mac-dash"; mkdir -p "$cache"
ha_host="ha"   # ~/.ssh/config alias on the Mac for the Home Assistant host (used to copy screenshots)

case "$action" in
  stop-claude)
    sess="claude-${a1//[^a-zA-Z0-9_-]/_}"
    tmux kill-session -t "$sess" 2>/dev/null && echo "stopped $sess" || echo "$sess not running" ;;
  stop-all-claude)
    n=0; for s in $(tmux ls -F '#{session_name}' 2>/dev/null | grep '^claude-'); do tmux kill-session -t "$s" && n=$((n+1)); done
    echo "stopped $n session(s)" ;;
  update-claude)   # background `claude update` (can take > HA's 60 s shell_command limit); poll with update-claude-log
    # Running hosts keep their own versioned binary (and so do the sessions they spawn): stop + start them after this.
    log="$cache/claude-update.log"
    pgrep -qf 'claude update' && { echo "update already running"; exit 0; }
    { echo "from $(claude --version 2>/dev/null)"; claude update 2>&1; echo "now $(claude --version 2>/dev/null)"; echo "DONE"; } </dev/null 2>&1 \
      | LC_ALL=C sed 's/\x1b\[[0-9;]*[a-zA-Z]//g' > "$log" 2>&1 &!
    echo "update started" ;;
  update-claude-log)
    cat "$cache/claude-update.log" 2>/dev/null || echo "no update log" ;;
  open-app)   # open-app <AppName> [folder]
    [[ " ${(j: :)apps} " == *" $a1 "* ]] || { echo "app not allowed: $a1"; exit 1; }
    if [ -n "$a2" ] && [ "$a1" != "Google Chrome" ]; then
      [ -d "$a2" ] || { echo "no such folder: $a2"; exit 1; }; open -a "$a1" "$a2"
    else open -a "$a1"; fi
    echo "opened $a1${a2:+ in $a2}" ;;
  quit-app)
    [[ " ${(j: :)apps} " == *" $a1 "* ]] || { echo "app not allowed: $a1"; exit 1; }
    osascript -e "quit app \"$a1\"" && echo "quit $a1" ;;
  sleep)        pmset sleepnow ;;
  display-off)  pmset displaysleepnow && echo "display off" ;;
  lock)         osascript -e 'tell application "System Events" to keystroke "q" using {command down, control down}' && echo "locked" ;;
  caffeinate)   # caffeinate <hours>
    h="${a1:-2}"; [[ "$h" == <1-24> ]] || h=2
    pkill -x caffeinate; nohup caffeinate -dims -t $((h*3600)) >/dev/null 2>&1 &
    echo $(( $(date +%s) + h*3600 )) > "$cache/caffeinate_until"
    echo "awake for ${h}h" ;;
  decaffeinate) pkill -x caffeinate && echo "caffeinate stopped" || echo "was not running"; rm -f "$cache/caffeinate_until" ;;
  screenshot)   # main display -> 1600px jpg -> HA /config/mac/screen.jpg (camera.mac_screen, local_file)
    f="$cache/screen.jpg"; t="$cache/screen.new.jpg"; rm -f "$t"
    # Needs Screen Recording permission for ssh sessions (one-time, in System Settings > Privacy & Security
    # > Screen Recording: add /usr/libexec/sshd-keygen-wrapper). Without it macOS says "could not create image".
    /usr/sbin/screencapture -x -t jpg -r "$t" 2>/dev/null
    [ -s "$t" ] || { echo "screencapture failed: allow Screen Recording for /usr/libexec/sshd-keygen-wrapper in System Settings > Privacy & Security"; exit 1; }
    sips -Z 1600 "$t" >/dev/null 2>&1; mv -f "$t" "$f"
    scp -q -o BatchMode=yes -o ConnectTimeout=5 "$f" "$ha_host:/config/mac/screen.jpg" && echo "screen captured $(date '+%H:%M:%S')" || echo "captured, but copy to HA failed" ;;
  open-url)     # open-url <http(s)://...>
    [[ "$a1" == http://* || "$a1" == https://* ]] || { echo "not a web link: $a1"; exit 1; }
    open "$a1" && echo "opened link" ;;
  say)          # say <text>  (max 300 chars, spoken on the Mac)
    t="${a1:0:300}"; [ -n "$t" ] || { echo "nothing to say"; exit 1; }
    say -- "$t" && echo "said it" ;;
  clip)         # clip <text> -> clipboard
    [ -n "$a1" ] || { echo "nothing to copy"; exit 1; }
    printf %s "$a1" | pbcopy && echo "copied ${#a1} chars" ;;
  status)       ~/bin/mac-status.py ;;
  *)            echo "unknown action: $action"; exit 1 ;;
esac
