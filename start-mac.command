#!/bin/bash
# Double-click on macOS (or run ./start-mac.command)
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/"; open https://www.python.org/downloads/; read -p "Press Enter to close"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run - setting things up..."
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/flask --app outreach seed || { read -p "Setup failed. Press Enter"; exit 1; }
fi
.venv/bin/flask --app outreach worker &
WORKER=$!
trap "kill $WORKER" EXIT
(sleep 3; open http://localhost:5000) &
echo "Outreach running at http://localhost:5000 - keep this window open. Ctrl+C to stop."
.venv/bin/flask --app outreach run
