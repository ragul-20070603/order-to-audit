"""Order -> approval -> GST invoice -> dispatch -> payment. Cancelling a posted invoice
creates a credit note and puts the stock back; nothing is ever deleted."""
from datetime import date

import stock
from core import (BusinessError, audit, fmt_qty, fy_of, gst_split, money, rnd, today, uname)
from db import setting
from decimal import Decimal


def _order(db, order_id):
    o = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        raise BusinessError("Order not found")
    return o


def create_order(db, user, customer_id, lines, client_ref=None, note=None):
    """lines: [(item_id, qty_m)]. Re-sending the same client_ref returns the existing order,
    so an offline queue that retries can never create duplicates."""
    if client_ref:
        dup = db.execute("SELECT id FROM orders WHERE client_ref=?", (client_ref,)).fetchone()
        if dup:
            return dup["id"]
    lines = [(i, q) for i, q in lines if q > 0]
    if not lines:
        raise BusinessError("Add at least one product with a quantity")
    if not db.execute("SELECT 1 FROM customers WHERE id=?", (customer_id,)).fetchone():
        raise BusinessError("Unknown customer")
    with db:
        oid = db.execute("INSERT INTO orders(customer_id,salesman_id,status,note,client_ref,created_at) "
                         "VALUES (?,?,'submitted',?,?,datetime('now','localtime'))",
                         (customer_id, user["id"], note, client_ref)).lastrowid
        db.execute("UPDATE orders SET order_no=? WHERE id=?", (f"ORD-{oid:05d}", oid))
        for item_id, qty in lines:
            item = db.execute("SELECT * FROM items WHERE id=? AND kind='fg'", (item_id,)).fetchone()
            if not item:
                raise BusinessError("Unknown product")
            db.execute("INSERT INTO order_lines(order_id,item_id,qty_m,price_paise) VALUES (?,?,?,?)",
                       (oid, item_id, qty, item["price_paise"]))
        audit(db, user, "order", oid, "create", {"lines": lines})
    return oid


def committed_m(db, item_id, exclude_order=None):
    """Quantity already promised to other approved/invoiced orders but not yet dispatched."""
    return db.execute(
        "SELECT COALESCE(SUM(ol.qty_m),0) FROM order_lines ol JOIN orders o ON o.id=ol.order_id "
        "WHERE ol.item_id=? AND o.status IN ('approved','invoiced') AND o.id != ?",
        (item_id, exclude_order or -1)).fetchone()[0]


def stock_check(db, order_id):
    out = []
    for l in db.execute("SELECT ol.*, i.name, i.unit FROM order_lines ol JOIN items i ON i.id=ol.item_id "
                        "WHERE ol.order_id=? ORDER BY ol.id", (order_id,)):
        on_hand = stock.balance(db, l["item_id"])[0]
        reserved = committed_m(db, l["item_id"], order_id)
        avail = on_hand - reserved
        out.append({**dict(l), "on_hand": on_hand, "reserved": reserved, "available": avail,
                    "shortage": max(0, l["qty_m"] - avail)})
    return out


def edit_lines(db, user, order_id, edits):
    """edits: {line_id: (qty_m, price_paise)}. Quantity 0 removes the line. Logged old -> new."""
    o = _order(db, order_id)
    if o["status"] != "submitted":
        raise BusinessError("Only submitted orders can be edited")
    with db:
        changes = {}
        for line_id, (qty, price) in edits.items():
            old = db.execute("SELECT * FROM order_lines WHERE id=? AND order_id=?", (line_id, order_id)).fetchone()
            if not old or (old["qty_m"], old["price_paise"]) == (qty, price):
                continue
            if price < 0 or qty < 0:
                raise BusinessError("Quantity and price cannot be negative")
            changes[line_id] = {"qty_m": [old["qty_m"], qty], "price_paise": [old["price_paise"], price]}
            if qty == 0:
                db.execute("DELETE FROM order_lines WHERE id=?", (line_id,))
            else:
                db.execute("UPDATE order_lines SET qty_m=?, price_paise=? WHERE id=?", (qty, price, line_id))
        if not db.execute("SELECT 1 FROM order_lines WHERE order_id=?", (order_id,)).fetchone():
            raise BusinessError("An order needs at least one line. Reject it instead.")
        if changes:
            audit(db, user, "order", order_id, "edit", changes)


def approve(db, user, order_id):
    o = _order(db, order_id)
    if o["status"] != "submitted":
        raise BusinessError("Only submitted orders can be approved")
    short = [c for c in stock_check(db, order_id) if c["shortage"] > 0]
    if short:
        raise BusinessError("Not enough finished goods: " + "; ".join(
            f"{c['name']} short by {fmt_qty(c['shortage'])} (ordered {fmt_qty(c['qty_m'])}, "
            f"available {fmt_qty(max(c['available'], 0))})" for c in short) +
            ". Reduce the quantity or reject the order.")
    with db:
        db.execute("UPDATE orders SET status='approved' WHERE id=?", (order_id,))
        audit(db, user, "order", order_id, "approve")


def reject(db, user, order_id, reason):
    o = _order(db, order_id)
    if o["status"] not in ("submitted", "approved"):
        raise BusinessError("This order can no longer be rejected")
    with db:
        db.execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
        audit(db, user, "order", order_id, "reject", {"reason": reason})


def _next_no(db, prefix, fy):
    key = prefix + fy
    row = db.execute("SELECT last FROM doc_seq WHERE key=?", (key,)).fetchone()
    n = (row[0] if row else 0) + 1
    db.execute("INSERT INTO doc_seq(key,last) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET last=excluded.last", (key, n))
    return n


