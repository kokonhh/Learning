import json

from outreach import importer, templating


def ctx(**kw):
    base = {"first_name": "Jane", "custom_fields": {"pain_point": "slow follow-ups"}}
    base.update(kw)
    return templating.build_context(base, {"name": "Acme"}, {"sender_name": "Rita", "company_name": "SellerCo"})


def test_merge_fields_fallbacks_and_missing():
    out, missing = templating.render("Hi {{first_name}} at {{company}} — {{pain_point}}", ctx())
    assert out == "Hi Jane at Acme — slow follow-ups" and missing == []

    out, missing = templating.render("Hi {{first_name|there}}, {{title}}", ctx(first_name=""))
    assert out == "Hi there, " and missing == ["title"]

    out, _ = templating.render("{{custom.pain_point}} / {{sender_company}}", ctx())
    assert out == "slow follow-ups / SellerCo"


def test_spintax_is_deterministic_per_contact():
    a, _ = templating.render("[[Hi|Hey|Hello]]", ctx(), seed="1")
    b, _ = templating.render("[[Hi|Hey|Hello]]", ctx(), seed="1")
    assert a == b and a in ("Hi", "Hey", "Hello")
    seen = {templating.render("[[Hi|Hey|Hello]]", ctx(), seed=str(i))[0] for i in range(30)}
    assert len(seen) > 1


def test_text_to_html_escapes_and_links():
    html = templating.text_to_html("a <b>\n\nsee https://x.test/p?a=1&b=2")
    assert "&lt;b&gt;" in html
    assert '<a href="https://x.test/p?a=1&amp;b=2">' in html
    assert html.count("<p>") == 2


def test_header_mapping():
    m = importer.map_headers(["Email Address", "First Name", "Organization", "Job Title", "Pain Point"])
    assert m == {"Email Address": "email", "First Name": "first_name", "Organization": "company",
                 "Job Title": "title", "Pain Point": "custom:Pain Point"}


def test_import_creates_updates_and_skips(conn):
    csv1 = ("Email,Name,Company,Employees,Tags,pain_point\n"
            "a@x.test,Ann Lee,Xco,120,\"T1, T2\",churn\n"
            "not-an-email,Bob,Yco,,,\n")
    r = importer.import_csv(conn, csv1, extra_tags="webinar")
    assert r["created"] == 1 and r["skipped"] == 1
    c = conn.execute("SELECT * FROM contacts WHERE email = 'a@x.test'").fetchone()
    assert (c["first_name"], c["last_name"]) == ("Ann", "Lee")
    assert c["tags"] == "t1,t2,webinar"
    assert json.loads(c["custom_fields"]) == {"pain_point": "churn"}
    co = conn.execute("SELECT * FROM companies WHERE id = ?", (c["company_id"],)).fetchone()
    assert co["name"] == "Xco" and co["size"] == "51-200"

    r2 = importer.import_csv(conn, "email,title\nA@X.test,VP Sales\n")
    assert r2["updated"] == 1 and r2["created"] == 0
    c = conn.execute("SELECT * FROM contacts WHERE email = 'a@x.test'").fetchone()
    assert c["title"] == "VP Sales" and c["first_name"] == "Ann"  # blanks don't overwrite
    assert c["score"] > 0


def test_import_requires_email_column(conn):
    r = importer.import_csv(conn, "name,company\nBob,Y\n")
    assert r["created"] == 0 and "No email column" in r["errors"][0]
