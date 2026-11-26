"""The section 5 scenario from the brief, run through the service layer, with every
expected figure asserted. If these pass, the numbers in the README are real."""
from decimal import Decimal

import pytest

import production
import reports
import sales
import stock
from core import BusinessError, StockError, paise, to_milli


def bal(conn, ids, code):
    return stock.balance(conn, ids[code])[0]


@pytest.fixture
def benchmark(conn, users, ids):
    """Steps 1 to 8. Returns the interesting ids."""
    admin, prod = users["admin"], users["production"]
    cust = {r["name"]: r["id"] for r in conn.execute("SELECT * FROM customers")}
    sup = conn.execute("SELECT id FROM suppliers").fetchone()["id"]

    # 1. purchase 60 kg flour at Rs 45
    with conn:
        conn.execute("INSERT INTO purchases(txn_date,supplier_id,ref,item_id,qty_m,rate_paise,amount_paise,user) VALUES "
                     "('2026-11-26',?,'P1',?,?,?,?,'production')", (sup, ids["FLOUR"], to_milli(60), paise(45), paise(2700)))
        stock.post(conn, ids["FLOUR"], to_milli(60), "purchase", 1, prod, rate_paise=paise(45))

    # 2-3. production run: flour 40 kg, oil 10.5 L, spice 1.2 kg, 9 cartons, 188 good, 6 pouches damaged
    run_id = production.create_run(
        conn, prod, ids["MUR200"], 1,
        {ids["FLOUR"]: to_milli(40), ids["OIL"]: to_milli("10.5"), ids["SPICE"]: to_milli("1.2"), ids["CARTON"]: to_milli(9)},
        good_m=to_milli(188), damaged_m=to_milli(6))
    after_run_fg = bal(conn, ids, "MUR200")

    # 4-5. Salesman A: 150 packets for Retailer A -> approve -> invoice -> dispatch
    oa = sales.create_order(conn, users["salesman_a"], cust["Retailer A"], [(ids["MUR200"], to_milli(150))])
    sales.approve(conn, admin, oa)
    inv_a = sales.create_invoice(conn, admin, oa)
    sales.dispatch(conn, admin, inv_a)

    # 6. Salesman B: 120 packets for Distributor B (only 88 left)
    ob = sales.create_order(conn, users["salesman_b"], cust["Distributor B"], [(ids["MUR200"], to_milli(120))])

    # 7. payment
    sales.record_payment(conn, admin, inv_a, paise(4000), "UPI")

    # 8. overheads
    production.add_overhead(conn, prod, run_id, "Labour", paise(1200))
    production.add_overhead(conn, prod, run_id, "Gas / power", paise(600))
    return dict(run=run_id, inv_a=inv_a, order_b=ob, after_run_fg=after_run_fg, cust=cust)


def test_closing_balances(conn, ids, benchmark):
    assert bal(conn, ids, "FLOUR") == to_milli(120)
    assert bal(conn, ids, "OIL") == to_milli("19.5")
    assert bal(conn, ids, "SPICE") == to_milli("3.8")
    assert bal(conn, ids, "POUCH200") == to_milli(306)
    assert bal(conn, ids, "CARTON") == to_milli(21)


def test_finished_goods(conn, ids, benchmark):
    assert benchmark["after_run_fg"] == to_milli(238)
    assert bal(conn, ids, "MUR200") == to_milli(88)


def test_variances_are_flagged(conn, benchmark):
    d = production.run_detail(conn, benchmark["run"])
    assert d["yield_var"] == Decimal("-6.00") and d["yield_flag"]
    by = {l["name"]: l for l in d["lines"]}
    assert by["Cooking oil"]["var"] == Decimal("5.00") and by["Cooking oil"]["flag"]
    assert by["Flour"]["var"] == 0 and not by["Flour"]["flag"]
    assert by["200 g pouch"]["var"] == Decimal("-3.00") and not by["200 g pouch"]["flag"]   # exactly on tolerance, not beyond
    assert by["Carton (holds 20 packets)"]["flag"]                                          # 9 used vs 10 planned
    assert d["run"]["damaged_m"] == to_milli(6)


def test_retailer_invoice_and_outstanding(conn, benchmark):
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (benchmark["inv_a"],)).fetchone()
    assert (inv["taxable_paise"], inv["cgst_paise"], inv["sgst_paise"], inv["igst_paise"], inv["total_paise"]) == \
           (750000, 18750, 18750, 0, 787500)
    assert inv["inv_no"] == "INV/2026-27/0001"
    assert sales.outstanding_paise(conn, benchmark["inv_a"]) == 387500


