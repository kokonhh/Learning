import re
from email.message import EmailMessage
from urllib.parse import unquote

from outreach import engine, inbox

from .conftest import make_contact, make_sequence


def send_first(conn, mailer, **contact):
    cid = make_contact(conn, **contact)
    sid = make_sequence(conn, [{"subject": "Hello", "body": "Hi {{first_name}} see https://seller.test/case?x=1&y=2"},
                               {"delay": 3, "body": "Following up", "thread": 1}])
    engine.enroll(conn, sid, [cid])
    engine.process_due(conn, mailer, "test-secret")
    token = conn.execute("SELECT token FROM messages").fetchone()[0]
    return cid, token, mailer.sent[-1].get_body(("html",)).get_content()


def test_all_pages_render(client, conn, mailer):
    send_first(conn, mailer)
    for url in ["/", "/contacts", "/contacts/1", "/contacts/import", "/companies", "/templates", "/templates/new",
                "/sequences", "/sequences/1", "/tasks", "/pipeline", "/deals/new", "/settings", "/suppressions",
                "/contacts/export.csv"]:
        assert client.get(url).status_code == 200, url


def test_post_without_csrf_is_rejected(app):
    assert app.test_client().post("/contacts/new", data={"email": "x@y.test"}).status_code == 400


def test_open_and_click_tracking(app, client, conn, mailer):
    cid, token, html = send_first(conn, mailer)
    assert client.get(f"/t/o/{token}.gif").headers["Content-Type"] == "image/gif"
    href = re.search(r'href="(https://out\.seller\.test/t/c/[^"]+)"', html).group(1).replace("&amp;", "&")
    path = href.replace("https://out.seller.test", "")
    r = client.get(path)
    assert r.status_code == 302 and r.headers["Location"] == "https://seller.test/case?x=1&y=2"
    m = conn.execute("SELECT open_count, click_count FROM messages").fetchone()
    assert tuple(m) == (1, 1)
    assert conn.execute("SELECT status FROM contacts").fetchone()[0] == "engaged"
    # tampered destination => no open redirect
    evil = path.split("?")[0] + "?u=" + "https%3A%2F%2Fevil.test" + "&s=" + unquote(path.split("s=")[1])
    assert client.get(evil).status_code == 400


def test_unsubscribe_one_click_and_page(app, conn, mailer):
    cid, token, _ = send_first(conn, mailer)
    c = app.test_client()
    assert c.get(f"/u/{token}").status_code == 200
    r = c.post(f"/u/{token}", data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200
    assert conn.execute("SELECT status FROM contacts").fetchone()[0] == "unsubscribed"
    assert conn.execute("SELECT reason FROM suppressions").fetchone()[0] == "unsubscribed"
    assert conn.execute("SELECT status FROM enrollments").fetchone()[0] == "unsubscribed"


def test_contact_crud_bulk_enroll_and_run(client, conn, mailer):
    r = client.post("/contacts/new", data={"email": "New@Lead.test", "first_name": "Nia", "company": "Lead Co",
                                           "custom_fields": "pain_point: hiring"}, follow_redirects=True)
    assert r.status_code == 200 and b"Nia" in r.data
    cid = conn.execute("SELECT id FROM contacts WHERE email = 'new@lead.test'").fetchone()[0]
    sid = make_sequence(conn, [{"subject": "Re {{pain_point}}", "body": "Hi {{first_name}}"}])
    client.post("/contacts/bulk", data={"ids": [str(cid)], "action": "enroll", "sequence_id": str(sid)})
    assert conn.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0] == 1
    client.post("/run")
    assert mailer.sent[-1]["Subject"] == "Re hiring"


def test_csv_import_with_enroll(client, conn):
    sid = make_sequence(conn, [{"subject": "S", "body": "B"}])
    r = client.post("/contacts/import", data={"csv_text": "email,first name\nq@w.test,Q\nbad,\n",
                                              "sequence_id": str(sid), "update_existing": "1"})
    assert r.status_code == 200 and b"Import complete" in r.data
    assert conn.execute("SELECT COUNT(*) FROM enrollments").fetchone()[0] == 1


