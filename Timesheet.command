#!/bin/bash
# QBO Timesheet launcher — double-click to update, start, and open the app.
# Keep this file on your Desktop; it updates itself from the repo.

REPO_URL="https://github.com/murumoto-rgb/Timesheet.git"
BRANCH="main"
APP_DIR="$HOME/TimesheetApp"
PORT=8000

pause_exit() { echo; read -r -p "Press Return to close this window… " _; exit "${1:-1}"; }

echo "── QBO Timesheet ──────────────────────────────"

# Already running? Just open it.
if curl -s -o /dev/null --max-time 2 "http://localhost:$PORT/api/status"; then
  echo "App is already running — opening it."
  open "http://localhost:$PORT"
  exit 0
fi

# git comes with Apple's developer tools; trigger the installer if missing.
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Your Mac needs Apple's command-line developer tools (one time)."
  echo "An install dialog will appear — click Install, wait for it to finish,"
  echo "then double-click Timesheet again."
  xcode-select --install >/dev/null 2>&1
  pause_exit 0
fi

# Get or update the code.
if [ -d "$APP_DIR/.git" ]; then
  echo "Checking for updates…"
  if git -C "$APP_DIR" fetch --quiet origin "$BRANCH"; then
    if [ "$(git -C "$APP_DIR" branch --show-current)" != "$BRANCH" ]; then
      echo "This copy is on another branch — preserving it and skipping update."
    elif [ -z "$(git -C "$APP_DIR" status --porcelain)" ]; then
      if git -C "$APP_DIR" merge --ff-only --quiet "origin/$BRANCH"; then
        echo "Up to date: $(git -C "$APP_DIR" log -1 --format='%s')"
      else
        echo "Local branch needs review — update was not applied; starting the existing copy."
      fi
    else
      echo "Local edits detected — preserving them and skipping update."
    fi
  else
    echo "(Couldn't reach GitHub — starting the copy you already have.)"
  fi
else
  echo "First run — downloading the app…"
  git clone --quiet -b "$BRANCH" "$REPO_URL" "$APP_DIR" || {
    echo "Download failed. Are you online? Is the repo public?"; pause_exit 1; }
fi
cd "$APP_DIR" || pause_exit 1

# If the repo has a newer launcher, replace this file and restart.
SELF="${BASH_SOURCE[0]}"
if [ -f "$APP_DIR/Timesheet.command" ] && ! cmp -s "$APP_DIR/Timesheet.command" "$SELF"; then
  cp "$APP_DIR/Timesheet.command" "$SELF.new" && mv "$SELF.new" "$SELF" && chmod +x "$SELF"
  echo "Launcher updated — restarting it…"
  exec /bin/bash "$SELF"
fi

# Never move an old connection without its matching save journal. A silent
# first-match copy can also select the wrong company when old copies coexist.
if [ ! -f .env ]; then
  for d in "$HOME/Desktop" "$HOME/Downloads" "$HOME"; do
    old=$(find "$d" -maxdepth 3 -name ".env" -path "*Timesheet*" ! -path "$APP_DIR/*" 2>/dev/null | head -1)
    [ -n "$old" ] && break
  done
  if [ -n "$old" ]; then
    olddir=$(dirname "$old")
    echo "An older Timesheet installation was found at:"
    echo "$olddir"
    echo "No settings or accounting data have been copied, and the app has not started."
    echo "Keep using your existing copy until its connection and save history can"
    echo "be moved together with both copies stopped. Do not reconnect this empty copy."
    echo "In Codex, ask: Move my old Timesheet installation safely to $APP_DIR."
    pause_exit 1
  else
    cp .env.example .env
    echo "One-time setup: paste your Intuit Client ID and Secret into the file"
    echo "that just opened, save it (Cmd+S), close it, then double-click"
    echo "Timesheet again."
    open -e .env
    pause_exit 0
  fi
fi

# Use the tested Python minor version (a newer security patch is also accepted).
TIMESHEET_REQUIRED_PYTHON=$(cat .python-version)
TIMESHEET_PYTHON=$(command -v python3.13 || command -v python3 || true)
timesheet_python_compatible() {
  "$1" -c 'import sys; required=tuple(map(int,sys.argv[1].split("."))); sys.exit(not (sys.version_info[:2] == required[:2] and sys.version_info[:3] >= required))' "$TIMESHEET_REQUIRED_PYTHON" >/dev/null 2>&1
}
if [ -z "$TIMESHEET_PYTHON" ] || ! timesheet_python_compatible "$TIMESHEET_PYTHON"; then
  echo "Timesheet needs Python $TIMESHEET_REQUIRED_PYTHON or a newer Python 3.13 patch."
  echo "Install Python 3.13 from https://www.python.org/downloads/macos/"
  echo "then close this window and double-click Timesheet again."
  pause_exit 1
fi
if [ -d .venv ] && ! timesheet_python_compatible ./.venv/bin/python; then
  TIMESHEET_OLD_VENV=$(mktemp -d "$APP_DIR/.venv.previous.XXXXXX") || pause_exit 1
  mv .venv "$TIMESHEET_OLD_VENV/venv" || pause_exit 1
  echo "Preserved the old Python environment in $TIMESHEET_OLD_VENV; rebuilding it."
fi
[ -d .venv ] || { echo "Setting up the private Python environment…"; "$TIMESHEET_PYTHON" -m venv .venv || pause_exit 1; }
REQ_HASH=$(cat requirements.txt requirements.lock .python-version | shasum -a 256 | awk '{print $1}')
INSTALLED_HASH=$([ -f .venv/.requirements-sha ] && head -1 .venv/.requirements-sha)
if [ "$REQ_HASH" != "$INSTALLED_HASH" ]; then
  ./.venv/bin/pip install --quiet -r requirements.txt -c requirements.lock || {
    echo "Dependency install failed — check your internet connection."; pause_exit 1; }
  printf '%s\n' "$REQ_HASH" > .venv/.requirements-sha
fi

# Open the browser once the server is up, then run the server in this window.
( for _ in $(seq 1 30); do
    curl -s -o /dev/null --max-time 1 "http://localhost:$PORT/api/status" && break
    sleep 0.5
  done
  open "http://localhost:$PORT" ) &

echo
echo "Timesheet is running. KEEP THIS WINDOW OPEN while you use the app."
echo "Close the window (or press Ctrl+C) to stop it."
echo "───────────────────────────────────────────────"
exec ./.venv/bin/python -m uvicorn main:app --port "$PORT" --workers 1
