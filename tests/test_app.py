"""End-to-end through the web layer: logins, roles, the whole benchmark by form posts,
PDF and export downloads, CSRF, and the offline-sync API."""
import re

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "web.db"))
    monkeypatch.setenv("FACTORY_TODAY", "2026-11-26")
    import importlib
    import db as dbm
    importlib.reload(dbm)
    import app as appmod
    importlib.reload(appmod)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client


class Session:
    def __init__(self, c):
        self.c = c

    def token(self, path="/login"):
        html = self.c.get(path).get_data(as_text=True)
        m = re.search(r'name="_csrf" value="([^"]+)"', html)
        if m:
            return m.group(1)
        with self.c.session_transaction() as s:
            return s["csrf"]

    def login(self, user, pw):
        r = self.c.post("/login", data={"username": user, "password": pw, "_csrf": self.token()})
        assert r.status_code == 302
        return self

    def post(self, path, **data):
        with self.c.session_transaction() as s:
            data["_csrf"] = s["csrf"]
        return self.c.post(path, data=data, follow_redirects=True)


def login(client, user, pw):
    return Session(client()).login(user, pw)


def test_login_required_and_roles(client):
    c = client()
    assert c.get("/orders").status_code == 302
    s = login(client, "salesman_a", "sales123")
    assert s.c.get("/order/new").status_code == 200
    assert s.c.get("/orders").status_code == 403
    assert s.c.get("/reports").status_code == 403
    assert s.c.get("/production/new").status_code == 403


def test_csrf_is_enforced(client):
    s = login(client, "admin", "admin123")
    assert s.c.post("/orders/1/approve").status_code == 400


def test_full_benchmark_through_the_ui(client):
    prod = login(client, "production", "prod123")
    r = prod.post("/purchases", supplier=1, item=1, qty="60", unit="kg", rate="45", ref="B-1")
    assert b"Purchase recorded" in r.data
    r = prod.post("/production/new", product=6, batches=1, qty_1="40", unit_1="kg", qty_2="10.5", unit_2="l",
                  qty_3="1200", unit_3="g", qty_5="9", unit_5="piece", good="188", damaged="6")
    assert b"PR-20261126-001" in r.data and b"flagged" in r.data

    sa = login(client, "salesman_a", "sales123")
    tok = sa.token("/order/new")
    r = sa.c.post("/api/orders", json={"client_ref": "u1", "customer_id": 1, "lines": [{"item_id": 6, "qty": "150"}]}, headers={"X-CSRF": tok})
    assert r.status_code == 200 and r.get_json()["order_no"] == "ORD-00001"
    dup = sa.c.post("/api/orders", json={"client_ref": "u1", "customer_id": 1, "lines": [{"item_id": 6, "qty": "150"}]}, headers={"X-CSRF": tok})
    assert dup.get_json()["id"] == 1                                    # offline retry is idempotent

    admin = login(client, "admin", "admin123")
    assert b"approved" in admin.post("/orders/1/approve").data
    r = admin.post("/orders/1/invoice")
    assert b"INV/2026-27/0001" in r.data and b"7875.00" in r.data.replace(b",", b"")
    admin.post("/invoices/1/dispatch")
    pdf = admin.c.get("/invoices/1/pdf")
    assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"

    sb = login(client, "salesman_b", "sales123")
    tok = sb.token("/order/new")
    sb.c.post("/api/orders", json={"client_ref": "u2", "customer_id": 2, "lines": [{"item_id": 6, "qty": "120"}]}, headers={"X-CSRF": tok})
    r = admin.post("/orders/2/approve")
    assert b"short by 32" in r.data

    assert b"Payment recorded" in admin.post("/invoices/1/pay", amount="4000", mode="UPI").data
    admin.post("/production/1/overhead", kind="Labour", amount="1200")
    r = admin.post("/production/1/overhead", kind="Gas / power", amount="600")
    assert b"31.14" in r.data

    for key in ("sales_register", "gst_summary", "stock_ledger", "production_report", "cost_report", "outstanding", "purchase_register"):
        assert admin.c.get(f"/reports/{key}").status_code == 200
        for fmt, magic in (("csv", b""), ("xlsx", b"PK"), ("pdf", b"%PDF")):
            resp = admin.c.get(f"/reports/{key}?fmt={fmt}")
            assert resp.status_code == 200 and resp.data.startswith(magic), (key, fmt)
    assert b"3875.00" in admin.c.get("/reports/outstanding?fmt=csv").data

    for who in (("accountant", "acct123"), ("owner", "owner123")):
        assert login(client, *who).c.get("/reports/sales_register").status_code == 200
    assert login(client, "owner", "owner123").c.get("/dashboard").status_code == 200
    assert admin.c.get("/stock").status_code == 200
    for page in ("/masters/items", "/masters/customers", "/masters/boms", "/purchases", "/production", "/invoices"):
        assert admin.c.get(page).status_code == 200, page


def test_salesman_cannot_see_other_pages_or_post_bad_orders(client):
    s = login(client, "salesman_a", "sales123")
    tok = s.token("/order/new")
    r = s.c.post("/api/orders", json={"customer_id": 1, "lines": [{"item_id": 6, "qty": "0"}]}, headers={"X-CSRF": tok})
    assert r.status_code == 400