def test_sequence_builder_and_step_reorder(client, conn):
    client.post("/sequences", data={"name": "Built in UI"})
    sid = conn.execute("SELECT id FROM sequences WHERE name = 'Built in UI'").fetchone()[0]
    client.post(f"/sequences/{sid}/steps", data={"step_type": "email", "delay_days": "0", "subject": "A", "body": "a"})
    client.post(f"/sequences/{sid}/steps", data={"step_type": "call", "delay_days": "2", "instructions": "ring"})
    steps = conn.execute("SELECT id, step_type FROM sequence_steps WHERE sequence_id = ? ORDER BY position",
                         (sid,)).fetchall()
    assert [s[1] for s in steps] == ["email", "call"]
    client.post(f"/sequences/{sid}/steps/{steps[1][0]}", data={"action": "up"})
    order = [r[0] for r in conn.execute("SELECT step_type FROM sequence_steps WHERE sequence_id = ? ORDER BY position",
                                        (sid,))]
    assert order == ["call", "email"]
    client.post(f"/sequences/{sid}/settings", data={"action": "activate"})
    assert conn.execute("SELECT status FROM sequences WHERE id = ?", (sid,)).fetchone()[0] == "active"


def test_pipeline_stage_move(client, conn):
    cid = make_contact(conn)
    client.post("/deals/new", data={"name": "Big deal", "contact_id": str(cid), "stage": "lead", "value": "5000"})
    did = conn.execute("SELECT id FROM deals").fetchone()[0]
    r = client.post(f"/ui/deals/{did}/stage", json={"stage": "won"})
    assert r.status_code == 200 and r.get_json()["summary"]["won_value"] == 5000
    assert conn.execute("SELECT status FROM contacts").fetchone()[0] == "customer"
    assert client.post(f"/ui/deals/{did}/stage", json={"stage": "nope"}).status_code == 400


def test_preview_endpoint(client, conn):
    make_contact(conn)
    d = client.post("/ui/preview", json={"subject": "Hi {{first_name}}", "body": "{{title}}"}).get_json()
    assert d["subject"] == "Hi Jane" and d["missing"] == ["title"]


def test_api_requires_key_and_creates_leads(app, conn):
    c = app.test_client()
    assert c.post("/api/v1/leads", json={"email": "z@z.test"}).status_code == 401
    sid = make_sequence(conn, [{"subject": "S", "body": "B"}])
    r = c.post("/api/v1/leads", json={"email": "z@z.test", "first_name": "Zed", "company": "Zco",
                                      "budget": "10k", "sequence_id": sid}, headers={"X-API-Key": "k3y"})
    assert r.status_code == 201 and r.get_json()["enrolled"]["enrolled"] == 1
    r = c.post("/api/v1/events", json={"email": "z@z.test", "type": "meeting"}, headers={"X-API-Key": "k3y"})
    assert r.get_json()["ok"]
    assert conn.execute("SELECT status FROM contacts").fetchone()[0] == "meeting"
    assert conn.execute("SELECT status FROM enrollments").fetchone()[0] == "replied"


def test_password_protection(tmp_path):
    from outreach import create_app
    app = create_app({"DATABASE": str(tmp_path / "p.db"), "SECRET_KEY": "s", "PASSWORD": "pw", "SEED_LIBRARY": False})
    c = app.test_client()
    assert c.get("/contacts").status_code == 302
    page = c.get("/login").data.decode()
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    c.post("/login", data={"password": "pw", "csrf_token": token})
    assert c.get("/contacts").status_code == 200


# ------------------------------------------------------------------ inbox parsing

def _mail(frm, subject, body, **headers):
    m = EmailMessage()
    m["From"], m["Subject"] = frm, subject
    for k, v in headers.items():
        m[k.replace("_", "-")] = v
    m.set_content(body)
    return m


def test_inbox_reply_autoreply_and_bounce(conn, mailer):
    cid, _, _ = send_first(conn, mailer)
    assert inbox.handle_message(conn, _mail("Jane <JANE@acme.test>", "Out of Office", "away",
                                            Auto_Submitted="auto-replied")) == "auto_reply"
    assert conn.execute("SELECT status FROM enrollments").fetchone()[0] == "active"  # auto-replies don't stop
    assert inbox.handle_message(conn, _mail("Jane <jane@acme.test>", "Re: Hello", "Sounds good, Tuesday?")) == "reply"
    assert conn.execute("SELECT status FROM contacts").fetchone()[0] == "replied"
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE direction = 'in'").fetchone()[0] == 1

    other = make_contact(conn, email="gone@acme.test")
    dsn = _mail("Mail Delivery Subsystem <mailer-daemon@mx.test>", "Delivery Status Notification (Failure)",
                "Final-Recipient: rfc822; gone@acme.test\nStatus: 5.1.1")
    assert inbox.handle_message(conn, dsn) == "bounce"
    assert conn.execute("SELECT status FROM contacts WHERE id = ?", (other,)).fetchone()[0] == "bounced"
    assert conn.execute("SELECT reason FROM suppressions WHERE email = 'gone@acme.test'").fetchone()[0] == "bounced"
