#!/bin/zsh
# macOS: double-click to start the menu-bar app in the background, using the
# uv virtualenv. The Terminal window can be closed afterwards.
cd "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  echo "No .venv found. Run 'uv sync' in this folder first."
  exit 1
fi
nohup .venv/bin/python tray_app.py >/dev/null 2>&1 &
disown
echo "Sony CH720N Auto-Off started (look for the headphone icon in the menu bar)."
