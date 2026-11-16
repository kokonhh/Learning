import os
import re

import pytest

from outreach import create_app
from outreach.db import connect, set_setting
from outreach.mailer import DryRunMailer


@pytest.fixture
def app(tmp_path):
    mailer = DryRunMailer(str(tmp_path / "outbox"))
    app = create_app({
        "DATABASE": str(tmp_path / "test.db"),
        "SECRET_KEY": "test-secret",
        "MAILER": mailer,
        "API_KEY": "k3y",
        "SEED_LIBRARY": False,
    })
    conn = connect(app.config["DATABASE"])
    # always inside the send window during tests
    for k, v in {"send_window_start": "0", "send_window_end": "24", "send_weekdays_only": "0",
                 "sender_email": "rep@seller.test", "sender_name": "Rita Rep", "company_name": "SellerCo",
                 "base_url": "https://out.seller.test"}.items():
        set_setting(conn, k, v)
    conn.commit()
    conn.close()
    return app


@pytest.fixture
def conn(app):
    c = connect(app.config["DATABASE"])
    yield c
    c.close()


@pytest.fixture
def mailer(app):
    return app.extensions["mailer"]


class CsrfClient:
    """Test client that fetches a CSRF token once and adds it to every POST."""

    def __init__(self, client):
        self.c = client
        html = client.get("/settings").data.decode()
        self.token = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)

    def get(self, *a, **kw):
        return self.c.get(*a, **kw)

    def post(self, url, data=None, json=None, **kw):
        if json is not None:
            headers = kw.pop("headers", {})
            headers["X-CSRF-Token"] = self.token
            return self.c.post(url, json=json, headers=headers, **kw)
        data = dict(data or {})
        data["csrf_token"] = self.token
        return self.c.post(url, data=data, **kw)


@pytest.fixture
def client(app):
    return CsrfClient(app.test_client())


def make_contact(conn, email="jane@acme.test", first="Jane", company="Acme", **extra):
    from outreach import importer
    cols = {"email": email, "first_name": first, "company": company, **extra}
    header = ",".join(cols)
    row = ",".join(str(v) for v in cols.values())
    res = importer.import_csv(conn, f"{header}\n{row}\n")
    return res["contact_ids"][0]


def make_sequence(conn, steps, status="active", name="Test seq"):
    sid = conn.execute("INSERT INTO sequences(name, status) VALUES (?, ?)", (name, status)).lastrowid
    for pos, s in enumerate(steps, start=1):
        conn.execute(
            "INSERT INTO sequence_steps(sequence_id, position, step_type, delay_days, subject, body, thread_reply, "
            "instructions) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, pos, s.get("type", "email"), s.get("delay", 0), s.get("subject"), s.get("body"),
             s.get("thread", 0), s.get("instructions")))
    conn.commit()
    return sid
