"""Money, quantity, unit and tax helpers.

Money is integer paise. Quantity is an integer number of thousandths of the
item's stock unit. Everything that needs division goes through Decimal with an
explicit rounding rule, so nothing drifts.
"""
import os
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


class BusinessError(Exception):
    """A rule of the business was broken. The message is safe to show the user."""


class StockError(BusinessError):
    pass


def today():
    # FACTORY_TODAY lets tests and demos pin the date.
    return os.environ.get("FACTORY_TODAY") or date.today().isoformat()


def uname(user):
    return user if isinstance(user, str) else user["username"]


def D(v):
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v).strip().replace(",", "") or "0")
    except InvalidOperation:
        raise ValueError(f"'{v}' is not a number")


def rnd(x):
    """Round a Decimal to an integer, half away from zero."""
    return int(D(x).quantize(Decimal(1), rounding=ROUND_HALF_UP))


# ---- money -------------------------------------------------------------
def paise(rupees):
    return rnd(D(rupees) * 100)


def rs(p):
    """paise -> Decimal rupees with 2 places (good for CSV/Excel and display)."""
    return (Decimal(p) / 100).quantize(Decimal("0.01"))


def money(p):
    return f"{rs(p):,.2f}"


# ---- quantity ----------------------------------------------------------
def to_milli(v):
    q = D(v) * 1000
    if q != q.to_integral_value():
        raise ValueError("Quantities can have at most 3 decimal places")
    return int(q)


def from_milli(m):
    return Decimal(m) / 1000


def fmt_qty(m):
    s = f"{from_milli(m):f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


# ---- units -------------------------------------------------------------
# unit -> (dimension, size in the dimension's base unit)
# A carton holds 20 packets, so it converts into the "packet" dimension.
UNITS = {
    "kg": ("mass", 1000), "g": ("mass", 1),
    "l": ("volume", 1000), "ml": ("volume", 1),
    "piece": ("piece", 1),
    "packet": ("packet", 1), "carton": ("packet", 20),
}


def compatible_units(unit):
    dim = UNITS[unit][0]
    return [u for u, (d, _) in UNITS.items() if d == dim]


def convert(qty, from_unit, to_unit):
    d1, f1 = UNITS[from_unit]
    d2, f2 = UNITS[to_unit]
    if d1 != d2:
        raise BusinessError(f"Cannot convert {from_unit} to {to_unit}")
    return D(qty) * f1 / f2


# ---- GST ---------------------------------------------------------------
def gst_split(taxable_paise, gst_bp, interstate):
    """Return (cgst, sgst, igst) in paise. Tax is rounded once, then CGST takes
    the floor of half and SGST the rest, so CGST + SGST always equals the tax."""
    tax = rnd(Decimal(taxable_paise) * gst_bp / 10000)
    if interstate:
        return 0, 0, tax
    cgst = tax // 2
    return cgst, tax - cgst, 0


def fy_of(d):
    d = date.fromisoformat(d) if isinstance(d, str) else d
    y = d.year if d.month >= 4 else d.year - 1
    return f"{y}-{str(y + 1)[2:]}"


# ---- amount in words (Indian system) -----------------------------------
_ONES = ("Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen "
         "Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen").split()
_TENS = "_ _ Twenty Thirty Forty Fifty Sixty Seventy Eighty Ninety".split()


def _below_1000(n):
    out = []
    if n >= 100:
        out.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))
    elif n > 0:
        out.append(_ONES[n])
    return " ".join(out)


def amount_in_words(p):
    rupees, ps = divmod(p, 100)
    parts = []
    for size, name in ((10**7, "Crore"), (10**5, "Lakh"), (1000, "Thousand")):
        if rupees >= size:
            parts.append(_below_1000(rupees // size) + " " + name)
            rupees %= size
    if rupees or not parts:
        parts.append(_below_1000(rupees) if rupees else "Zero")
    text = "Rupees " + " ".join(parts)
    if ps:
        text += " and " + _below_1000(ps) + " Paise"
    return text + " Only"


def pct(part, whole):
    if not whole:
        return Decimal("0.00")
    return (Decimal(part) / Decimal(whole) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def audit(db, user, entity, entity_id, action, detail=None):
    import json
    from datetime import datetime
    db.execute(
        "INSERT INTO audit_log(ts,user,entity,entity_id,action,detail) VALUES (?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), uname(user), entity, entity_id, action,
         json.dumps(detail, default=str) if detail is not None else None))
