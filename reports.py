"""Auditor reports. Every report is a function (db, date_from, date_to, item_id=None) that
returns (columns, rows). The web layer renders them and exports the same data to CSV,
Excel and PDF, so the numbers in every format always match."""
from datetime import date
from decimal import Decimal

import production
from core import from_milli, pct, rs

Q2 = Decimal("0.01")


def _total(rows, label_col, sum_cols):
    if not rows:
        return rows
    t = [""] * len(rows[0])
    t[label_col] = "Total"
    for c in sum_cols:
        t[c] = sum((r[c] for r in rows), Decimal(0))
    return rows + [t]


def sales_register(db, f, t, **_):
    cols = ["Invoice", "Date", "Customer", "GSTIN", "Place of supply", "Taxable", "CGST", "SGST", "IGST", "Total", "Status"]
    rows = [[r["inv_no"], r["inv_date"], r["name"], r["gstin"] or "", r["place_of_supply"], rs(r["taxable_paise"]),
             rs(r["cgst_paise"]), rs(r["sgst_paise"]), rs(r["igst_paise"]), rs(r["total_paise"]), r["status"]]
            for r in db.execute("SELECT i.*, c.name, c.gstin FROM invoices i JOIN customers c ON c.id=i.customer_id "
                                "WHERE i.inv_date BETWEEN ? AND ? AND i.status != 'cancelled' ORDER BY i.inv_date, i.id", (f, t))]
    return cols, _total(rows, 0, range(5, 10))


def customer_sales(db, f, t, **_):
    cols = ["Customer", "Type", "State", "Invoices", "Taxable", "Tax", "Total"]
    rows = [[r["name"], r["type"], r["state"], r["n"], rs(r["tx"]), rs(r["tot"] - r["tx"]), rs(r["tot"])]
            for r in db.execute("SELECT c.name,c.type,c.state,COUNT(i.id) n,SUM(i.taxable_paise) tx,SUM(i.total_paise) tot "
                                "FROM invoices i JOIN customers c ON c.id=i.customer_id "
                                "WHERE i.inv_date BETWEEN ? AND ? AND i.status!='cancelled' GROUP BY c.id ORDER BY tot DESC", (f, t))]
    return cols, _total(rows, 0, [4, 5, 6])


def product_sales(db, f, t, **_):
    cols = ["Product", "HSN", "Qty", "Unit", "Taxable", "Tax", "Total"]
    rows = [[r["name"], r["hsn"] or "", from_milli(r["q"]), r["unit"], rs(r["tx"]), rs(r["tax"]), rs(r["tx"] + r["tax"])]
            for r in db.execute("SELECT it.name,it.hsn,it.unit,SUM(l.qty_m) q,SUM(l.taxable_paise) tx,"
                                "SUM(l.cgst_paise+l.sgst_paise+l.igst_paise) tax FROM invoice_lines l "
                                "JOIN invoices i ON i.id=l.invoice_id JOIN items it ON it.id=l.item_id "
                                "WHERE i.inv_date BETWEEN ? AND ? AND i.status!='cancelled' GROUP BY it.id ORDER BY tx DESC", (f, t))]
    return cols, _total(rows, 0, [4, 5, 6])


def gst_summary(db, f, t, **_):
    """Outward supplies grouped the way GSTR-1 (B2B vs B2C) and GSTR-3B table 3.1(a) ask for them."""
    cols = ["Supply type", "Place of supply", "GST rate %", "Invoices", "Taxable value", "IGST", "CGST", "SGST", "Total tax"]
    rows = []
    for r in db.execute(
            "SELECT CASE WHEN c.gstin IS NULL OR c.gstin='' THEN 'B2C' ELSE 'B2B' END AS typ, i.place_of_supply pos, "
            "l.gst_bp bp, COUNT(DISTINCT i.id) n, SUM(l.taxable_paise) tx, SUM(l.igst_paise) ig, SUM(l.cgst_paise) cg, SUM(l.sgst_paise) sg "
            "FROM invoice_lines l JOIN invoices i ON i.id=l.invoice_id JOIN customers c ON c.id=i.customer_id "
            "WHERE i.inv_date BETWEEN ? AND ? AND i.status!='cancelled' GROUP BY typ, pos, bp ORDER BY typ, pos, bp", (f, t)):
        rows.append([r["typ"], r["pos"], Decimal(r["bp"]) / 100, r["n"], rs(r["tx"]), rs(r["ig"]), rs(r["cg"]), rs(r["sg"]),
                     rs(r["ig"] + r["cg"] + r["sg"])])
    return cols, _total(rows, 0, range(4, 9))


