"""Flask web layer: login, role checks, forms and report/PDF/CSV/Excel downloads.
All business rules live in sales.py, production.py and stock.py; this file only
reads forms, calls them and renders the result."""
import csv
import io
import os
import secrets
from datetime import date
from decimal import Decimal
from functools import wraps
from urllib.parse import quote

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash

import core
import db as dbm
import invoice_pdf
import production
import reports
import sales
import seed
import stock
from core import BusinessError, D, from_milli, fmt_qty, money, paise, to_milli

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
seed.run()          # first start creates and seeds the database


def db():
    if "db" not in g:
        g.db = dbm.connect()
    return g.db


@app.teardown_appcontext
def close_db(_):
    if "db" in g:
        g.db.close()


# ---- auth, roles, CSRF --------------------------------------------------
NAV = {
    "salesman": [("New order", "order_new"), ("My orders", "my_orders"), ("My customers", "my_sales")],
    "admin": [("Dashboard", "dashboard"), ("Orders", "orders"), ("Invoices", "invoices"), ("Production", "runs"),
              ("Purchases", "purchases"), ("Stock", "stock_page"), ("Reports", "report_index"), ("Masters", "items")],
    "production": [("Production", "runs"), ("Purchases", "purchases"), ("Stock", "stock_page"), ("Reports", "report_index")],
    "accountant": [("Invoices", "invoices"), ("Reports", "report_index"), ("Stock", "stock_page")],
    "owner": [("Dashboard", "dashboard"), ("Invoices", "invoices"), ("Production", "runs"), ("Stock", "stock_page"), ("Reports", "report_index")],
}
REPORT_GROUPS = {"admin": {"accounts", "stock"}, "accountant": {"accounts", "stock"}, "owner": {"accounts", "stock"}, "production": {"stock"}}


@app.before_request
def load_user():
    g.user = None
    if session.get("uid"):
        g.user = db().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
    if request.method == "POST":
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Missing or invalid form token. Reload the page and try again.")


@app.context_processor
def inject():
    session.setdefault("csrf", secrets.token_hex(16))
    return {"user": g.user, "nav": NAV.get(g.user["role"], []) if g.user else [], "csrf_token": lambda: session["csrf"],
            "money": money, "fmt_qty": fmt_qty, "from_milli": from_milli, "today": core.today()}


def need(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **k):
            if not g.user:
                if request.path.startswith("/api/"):
                    return jsonify(error="Not signed in"), 401
                return redirect(url_for("login"))
            if g.user["role"] not in roles:
                abort(403)
            return f(*a, **k)
        return wrapper
    return deco


def link(url, text):
    return Markup('<a href="%s">%s</a>') % (url, text)


def attempt(fn, ok, back):
    try:
        result = fn()
        flash(ok, "ok")
        return result if isinstance(result, Response) else redirect(back)
    except (BusinessError, ValueError) as e:
        flash(str(e), "err")
        return redirect(back)


