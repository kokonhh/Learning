"""Desktop launcher: double-click to run Outreach locally (used for the Outreach.exe build).

Runs the web UI and the sending engine in one process and opens the browser.
Data lives in %APPDATA%\\Outreach on Windows (~/.outreach elsewhere) so it
survives restarts and upgrades. Put a .env file in that folder to configure SMTP etc.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("APPDATA")
    d = Path(base) / "Outreach" if base else Path.home() / ".outreach"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def free_port(preferred: int = 5000) -> int:
    for port in [preferred] + list(range(5001, 5100)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def worker_loop(app, interval: int = 60) -> None:
    from outreach import engine, inbox
    from outreach.db import connect

    while True:
        conn = connect(app.config["DATABASE"])
        try:
            engine.process_due(conn, app.extensions["mailer"], app.config["SECRET_KEY"])
            try:
                inbox.check_inbox(conn)
            except Exception as exc:  # IMAP problems must not stop sending
                print(f"[inbox] {exc}")
        except Exception as exc:
            print(f"[worker] {exc}")
        finally:
            conn.close()
        time.sleep(interval)


def main() -> None:
    d = data_dir()
    load_env(d / ".env")
    os.environ.setdefault("OUTREACH_INSTANCE", str(d))
    os.environ.setdefault("OUTREACH_DB", str(d / "outreach.db"))
    os.environ.setdefault("OUTREACH_OUTBOX", str(d / "outbox"))
    first_run = not Path(os.environ["OUTREACH_DB"]).exists()

    from werkzeug.serving import make_server

    from outreach import create_app
    from outreach.db import connect, set_setting

    app = create_app()
    port = free_port()
    if first_run:
        from outreach.seed import seed
        conn = connect(app.config["DATABASE"])
        seed(conn)
        set_setting(conn, "base_url", f"http://localhost:{port}")
        conn.commit()
        conn.close()

    url = f"http://localhost:{port}"
    print("=" * 60)
    print(f"  Outreach is running at {url}")
    print(f"  Your data is stored in {d}")
    print("  KEEP THIS WINDOW OPEN while you use the app.")
    print("  Close this window to stop Outreach.")
    print("=" * 60, flush=True)

    threading.Thread(target=worker_loop, args=(app,), daemon=True).start()
    if not os.environ.get("OUTREACH_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    server = make_server("127.0.0.1", port, app, threaded=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # keep the window open so the user can read the error
        import traceback
        traceback.print_exc()
        input(f"\nOutreach could not start: {exc}\nPress Enter to close...")
        sys.exit(1)
