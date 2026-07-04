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
    git -C "$APP_DIR" reset --hard --quiet "origin/$BRANCH"
    echo "Up to date: $(git -C "$APP_DIR" log -1 --format='%s')"
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

# One-time: adopt an .env (and QuickBooks connection) from an older download.
if [ ! -f .env ]; then
  for d in "$HOME/Desktop" "$HOME/Downloads" "$HOME"; do
    old=$(find "$d" -maxdepth 3 -name ".env" -path "*Timesheet*" ! -path "$APP_DIR/*" 2>/dev/null | head -1)
    [ -n "$old" ] && break
  done
  if [ -n "$old" ]; then
    cp "$old" .env
    olddir=$(dirname "$old")
    [ -f "$olddir/qbo_tokens.json" ] && cp "$olddir/qbo_tokens.json" qbo_tokens.json
    echo "Copied your settings from $olddir"
  else
    cp .env.example .env
    echo "One-time setup: paste your Intuit Client ID and Secret into the file"
    echo "that just opened, save it (Cmd+S), close it, then double-click"
    echo "Timesheet again."
    open -e .env
    pause_exit 0
  fi
fi

# Python check + private virtualenv for dependencies.
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is missing. Install it from https://www.python.org/downloads/"
  echo "then double-click Timesheet again."
  pause_exit 1
fi
[ -d .venv ] || { echo "Setting up (first run only)…"; python3 -m venv .venv; }
./.venv/bin/pip install --quiet -r requirements.txt || {
  echo "Dependency install failed — check your internet connection."; pause_exit 1; }

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
exec ./.venv/bin/python -m uvicorn main:app --port "$PORT"