def gst_hsn(db, f, t, **_):
    cols = ["HSN", "Description", "UQC", "Qty", "Rate %", "Taxable value", "IGST", "CGST", "SGST"]
    rows = [[r["hsn"] or "", r["name"], r["unit"], from_milli(r["q"]), Decimal(r["bp"]) / 100, rs(r["tx"]), rs(r["ig"]), rs(r["cg"]), rs(r["sg"])]
            for r in db.execute("SELECT l.hsn,it.name,it.unit,l.gst_bp bp,SUM(l.qty_m) q,SUM(l.taxable_paise) tx,SUM(l.igst_paise) ig,"
                                "SUM(l.cgst_paise) cg,SUM(l.sgst_paise) sg FROM invoice_lines l JOIN invoices i ON i.id=l.invoice_id "
                                "JOIN items it ON it.id=l.item_id WHERE i.inv_date BETWEEN ? AND ? AND i.status!='cancelled' "
                                "GROUP BY l.hsn, it.id, l.gst_bp", (f, t))]
    return cols, _total(rows, 0, range(5, 9))


def purchase_register(db, f, t, **_):
    cols = ["Date", "Supplier", "Ref", "Item", "Qty", "Unit", "Rate / unit", "Amount"]
    rows = [[r["txn_date"], r["sname"], r["ref"] or "", r["name"], from_milli(r["qty_m"]), r["unit"], rs(r["rate_paise"]), rs(r["amount_paise"])]
            for r in db.execute("SELECT p.*, s.name sname, i.name, i.unit FROM purchases p JOIN suppliers s ON s.id=p.supplier_id "
                                "JOIN items i ON i.id=p.item_id WHERE p.txn_date BETWEEN ? AND ? ORDER BY p.txn_date, p.id", (f, t))]
    return cols, _total(rows, 0, [7])


def stock_summary(db, f, t, kind="raw", **_):
    """opening + receipts - issues +/- adjustments = closing, for every item of one kind."""
    cols = ["Item", "Unit", "Opening", "Receipts", "Issues", "Adjustments", "Closing", "Value (avg cost)"]
    sql = ("SELECT i.name,i.unit,"
           "COALESCE(SUM(CASE WHEN l.txn_date < :f THEN l.qty_m END),0) opening,"
           "COALESCE(SUM(CASE WHEN l.txn_date BETWEEN :f AND :t AND l.qty_m>0 AND l.source_type!='adjust' THEN l.qty_m END),0) receipts,"
           "COALESCE(-SUM(CASE WHEN l.txn_date BETWEEN :f AND :t AND l.qty_m<0 AND l.source_type!='adjust' THEN l.qty_m END),0) issues,"
           "COALESCE(SUM(CASE WHEN l.txn_date BETWEEN :f AND :t AND l.source_type='adjust' THEN l.qty_m END),0) adj,"
           "COALESCE(SUM(CASE WHEN l.txn_date <= :t THEN l.qty_m END),0) closing,"
           "COALESCE(SUM(CASE WHEN l.txn_date <= :t THEN l.value_paise END),0) value "
           "FROM items i LEFT JOIN stock_ledger l ON l.item_id=i.id WHERE i.kind=:k GROUP BY i.id ORDER BY i.name")
    rows = [[r["name"], r["unit"], from_milli(r["opening"]), from_milli(r["receipts"]), from_milli(r["issues"]),
             from_milli(r["adj"]), from_milli(r["closing"]), rs(r["value"])]
            for r in db.execute(sql, {"f": f, "t": t, "k": kind})]
    return cols, rows


def stock_raw(db, f, t, **_):  return stock_summary(db, f, t, kind="raw")
def stock_pack(db, f, t, **_): return stock_summary(db, f, t, kind="pack")
def stock_fg(db, f, t, **_):   return stock_summary(db, f, t, kind="fg")


def stock_ledger(db, f, t, item_id=None, **_):
    cols = ["Date", "Item", "Source", "Ref", "Batch", "Qty in", "Qty out", "Value", "Balance qty", "Note", "User"]
    where, args = "WHERE l.txn_date <= ?", [t]
    if item_id:
        where += " AND l.item_id = ?"
        args.append(item_id)
    rows, running = [], {}
    for r in db.execute("SELECT l.*, i.name FROM stock_ledger l JOIN items i ON i.id=l.item_id " + where +
                        " ORDER BY l.item_id, l.id", args):
        running[r["item_id"]] = running.get(r["item_id"], 0) + r["qty_m"]
        if r["txn_date"] >= f:
            rows.append([r["txn_date"], r["name"], r["source_type"], r["source_id"] or "", r["batch_no"] or "",
                         from_milli(r["qty_m"]) if r["qty_m"] > 0 else "", from_milli(-r["qty_m"]) if r["qty_m"] < 0 else "",
                         rs(r["value_paise"]), from_milli(running[r["item_id"]]), r["note"] or "", r["user"] or ""])
    return cols, rows


def production_report(db, f, t, **_):
    cols = ["Run", "Date", "Product", "Line", "Planned", "Actual", "Unit", "Variance %", "Flag"]
    rows = []
    for r in db.execute("SELECT id FROM production_runs WHERE txn_date BETWEEN ? AND ? AND status='posted' ORDER BY id", (f, t)):
        d = production.run_detail(db, r["id"])
        run = d["run"]
        rows.append([run["run_no"], run["txn_date"], run["product"], "Yield (good packets)", from_milli(run["expected_m"]),
                     from_milli(run["good_m"]), "packet", d["yield_var"], "FLAG" if d["yield_flag"] else ""])
        rows.append([run["run_no"], run["txn_date"], run["product"], "Wastage (damaged packs)", "", from_milli(run["damaged_m"]), "packet", "", ""])
        for l in d["lines"]:
            rows.append([run["run_no"], run["txn_date"], run["product"], l["name"], from_milli(l["planned_m"]),
                         from_milli(l["actual_m"]), l["unit"], l["var"], "FLAG" if l["flag"] else ""])
    return cols, rows