@app.errorhandler(403)
def forbidden(_):
    return render_template("page.html", title="Not allowed", intro="Your role cannot open this page."), 403


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = db().execute("SELECT * FROM users WHERE username=?", (request.form["username"].strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            csrf = session.get("csrf")
            session.clear()
            session["uid"], session["csrf"] = u["id"], csrf or secrets.token_hex(16)
            return redirect(url_for("home"))
        flash("Wrong username or password", "err")
    return render_template("login.html", users=seed.USERS)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@need("salesman", "admin", "production", "accountant", "owner")
def home():
    return redirect(url_for({"salesman": "order_new", "production": "runs", "accountant": "invoices"}.get(g.user["role"], "dashboard")))


# ---- dashboard ----------------------------------------------------------
@app.route("/dashboard")
@need("admin", "owner")
def dashboard():
    d = db()
    q = lambda sql, *a: d.execute(sql, a).fetchone()[0]
    tiles = [
        ("Orders today", q("SELECT COUNT(*) FROM orders WHERE substr(created_at,1,10)=?", core.today())),
        ("Waiting for approval", q("SELECT COUNT(*) FROM orders WHERE status='submitted'")),
        ("To dispatch", q("SELECT COUNT(*) FROM invoices WHERE status='issued'")),
        ("Outstanding dues (Rs)", money(sum(sales.outstanding_paise(d, r["id"]) for r in d.execute("SELECT id FROM invoices WHERE status!='cancelled'")))),
    ]
    low = []
    for it in d.execute("SELECT * FROM items WHERE kind != 'fg' AND reorder_m > 0"):
        if stock.balance(d, it["id"])[0] <= it["reorder_m"]:
            low.append([it["name"], fmt_qty(stock.balance(d, it["id"])[0]), it["unit"], fmt_qty(it["reorder_m"])])
    stock_rows = [[r[0], r[1], r[6], r[7]] for k in ("fg", "raw", "pack") for r in reports.stock_summary(d, "1900-01-01", core.today(), kind=k)[1]]
    cost_rows = [[r[2], r[0], r[7], r[8], f"{r[10]}%"] for r in reports.cost_report(d, "1900-01-01", core.today())[1]][-3:]
    alerts = [[a, b, f"{c}%"] for a, b, c in production.recent_alerts(d)]
    return render_template("dashboard.html", tiles=tiles, low=low, stock_rows=stock_rows, cost_rows=cost_rows, alerts=alerts, tol=production.tolerance(d))


# ---- salesman -----------------------------------------------------------
@app.route("/order/new")
@need("salesman", "admin")
def order_new():
    d = db()
    return render_template("order_new.html", customers=d.execute("SELECT * FROM customers ORDER BY name").fetchall(),
                           products=d.execute("SELECT * FROM items WHERE kind='fg' ORDER BY name").fetchall())


@app.post("/api/orders")
@need("salesman", "admin")
def api_orders():
    data = request.get_json(silent=True) or {}
    try:
        lines = [(int(l["item_id"]), to_milli(l["qty"])) for l in data.get("lines", [])]
        oid = sales.create_order(db(), g.user, int(data["customer_id"]), lines, client_ref=data.get("client_ref"), note=data.get("note"))
    except (BusinessError, ValueError, KeyError, TypeError) as e:
        return jsonify(error=str(e)), 400
    return jsonify(id=oid, order_no=db().execute("SELECT order_no FROM orders WHERE id=?", (oid,)).fetchone()[0])


ORDER_SQL = ("SELECT o.id, o.order_no, o.created_at, o.status, c.name cname, "
             "(SELECT COALESCE(SUM(qty_m*price_paise/1000),0) FROM order_lines WHERE order_id=o.id) value, "
             "(SELECT group_concat(i.name || ' x ' || CAST(l.qty_m/1000 AS INT), ', ') FROM order_lines l JOIN items i ON i.id=l.item_id WHERE l.order_id=o.id) items "
             "FROM orders o JOIN customers c ON c.id=o.customer_id ")


@app.route("/my/orders")
@need("salesman")
def my_orders():
    rows = [[r["order_no"], r["created_at"][:16], r["cname"], r["items"], money(r["value"]), Markup('<span class="badge %s">%s</span>') % (r["status"], r["status"])]
            for r in db().execute(ORDER_SQL + "WHERE o.salesman_id=? ORDER BY o.id DESC", (g.user["id"],))]
    return render_template("page.html", title="My orders", tables=[("", ["Order", "Placed", "Customer", "Items", "Value (Rs, before tax)", "Status"], rows)])


@app.route("/my/customers")
@need("salesman")
def my_sales():
    rows = [[r["name"], r["n"], money(r["v"])] for r in db().execute(
        "SELECT c.name, COUNT(*) n, SUM((SELECT COALESCE(SUM(qty_m*price_paise/1000),0) FROM order_lines WHERE order_id=o.id)) v "
        "FROM orders o JOIN customers c ON c.id=o.customer_id WHERE o.salesman_id=? AND o.status NOT IN ('rejected','cancelled') "
        "GROUP BY c.id ORDER BY v DESC", (g.user["id"],))]
    return render_template("page.html", title="Customer-wise sales (my orders)", tables=[("", ["Customer", "Orders", "Value (Rs, before tax)"], rows)])


# ---- admin: orders -> invoices -----------------------------------------
@app.route("/orders")
@need("admin")
def orders():
    rows = [[link(url_for("order_detail", oid=r["id"]), r["order_no"]), r["created_at"][:16], r["cname"], r["items"], money(r["value"]),
             Markup('<span class="badge %s">%s</span>') % (r["status"], r["status"])]
            for r in db().execute(ORDER_SQL + "ORDER BY (o.status='submitted') DESC, o.id DESC")]
    return render_template("page.html", title="Orders", tables=[("", ["Order", "Placed", "Customer", "Items", "Value (Rs)", "Status"], rows)])


@app.route("/orders/<int:oid>")
@need("admin")
def order_detail(oid):
    d = db()
    o = d.execute("SELECT o.*, c.name cname, c.state, c.gstin, u.name salesman FROM orders o JOIN customers c ON c.id=o.customer_id "
                  "JOIN users u ON u.id=o.salesman_id WHERE o.id=?", (oid,)).fetchone() or abort(404)
    inv = d.execute("SELECT id, inv_no FROM invoices WHERE order_id=? AND status!='cancelled'", (oid,)).fetchone()
    return render_template("order_detail.html", o=o, checks=sales.stock_check(d, oid), invoice=inv)


@app.post("/orders/<int:oid>/<action>")
@need("admin")
def order_action(oid, action):
    d, back = db(), url_for("order_detail", oid=oid)
    if action == "edit":
        edits = {int(k[2:]): (to_milli(request.form[k]), paise(request.form["p_" + k[2:]])) for k in request.form if k.startswith("q_")}
        return attempt(lambda: sales.edit_lines(d, g.user, oid, edits), "Order updated", back)
    if action == "approve":
        return attempt(lambda: sales.approve(d, g.user, oid), "Order approved", back)
    if action == "reject":
        return attempt(lambda: sales.reject(d, g.user, oid, request.form.get("reason", "")), "Order rejected", back)
    if action == "invoice":
        def make():
            iid = sales.create_invoice(d, g.user, oid)
            return redirect(url_for("invoice_detail", iid=iid))
        return attempt(make, "GST invoice created", back)
    abort(404)


@app.route("/invoices")
@need("admin", "accountant", "owner")
def invoices():
    rows = [[link(url_for("invoice_detail", iid=r["id"]), r["inv_no"]), r["inv_date"], r["name"], money(r["total_paise"]),
             money(sales.outstanding_paise(db(), r["id"])), Markup('<span class="badge %s">%s</span>') % (r["status"], r["status"])]
            for r in db().execute("SELECT i.*, c.name FROM invoices i JOIN customers c ON c.id=i.customer_id ORDER BY i.id DESC")]
    return render_template("page.html", title="Invoices", tables=[("", ["Invoice", "Date", "Customer", "Total (Rs)", "Outstanding (Rs)", "Status"], rows)])


@app.route("/invoices/<int:iid>")
@need("admin", "accountant", "owner")
def invoice_detail(iid):
    d = db()
    inv = d.execute("SELECT i.*, c.name cname, c.phone, c.gstin FROM invoices i JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (iid,)).fetchone() or abort(404)
    lines = d.execute("SELECT l.*, it.name, it.unit FROM invoice_lines l JOIN items it ON it.id=l.item_id WHERE invoice_id=?", (iid,)).fetchall()
    pays = d.execute("SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (iid,)).fetchall()
    wa = "https://wa.me/91%s?text=%s" % (inv["phone"] or "", quote(f"Invoice {inv['inv_no']} for Rs {money(inv['total_paise'])} from {dbm.setting(d, 'company_name')}. Thank you for your order."))
    return render_template("invoice_detail.html", inv=inv, lines=lines, pays=pays, due=sales.outstanding_paise(d, iid), wa=wa,
                           cn=d.execute("SELECT * FROM credit_notes WHERE invoice_id=?", (iid,)).fetchone())


@app.route("/invoices/<int:iid>/pdf")
@need("admin", "accountant", "owner")
def invoice_pdf_view(iid):
    inv = db().execute("SELECT inv_no FROM invoices WHERE id=?", (iid,)).fetchone() or abort(404)
    return Response(invoice_pdf.invoice_pdf(db(), iid), mimetype="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={inv['inv_no'].replace('/', '-')}.pdf"})


@app.post("/invoices/<int:iid>/<action>")
@need("admin")
def invoice_action(iid, action):
    d, back = db(), url_for("invoice_detail", iid=iid)
    if action == "dispatch":
        return attempt(lambda: sales.dispatch(d, g.user, iid), "Dispatched. Finished goods stock reduced.", back)
    if action == "pay":
        return attempt(lambda: sales.record_payment(d, g.user, iid, paise(request.form["amount"]), request.form.get("mode", "Cash"), request.form.get("ref")), "Payment recorded", back)
    if action == "cancel":
        return attempt(lambda: sales.cancel_invoice(d, g.user, iid, request.form.get("reason", "")), "Invoice cancelled with a credit note", back)
    abort(404)


# ---- production and purchases -------------------------------------------
@app.route("/production")
@need("admin", "production", "owner")
def runs():
    rows = [[link(url_for("run_detail", rid=r["id"]), r["run_no"]), r["txn_date"], r["name"], fmt_qty(r["expected_m"]), fmt_qty(r["good_m"]), r["batch_no"], r["status"]]
            for r in db().execute("SELECT r.*, i.name FROM production_runs r JOIN items i ON i.id=r.product_id ORDER BY r.id DESC")]
    return render_template("page.html", title="Production runs", tables=[("", ["Run", "Date", "Product", "Expected", "Good packets", "Batch", "Status"], rows)],
                           actions=[("New production run", url_for("run_new"))] if g.user["role"] != "owner" else [])


@app.route("/production/new", methods=["GET", "POST"])
@need("admin", "production")
def run_new():
    d = db()
    products = d.execute("SELECT * FROM items WHERE kind='fg' AND bom_id IS NOT NULL ORDER BY name").fetchall()
    if request.method == "POST":
        pid, batches = int(request.form["product"]), int(request.form["batches"])
        def make():
            actual = {}
            for l in production.bom_lines(d, production.bom_of(d, pid)["id"]):
                if l["rule"] == "manual":
                    stock_unit = core.convert(D(request.form[f"qty_{l['item_id']}"]), request.form[f"unit_{l['item_id']}"], l["unit"])
                    actual[l["item_id"]] = to_milli(stock_unit)
            rid = production.create_run(d, g.user, pid, batches, actual, to_milli(request.form["good"]), to_milli(request.form.get("damaged") or 0), request.form.get("note"))
            return redirect(url_for("run_detail", rid=rid))
        return attempt(make, "Production run saved. Stock updated.", url_for("run_new", product=pid, batches=batches))
    pid = int(request.args.get("product") or products[0]["id"])
    batches = max(1, int(request.args.get("batches") or 1))
    bom = production.bom_of(d, pid)
    lines = [{**dict(l), "planned": from_milli(l["qty_m"] * batches), "units": core.compatible_units(l["unit"]),
              "on_hand": stock.balance(d, l["item_id"])[0]} for l in production.bom_lines(d, bom["id"])]
    return render_template("run_new.html", products=products, pid=pid, batches=batches, bom=bom, lines=lines, expected=from_milli(bom["yield_m"] * batches))


@app.route("/production/<int:rid>")
@need("admin", "production", "owner")
def run_detail(rid):
    detail = production.run_detail(db(), rid) or abort(404)
    return render_template("run_detail.html", d=detail, ledger=db().execute(
        "SELECT l.*, i.name, i.unit FROM stock_ledger l JOIN items i ON i.id=l.item_id WHERE l.source_id=? AND l.source_type IN ('production_run','run_overhead','run_reversal') ORDER BY l.id", (rid,)).fetchall())


@app.post("/production/<int:rid>/<action>")
@need("admin", "production")
def run_action(rid, action):
    d, back = db(), url_for("run_detail", rid=rid)
    if action == "overhead":
        return attempt(lambda: production.add_overhead(d, g.user, rid, request.form["kind"], paise(request.form["amount"]), request.form.get("note")), "Overhead added", back)
    if action == "reverse":
        return attempt(lambda: production.reverse_run(d, g.user, rid, request.form.get("reason", "")), "Run reversed", back)
    abort(404)


@app.route("/purchases", methods=["GET", "POST"])
@need("admin", "production", "accountant")
def purchases():
    d = db()
    if request.method == "POST":
        if g.user["role"] == "accountant":
            abort(403)
        def make():
            item = d.execute("SELECT * FROM items WHERE id=? AND kind IN ('raw','pack')", (int(request.form["item"]),)).fetchone() or abort(400)
            qty_m = to_milli(core.convert(D(request.form["qty"]), request.form["unit"], item["unit"]))
            rate = paise(request.form["rate"])
            amount = core.rnd(Decimal(qty_m) * rate / 1000)
            with d:
                pid = d.execute("INSERT INTO purchases(txn_date,supplier_id,ref,item_id,qty_m,rate_paise,amount_paise,user) VALUES (?,?,?,?,?,?,?,?)",
                                (core.today(), int(request.form["supplier"]), request.form.get("ref"), item["id"], qty_m, rate, amount, g.user["username"])).lastrowid
                stock.post(d, item["id"], qty_m, "purchase", pid, g.user, rate_paise=rate, note=request.form.get("ref"))
                core.audit(d, g.user, "purchase", pid, "create", {"item": item["name"], "qty_m": qty_m, "rate_paise": rate})
        return attempt(make, "Purchase recorded. Stock updated.", url_for("purchases"))
    cols, rows = reports.purchase_register(d, "1900-01-01", "2999-12-31")
    return render_template("purchases.html", items=d.execute("SELECT * FROM items WHERE kind IN ('raw','pack') ORDER BY kind, name").fetchall(),
                           suppliers=d.execute("SELECT * FROM suppliers ORDER BY name").fetchall(), cols=cols, rows=rows[::-1][:25],
                           units=list(core.UNITS), can_edit=g.user["role"] != "accountant")


# ---- stock --------------------------------------------------------------
@app.route("/stock", methods=["GET", "POST"])
@need("admin", "production", "accountant", "owner")
def stock_page():
    d = db()
    if request.method == "POST":
        if g.user["role"] not in ("admin", "production"):
            abort(403)
        item = d.execute("SELECT * FROM items WHERE id=?", (int(request.form["item"]),)).fetchone() or abort(400)
        def make():
            delta = to_milli(core.convert(D(request.form["qty"]), request.form["unit"], item["unit"]))
            stock.adjust(d, g.user, item["id"], delta if request.form["dir"] == "add" else -delta, request.form.get("reason", ""))
        return attempt(make, "Adjustment posted", url_for("stock_page"))
    f, t = "1900-01-01", core.today()
    tables = [(title, *reports.stock_summary(d, f, t, kind=k)) for title, k in (("Finished goods", "fg"), ("Raw materials", "raw"), ("Packaging", "pack"))]
    return render_template("stock.html", tables=tables, items=d.execute("SELECT * FROM items ORDER BY kind, name").fetchall(), units=list(core.UNITS),
                           can_edit=g.user["role"] in ("admin", "production"))


# ---- masters (admin) ----------------------------------------------------
@app.route("/masters/items", methods=["GET", "POST"])
@need("admin")
def items():
    d = db()
    if request.method == "POST":
        def make():
            f = request.form
            with d:
                iid = d.execute("INSERT INTO items(code,name,kind,unit,hsn,gst_bp,price_paise,shelf_life_days,reorder_m,bom_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (f["code"].strip().upper(), f["name"].strip(), f["kind"], f["unit"], f.get("hsn"), int(D(f.get("gst") or 0) * 100), paise(f.get("price") or 0),
                                 int(f["shelf"]) if f.get("shelf") else None, to_milli(f.get("reorder") or 0), int(f["bom"]) if f.get("bom") else None)).lastrowid
                core.audit(d, g.user, "item", iid, "create", dict(f))
        return attempt(make, "Item added", url_for("items"))
    rows = [[r["code"], r["name"], r["kind"], r["unit"], r["hsn"] or "", f"{r['gst_bp'] / 100:g}%", money(r["price_paise"]) if r["kind"] == "fg" else "", fmt_qty(r["reorder_m"])]
            for r in d.execute("SELECT * FROM items ORDER BY kind, name")]
    return render_template("masters.html", what="items", rows=rows, cols=["Code", "Name", "Kind", "Unit", "HSN", "GST", "Price (Rs)", "Reorder at"],
                           boms=d.execute("SELECT * FROM boms").fetchall(), units=["kg", "l", "piece", "packet"])


@app.route("/masters/customers", methods=["GET", "POST"])
@need("admin")
def customers():
    d = db()
    if request.method == "POST":
        def make():
            f = request.form
            with d:
                cid = d.execute("INSERT INTO customers(name,type,gstin,state,state_code,address,phone) VALUES (?,?,?,?,?,?,?)",
                                (f["name"].strip(), f["type"], f.get("gstin", "").strip().upper() or None, f["state"].strip(), f["state_code"].strip(), f.get("address"), f.get("phone"))).lastrowid
                core.audit(d, g.user, "customer", cid, "create", dict(f))
        return attempt(make, "Customer added", url_for("customers"))
    rows = [[r["name"], r["type"], r["gstin"] or "", f"{r['state']} ({r['state_code']})", r["phone"] or ""] for r in d.execute("SELECT * FROM customers ORDER BY name")]
    return render_template("masters.html", what="customers", rows=rows, cols=["Name", "Type", "GSTIN", "State", "Phone"])


@app.route("/masters/boms", methods=["GET", "POST"])
@need("admin")
def boms():
    d = db()
    if request.method == "POST":
        f, act = request.form, request.form["act"]
        def make():
            with d:
                if act == "create":
                    bid = d.execute("INSERT INTO boms(name,yield_m) VALUES (?,?)", (f["name"], to_milli(f["yield"]))).lastrowid
                    if f.get("product"):
                        d.execute("UPDATE items SET bom_id=? WHERE id=?", (bid, int(f["product"])))
                    core.audit(d, g.user, "bom", bid, "create", dict(f))
                elif act == "yield":
                    d.execute("UPDATE boms SET yield_m=? WHERE id=?", (to_milli(f["yield"]), int(f["bom"])))
                    core.audit(d, g.user, "bom", int(f["bom"]), "yield", dict(f))
                elif act == "add_line":
                    d.execute("INSERT INTO bom_lines(bom_id,item_id,qty_m,rule) VALUES (?,?,?,?) ON CONFLICT(bom_id,item_id) DO UPDATE SET qty_m=excluded.qty_m, rule=excluded.rule",
                              (int(f["bom"]), int(f["item"]), to_milli(f["qty"]), f["rule"]))
                    core.audit(d, g.user, "bom", int(f["bom"]), "line", dict(f))
                elif act == "del_line":
                    d.execute("DELETE FROM bom_lines WHERE id=?", (int(f["line"]),))
                    core.audit(d, g.user, "bom", int(f["bom"]), "del_line", dict(f))
                elif act == "assign":
                    d.execute("UPDATE items SET bom_id=? WHERE id=?", (int(f["bom"]), int(f["product"])))
                    core.audit(d, g.user, "bom", int(f["bom"]), "assign", dict(f))
        return attempt(make, "BOM saved", url_for("boms"))
    data = [{"bom": b, "lines": production.bom_lines(d, b["id"]), "products": d.execute("SELECT name FROM items WHERE bom_id=?", (b["id"],)).fetchall()} for b in d.execute("SELECT * FROM boms")]
    return render_template("boms.html", data=data, inputs=d.execute("SELECT * FROM items WHERE kind IN ('raw','pack') ORDER BY name").fetchall(),
                           products=d.execute("SELECT * FROM items WHERE kind='fg' ORDER BY name").fetchall())


# ---- reports ------------------------------------------------------------
def allowed_reports():
    groups = REPORT_GROUPS.get(g.user["role"], set())
    return {k: v for k, v in reports.REPORTS.items() if v[2] in groups}


@app.route("/reports")
@need("admin", "accountant", "owner", "production")
def report_index():
    return render_template("page.html", title="Reports", intro="Every report can be filtered by date and exported to Excel, CSV and PDF.",
                           links=[(v[0], url_for("report", key=k)) for k, v in allowed_reports().items()])


@app.route("/reports/<key>")
@need("admin", "accountant", "owner", "production")
def report(key):
    entry = allowed_reports().get(key) or abort(404)
    title, fn, _ = entry
    first = date.fromisoformat(core.today()).replace(day=1).isoformat()
    f, t = request.args.get("from") or first, request.args.get("to") or core.today()
    item_id = int(request.args["item"]) if request.args.get("item") else None
    cols, rows = fn(db(), f, t, item_id=item_id)
    fmt = request.args.get("fmt")
    sub = f"{dbm.setting(db(), 'company_name')}  |  {f} to {t}"
    if fmt == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(cols)
        w.writerows(rows)
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={key}_{f}_{t}.csv"})
    if fmt == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "".join(ch for ch in title if ch not in "[]:*?/\\")[:31]
        ws.append(cols)
        for r in rows:
            ws.append([float(v) if isinstance(v, Decimal) else v for v in r])
        buf = io.BytesIO()
        wb.save(buf)
        return Response(buf.getvalue(), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={key}_{f}_{t}.xlsx"})
    if fmt == "pdf":
        return Response(invoice_pdf.table_pdf(title, cols, rows, sub), mimetype="application/pdf", headers={"Content-Disposition": f"inline; filename={key}.pdf"})
    return render_template("page.html", title=title, tables=[("", cols, rows)], filters=True, f=f, t=t, key=key, item_id=item_id,
                           items=db().execute("SELECT id, name FROM items ORDER BY name").fetchall() if key == "stock_ledger" else None)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=bool(os.environ.get("FLASK_DEBUG")))
