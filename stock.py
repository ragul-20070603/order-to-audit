"""The stock ledger. Every movement of raw, packaging or finished stock is one
row in stock_ledger. Balances are SUM() over it and are never stored or edited.

Valuation method: weighted average cost. A receipt is valued at its purchase
(or production) cost. An issue is valued at the item's running average at that
moment: balance value / balance quantity.
"""
from datetime import datetime
from decimal import Decimal

from core import BusinessError, StockError, audit, fmt_qty, rnd, today


def balance(db, item_id):
    """Return (qty_m, value_paise) for an item."""
    r = db.execute("SELECT COALESCE(SUM(qty_m),0), COALESCE(SUM(value_paise),0) "
                   "FROM stock_ledger WHERE item_id=?", (item_id,)).fetchone()
    return r[0], r[1]


def post(db, item_id, qty_m, source_type, source_id, user, rate_paise=None, value_paise=None,
         batch_no=None, expiry=None, note=None, txn_date=None):
    """Append one ledger row. Returns (row_id, value_paise).

    Receipts need rate_paise (paise per stock unit) or an explicit value_paise.
    Issues are valued at weighted average unless value_paise is given (reversals).
    Stock can never go negative.
    """
    from core import uname
    bal_q, bal_v = balance(db, item_id)
    if qty_m < 0 and bal_q + qty_m < 0:
        item = db.execute("SELECT name, unit FROM items WHERE id=?", (item_id,)).fetchone()
        raise StockError(f"Not enough {item['name']}: need {fmt_qty(-qty_m)} {item['unit']}, "
                         f"only {fmt_qty(bal_q)} {item['unit']} in stock")
    if value_paise is not None:
        value = value_paise
    elif qty_m >= 0:
        value = rnd(Decimal(qty_m) * (rate_paise or 0) / 1000)
    elif bal_q + qty_m == 0:
        value = -bal_v                                   # clear the balance exactly
    else:
        value = -rnd(Decimal(-qty_m) * bal_v / bal_q)
    cur = db.execute(
        "INSERT INTO stock_ledger(ts,txn_date,item_id,qty_m,value_paise,source_type,source_id,"
        "batch_no,expiry,note,user) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), txn_date or today(), item_id, qty_m, value,
         source_type, source_id, batch_no, expiry, note, uname(user)))
    return cur.lastrowid, value


def adjust(db, user, item_id, delta_m, reason):
    """Manual correction (count difference, spoilage). Needs a reason, and is audited."""
    if not reason or not reason.strip():
        raise BusinessError("A reason is required for a stock adjustment")
    if delta_m == 0:
        raise BusinessError("Adjustment quantity is zero")
    with db:
        if delta_m > 0:
            q, v = balance(db, item_id)
            value = rnd(Decimal(delta_m) * v / q) if q > 0 else 0
            rid, value = post(db, item_id, delta_m, "adjust", None, user, value_paise=value, note=reason)
        else:
            rid, value = post(db, item_id, delta_m, "adjust", None, user, note=reason)
        audit(db, user, "stock", item_id, "adjust", {"qty_m": delta_m, "value_paise": value, "reason": reason})
    return rid
