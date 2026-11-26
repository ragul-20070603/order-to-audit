"""Bill of materials and production runs.

A run issues raw/packaging stock against the BOM (valued at weighted average),
receives the good packets into finished goods at the cost of what was issued,
and keeps planned vs actual for every line so variance can be reported.
"""
from datetime import date, timedelta
from decimal import Decimal

import stock
from core import BusinessError, D, audit, from_milli, fmt_qty, pct, today
from db import setting


def tolerance(db):
    return D(setting(db, "variance_tolerance_pct", "3"))


def variance(planned_m, actual_m, tol):
    """Variance in percent of plan, and whether it breaches the tolerance."""
    if planned_m == 0:
        return Decimal("0.00"), False
    v = pct(actual_m - planned_m, planned_m)
    return v, abs(v) > tol


def bom_of(db, product_id):
    return db.execute("SELECT b.* FROM boms b JOIN items i ON i.bom_id=b.id WHERE i.id=?",
                      (product_id,)).fetchone()


def bom_lines(db, bom_id):
    return db.execute(
        "SELECT bl.*, it.name, it.unit, it.kind FROM bom_lines bl JOIN items it ON it.id=bl.item_id "
        "WHERE bl.bom_id=? ORDER BY bl.id", (bom_id,)).fetchall()


def create_run(db, user, product_id, batches, actual_by_item, good_m, damaged_m=0, note=None):
    """Post a production run.

    actual_by_item: {item_id: actual issued qty_m} for 'manual' BOM lines. Lines marked
    'per_packet' (pouches) are issued automatically as good + damaged packets.
    """
    if batches < 1:
        raise BusinessError("Batches must be at least 1")
    if good_m < 0 or damaged_m < 0:
        raise BusinessError("Output and damaged counts cannot be negative")
    bom = bom_of(db, product_id)
    if not bom:
        raise BusinessError("This product has no BOM")
    product = db.execute("SELECT * FROM items WHERE id=?", (product_id,)).fetchone()
    d = today()
    expected_m = bom["yield_m"] * batches
    expiry = (date.fromisoformat(d) + timedelta(days=product["shelf_life_days"])).isoformat() \
        if product["shelf_life_days"] else None
    with db:
        rid = db.execute(
            "INSERT INTO production_runs(txn_date,product_id,bom_id,batches,expected_m,good_m,damaged_m,"
            "expiry,note,user,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
            (d, product_id, bom["id"], batches, expected_m, good_m, damaged_m, expiry, note,
             user if isinstance(user, str) else user["username"])).lastrowid
        run_no = f"PR-{d.replace('-', '')}-{rid:03d}"
        batch_no = f"{product['code']}-{d[2:4]}{d[5:7]}{d[8:10]}-{rid:02d}"
        db.execute("UPDATE production_runs SET run_no=?, batch_no=? WHERE id=?", (run_no, batch_no, rid))
        material_cost = 0
        for l in bom_lines(db, bom["id"]):
            planned = l["qty_m"] * batches
            actual = good_m + damaged_m if l["rule"] == "per_packet" \
                else actual_by_item.get(l["item_id"], planned)
            if actual < 0:
                raise BusinessError(f"Negative quantity for {l['name']}")
            cost = 0
            if actual > 0:
                _, val = stock.post(db, l["item_id"], -actual, "production_run", rid, user,
                                    batch_no=batch_no, note=f"Issued to {run_no}")
                cost = -val
            material_cost += cost
            db.execute("INSERT INTO run_lines(run_id,item_id,planned_m,actual_m,cost_paise) VALUES (?,?,?,?,?)",
                       (rid, l["item_id"], planned, actual, cost))
        if good_m > 0:
            stock.post(db, product_id, good_m, "production_run", rid, user, value_paise=material_cost,
                       batch_no=batch_no, expiry=expiry, note=f"Produced by {run_no}")
        audit(db, user, "production_run", rid, "create",
              {"run_no": run_no, "expected_m": expected_m, "good_m": good_m, "damaged_m": damaged_m,
               "material_cost_paise": material_cost})
    return rid