def test_distributor_shortage_and_igst(conn, users, ids, benchmark):
    check = sales.stock_check(conn, benchmark["order_b"])
    assert check[0]["shortage"] == to_milli(32)
    with pytest.raises(BusinessError, match="short by 32"):
        sales.approve(conn, users["admin"], benchmark["order_b"])
    # Admin cuts the order to what is in stock; the invoice then carries IGST.
    line = conn.execute("SELECT id, price_paise FROM order_lines WHERE order_id=?", (benchmark["order_b"],)).fetchone()
    sales.edit_lines(conn, users["admin"], benchmark["order_b"], {line["id"]: (to_milli(88), line["price_paise"])})
    sales.approve(conn, users["admin"], benchmark["order_b"])
    inv = conn.execute("SELECT * FROM invoices WHERE id=?", (sales.create_invoice(conn, users["admin"], benchmark["order_b"]),)).fetchone()
    assert (inv["cgst_paise"], inv["sgst_paise"], inv["igst_paise"]) == (0, 0, 22000)
    assert inv["interstate"] == 1


def test_cost_per_packet_weighted_average(conn, benchmark):
    c = production.run_costs(conn, benchmark["run"])
    assert c["materials"] == 405500        # 1725 + 1470 + 240 + 485 + 135 rupees
    assert c["overhead"] == 180000
    assert (c["cpp_paise"] / 100).quantize(Decimal("0.01")) == Decimal("31.14")


def test_reports_run_and_stock_identity(conn, benchmark):
    for key, (_, fn, _g) in reports.REPORTS.items():
        cols, rows = fn(conn, "2026-11-01", "2026-11-30")
        assert all(len(r) == len(cols) for r in rows), key
    cols, rows = reports.stock_summary(conn, "2026-11-01", "2026-11-30", kind="raw")
    for r in rows:   # opening + receipts - issues + adjustments = closing
        assert r[2] + r[3] - r[4] + r[5] == r[6]
    _, ageing = reports.outstanding(conn, "2026-11-01", "2026-11-30")
    assert ageing[0][0] == "Retailer A" and ageing[0][-1] == Decimal("3875.00")


def test_every_ledger_row_has_a_source(conn, benchmark):
    rows = conn.execute("SELECT source_type, source_id FROM stock_ledger WHERE source_type != 'opening'").fetchall()
    assert rows and all(r["source_id"] is not None for r in rows)


def test_cannot_issue_more_than_stock(conn, users, ids):
    with pytest.raises(StockError):
        production.create_run(conn, users["production"], ids["MUR200"], 5, {ids["FLOUR"]: to_milli(500)}, to_milli(100))
    assert bal(conn, ids, "FLOUR") == to_milli(100)          # failed run left nothing behind
    assert conn.execute("SELECT COUNT(*) FROM production_runs").fetchone()[0] == 0


def test_credit_note_returns_stock_and_keeps_invoice(conn, users, ids, benchmark):
    # pay nothing on a fresh invoice, then cancel it
    cust = benchmark["cust"]["Retailer A"]
    o = sales.create_order(conn, users["salesman_a"], cust, [(ids["MUR200"], to_milli(10))])
    sales.approve(conn, users["admin"], o)
    inv = sales.create_invoice(conn, users["admin"], o)
    sales.dispatch(conn, users["admin"], inv)
    assert bal(conn, ids, "MUR200") == to_milli(78)
    cn = sales.cancel_invoice(conn, users["admin"], inv, "Wrong customer")
    assert cn == "CN/2026-27/0001"
    assert bal(conn, ids, "MUR200") == to_milli(88)
    assert conn.execute("SELECT status FROM invoices WHERE id=?", (inv,)).fetchone()[0] == "cancelled"
    assert sales.outstanding_paise(conn, inv) == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='cancel'").fetchone()[0] == 1


def test_offline_resend_does_not_duplicate(conn, users, ids, benchmark):
    args = (conn, users["salesman_a"], benchmark["cust"]["Retailer A"], [(ids["MUR200"], to_milli(5))])
    first = sales.create_order(*args, client_ref="abc-123")
    assert sales.create_order(*args, client_ref="abc-123") == first