def create_invoice(db, user, order_id):
    o = _order(db, order_id)
    if o["status"] != "approved":
        raise BusinessError("Approve the order before invoicing it")
    cust = db.execute("SELECT * FROM customers WHERE id=?", (o["customer_id"],)).fetchone()
    interstate = cust["state_code"] != setting(db, "company_state_code")
    d = today()
    fy = fy_of(d)
    lines, totals = [], [0, 0, 0, 0]
    for l in db.execute("SELECT ol.*, i.hsn, i.gst_bp FROM order_lines ol JOIN items i ON i.id=ol.item_id "
                        "WHERE ol.order_id=? ORDER BY ol.id", (order_id,)):
        taxable = rnd(Decimal(l["qty_m"]) * l["price_paise"] / 1000)
        cg, sg, ig = gst_split(taxable, l["gst_bp"], interstate)
        lines.append((l["item_id"], l["hsn"], l["qty_m"], l["price_paise"], taxable, l["gst_bp"], cg, sg, ig))
        for k, v in enumerate((taxable, cg, sg, ig)):
            totals[k] += v
    with db:
        seq = _next_no(db, "INV", fy)
        inv_no = f"INV/{fy}/{seq:04d}"
        iid = db.execute(
            "INSERT INTO invoices(inv_no,order_id,customer_id,inv_date,place_of_supply,interstate,taxable_paise,"
            "cgst_paise,sgst_paise,igst_paise,total_paise,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'issued')",
            (inv_no, order_id, cust["id"], d, cust["state"], int(interstate), *totals, sum(totals))).lastrowid
        for ln in lines:
            db.execute("INSERT INTO invoice_lines(invoice_id,item_id,hsn,qty_m,rate_paise,taxable_paise,gst_bp,"
                       "cgst_paise,sgst_paise,igst_paise) VALUES (?,?,?,?,?,?,?,?,?,?)", (iid, *ln))
        db.execute("UPDATE orders SET status='invoiced' WHERE id=?", (order_id,))
        audit(db, user, "invoice", iid, "create", {"inv_no": inv_no, "total_paise": sum(totals), "interstate": interstate})
    return iid


def _invoice(db, invoice_id):
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        raise BusinessError("Invoice not found")
    return inv


def dispatch(db, user, invoice_id):
    inv = _invoice(db, invoice_id)
    if inv["status"] != "issued":
        raise BusinessError("Only issued invoices can be dispatched")
    with db:
        for l in db.execute("SELECT * FROM invoice_lines WHERE invoice_id=?", (invoice_id,)).fetchall():
            stock.post(db, l["item_id"], -l["qty_m"], "invoice", invoice_id, user, note=f"Dispatched on {inv['inv_no']}")
        db.execute("UPDATE invoices SET status='dispatched', dispatched_at=datetime('now','localtime') WHERE id=?", (invoice_id,))
        db.execute("UPDATE orders SET status='dispatched' WHERE id=?", (inv["order_id"],))
        audit(db, user, "invoice", invoice_id, "dispatch")


def paid_paise(db, invoice_id):
    return db.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()[0]


def outstanding_paise(db, invoice_id):
    inv = _invoice(db, invoice_id)
    return 0 if inv["status"] == "cancelled" else inv["total_paise"] - paid_paise(db, invoice_id)


def record_payment(db, user, invoice_id, amount_paise, mode="Cash", ref=None):
    inv = _invoice(db, invoice_id)
    if inv["status"] == "cancelled":
        raise BusinessError("Invoice is cancelled")
    if amount_paise <= 0:
        raise BusinessError("Payment must be more than zero")
    due = outstanding_paise(db, invoice_id)
    if amount_paise > due:
        raise BusinessError(f"Payment is more than the outstanding Rs {money(due)}")
    with db:
        pid = db.execute("INSERT INTO payments(invoice_id,customer_id,pay_date,amount_paise,mode,ref,user) "
                         "VALUES (?,?,?,?,?,?,?)",
                         (invoice_id, inv["customer_id"], today(), amount_paise, mode, ref, uname(user))).lastrowid
        audit(db, user, "payment", pid, "create", {"invoice": inv["inv_no"], "amount_paise": amount_paise})
    return pid


def cancel_invoice(db, user, invoice_id, reason):
    """Cancel with a credit note for the full amount; returns dispatched stock to finished goods."""
    inv = _invoice(db, invoice_id)
    if inv["status"] == "cancelled":
        raise BusinessError("Already cancelled")
    if not reason or not reason.strip():
        raise BusinessError("A reason is required")
    if paid_paise(db, invoice_id) > 0:
        raise BusinessError("This invoice has payments recorded. Refund or reallocate them before cancelling.")
    with db:
        n = _next_no(db, "CN", fy_of(today()))
        cn_no = f"CN/{fy_of(today())}/{n:04d}"
        db.execute("INSERT INTO credit_notes(cn_no,invoice_id,cn_date,amount_paise,reason,user) VALUES (?,?,?,?,?,?)",
                   (cn_no, invoice_id, today(), inv["total_paise"], reason, uname(user)))
        if inv["status"] == "dispatched":
            for r in db.execute("SELECT * FROM stock_ledger WHERE source_type='invoice' AND source_id=?", (invoice_id,)).fetchall():
                stock.post(db, r["item_id"], -r["qty_m"], "credit_note", invoice_id, user,
                           value_paise=-r["value_paise"], note=f"Returned via {cn_no}")
        db.execute("UPDATE invoices SET status='cancelled' WHERE id=?", (invoice_id,))
        db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (inv["order_id"],))
        audit(db, user, "invoice", invoice_id, "cancel", {"credit_note": cn_no, "reason": reason})
    return cn_no