def add_overhead(db, user, run_id, kind, amount_paise, note=None):
    """Labour, gas, power etc. assigned directly to a run. The amount is also added to
    the value of finished goods, as a quantity-less ledger row, so FG valuation includes it."""
    run = db.execute("SELECT * FROM production_runs WHERE id=?", (run_id,)).fetchone()
    if not run or run["status"] != "posted":
        raise BusinessError("Run not found or already reversed")
    if amount_paise <= 0:
        raise BusinessError("Overhead amount must be positive")
    with db:
        oid = db.execute("INSERT INTO run_overheads(run_id,kind,amount_paise,note,user) VALUES (?,?,?,?,?)",
                         (run_id, kind, amount_paise, note, user if isinstance(user, str) else user["username"])
                         ).lastrowid
        stock.post(db, run["product_id"], 0, "run_overhead", run_id, user, value_paise=amount_paise,
                   batch_no=run["batch_no"], note=f"{kind} for {run['run_no']}")
        audit(db, user, "production_run", run_id, "overhead", {"kind": kind, "amount_paise": amount_paise})
    return oid


def reverse_run(db, user, run_id, reason):
    """Undo a run by posting opposite ledger rows. Fails if the finished goods were already sold."""
    if not reason or not reason.strip():
        raise BusinessError("A reason is required")
    run = db.execute("SELECT * FROM production_runs WHERE id=?", (run_id,)).fetchone()
    if not run or run["status"] != "posted":
        raise BusinessError("Run not found or already reversed")
    rows = db.execute("SELECT * FROM stock_ledger WHERE source_id=? AND source_type IN ('production_run','run_overhead')",
                      (run_id,)).fetchall()
    with db:
        for r in rows:
            stock.post(db, r["item_id"], -r["qty_m"], "run_reversal", run_id, user,
                       value_paise=-r["value_paise"], batch_no=r["batch_no"],
                       note=f"Reversal of {run['run_no']}: {reason}")
        db.execute("UPDATE production_runs SET status='reversed' WHERE id=?", (run_id,))
        audit(db, user, "production_run", run_id, "reverse", {"reason": reason})


def run_costs(db, run_id):
    run = db.execute("SELECT * FROM production_runs WHERE id=?", (run_id,)).fetchone()
    mat = db.execute("SELECT COALESCE(SUM(cost_paise),0) FROM run_lines WHERE run_id=?", (run_id,)).fetchone()[0]
    oh = db.execute("SELECT COALESCE(SUM(amount_paise),0) FROM run_overheads WHERE run_id=?", (run_id,)).fetchone()[0]
    packets = from_milli(run["good_m"])
    cpp = (Decimal(mat + oh) / packets) if packets > 0 else None      # paise per packet, unrounded
    return {"materials": mat, "overhead": oh, "total": mat + oh, "good_packets": packets, "cpp_paise": cpp}


def run_detail(db, run_id):
    run = db.execute("SELECT r.*, i.name AS product, i.price_paise, i.unit FROM production_runs r "
                     "JOIN items i ON i.id=r.product_id WHERE r.id=?", (run_id,)).fetchone()
    if not run:
        return None
    tol = tolerance(db)
    lines = []
    for l in db.execute("SELECT rl.*, it.name, it.unit FROM run_lines rl JOIN items it ON it.id=rl.item_id "
                        "WHERE rl.run_id=? ORDER BY rl.id", (run_id,)):
        v, flag = variance(l["planned_m"], l["actual_m"], tol)
        lines.append({**dict(l), "var": v, "flag": flag})
    yv, yflag = variance(run["expected_m"], run["good_m"], tol)
    return {"run": run, "lines": lines, "yield_var": yv, "yield_flag": yflag, "tol": tol,
            "overheads": db.execute("SELECT * FROM run_overheads WHERE run_id=? ORDER BY id", (run_id,)).fetchall(),
            "costs": run_costs(db, run_id)}


def recent_alerts(db, limit=8):
    """Flagged variances from the latest runs, for the owner dashboard."""
    out = []
    for r in db.execute("SELECT id FROM production_runs WHERE status='posted' ORDER BY id DESC LIMIT 5"):
        d = run_detail(db, r["id"])
        if d["yield_flag"]:
            out.append((d["run"]["run_no"], "Yield", d["yield_var"]))
        for l in d["lines"]:
            if l["flag"]:
                out.append((d["run"]["run_no"], l["name"], l["var"]))
    return out[:limit]