def cost_report(db, f, t, **_):
    cols = ["Run", "Date", "Product", "Good packets", "Materials", "Overheads", "Total cost", "Cost / packet", "Selling price", "Margin / packet", "Margin %"]
    rows = []
    for r in db.execute("SELECT id FROM production_runs WHERE txn_date BETWEEN ? AND ? AND status='posted' ORDER BY id", (f, t)):
        d = production.run_detail(db, r["id"])
        c, run = d["costs"], d["run"]
        if c["cpp_paise"] is None:
            continue
        cpp = (c["cpp_paise"] / 100).quantize(Q2)
        price = rs(run["price_paise"])
        rows.append([run["run_no"], run["txn_date"], run["product"], c["good_packets"], rs(c["materials"]), rs(c["overhead"]),
                     rs(c["total"]), cpp, price, price - cpp, pct(price - cpp, price)])
    return cols, rows


def collections(db, f, t, **_):
    cols = ["Date", "Customer", "Invoice", "Mode", "Reference", "Amount"]
    rows = [[r["pay_date"], r["name"], r["inv_no"], r["mode"], r["ref"] or "", rs(r["amount_paise"])]
            for r in db.execute("SELECT p.*, c.name, i.inv_no FROM payments p JOIN customers c ON c.id=p.customer_id "
                                "JOIN invoices i ON i.id=p.invoice_id WHERE p.pay_date BETWEEN ? AND ? ORDER BY p.pay_date, p.id", (f, t))]
    return cols, _total(rows, 0, [5])


def outstanding(db, f, t, **_):
    """Receivables by customer as on the 'to' date, aged from invoice date."""
    cols = ["Customer", "Open invoices", "0-30 days", "31-60 days", "61-90 days", "Over 90 days", "Outstanding"]
    asof = date.fromisoformat(t)
    by = {}
    for inv in db.execute("SELECT i.*, c.name FROM invoices i JOIN customers c ON c.id=i.customer_id "
                          "WHERE i.status!='cancelled' AND i.inv_date <= ?", (t,)):
        paid = db.execute("SELECT COALESCE(SUM(amount_paise),0) FROM payments WHERE invoice_id=? AND pay_date<=?", (inv["id"], t)).fetchone()[0]
        due = inv["total_paise"] - paid
        if due <= 0:
            continue
        age = (asof - date.fromisoformat(inv["inv_date"])).days
        b = 0 if age <= 30 else 1 if age <= 60 else 2 if age <= 90 else 3
        row = by.setdefault(inv["name"], [0, 0, 0, 0, 0])
        row[0] += 1
        row[1 + b] += due
    rows = [[n, v[0], rs(v[1]), rs(v[2]), rs(v[3]), rs(v[4]), rs(sum(v[1:]))] for n, v in sorted(by.items())]
    return cols, _total(rows, 0, range(2, 7))


def audit_trail(db, f, t, **_):
    cols = ["Time", "User", "Entity", "Id", "Action", "Detail"]
    rows = [[r["ts"], r["user"], r["entity"], r["entity_id"] or "", r["action"], r["detail"] or ""]
            for r in db.execute("SELECT * FROM audit_log WHERE substr(ts,1,10) BETWEEN ? AND ? ORDER BY id DESC", (f, t))]
    return cols, rows


# key -> (title, function, group). The group decides which roles see it in the web layer.
REPORTS = {
    "sales_register":     ("Sales register", sales_register, "accounts"),
    "customer_sales":     ("Customer-wise sales", customer_sales, "accounts"),
    "product_sales":      ("Product-wise sales", product_sales, "accounts"),
    "gst_summary":        ("GST summary (GSTR-1 / 3B shape)", gst_summary, "accounts"),
    "gst_hsn":            ("HSN-wise summary of outward supplies", gst_hsn, "accounts"),
    "purchase_register":  ("Purchase register", purchase_register, "accounts"),
    "stock_raw":          ("Stock summary: raw materials", stock_raw, "stock"),
    "stock_pack":         ("Stock summary: packaging", stock_pack, "stock"),
    "stock_fg":           ("Stock summary: finished goods", stock_fg, "stock"),
    "stock_ledger":       ("Stock ledger", stock_ledger, "stock"),
    "production_report":  ("Production: planned vs actual", production_report, "stock"),
    "cost_report":        ("Cost per packet and margin", cost_report, "accounts"),
    "collections":        ("Payments and collections", collections, "accounts"),
    "outstanding":        ("Outstanding receivables with ageing", outstanding, "accounts"),
    "audit_trail":        ("Audit trail", audit_trail, "accounts"),
}
