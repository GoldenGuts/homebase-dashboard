#!/bin/zsh
# Installs the Mac-side helpers that Home Assistant calls over SSH (packages/mac.yaml) into ~/bin.
set -e
cd "$(dirname "$0")"
mkdir -p ~/bin ~/Library/Caches/mac-dash
cp claude-rc.sh mac-action.sh mac-status.py ~/bin/
chmod +x ~/bin/claude-rc.sh ~/bin/mac-action.sh ~/bin/mac-status.py
rm -f ~/bin/mac-status.sh   # replaced by mac-status.py
echo "installed to ~/bin"; ls -la ~/bin/claude-rc.sh ~/bin/mac-action.sh ~/bin/mac-status.py
