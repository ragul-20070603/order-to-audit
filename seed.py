"""Create the database and load the benchmark master data from section 5 of the brief.

    python seed.py            create data/factory.db if it does not exist
    python seed.py --reset    drop it and start over
"""
import os
import sys

from werkzeug.security import generate_password_hash

import db as dbm
import stock
from core import audit, paise, to_milli

USERS = [  # username, display name, password, role
    ("admin", "Factory Admin", "admin123", "admin"),
    ("salesman_a", "Salesman A", "sales123", "salesman"),
    ("salesman_b", "Salesman B", "sales123", "salesman"),
    ("production", "Production Supervisor", "prod123", "production"),
    ("accountant", "Accountant", "acct123", "accountant"),
    ("owner", "Owner", "owner123", "owner"),
]


def load(conn):
    s = {"company_name": "Sri Murugan Snacks (demo)", "company_address": "12 Industrial Estate, Coimbatore, Tamil Nadu 641001",
         "company_gstin": "33ABCDE1234F1Z5", "company_state": "Tamil Nadu", "company_state_code": "33",
         "variance_tolerance_pct": "3"}
    conn.executemany("INSERT INTO settings VALUES (?,?)", s.items())
    for u, n, p, r in USERS:
        conn.execute("INSERT INTO users(username,name,password_hash,role) VALUES (?,?,?,?)", (u, n, generate_password_hash(p), r))

    bom = conn.execute("INSERT INTO boms(name,yield_m) VALUES ('Murukku 200 g (1 batch = 200 packets)', ?)", (to_milli(200),)).lastrowid
    items = [  # code, name, kind, unit, hsn, gst_bp, price, shelf, reorder
        ("FLOUR", "Flour", "raw", "kg", "1101", 0, 0, None, "50"),
        ("OIL", "Cooking oil", "raw", "l", "1512", 0, 0, None, "10"),
        ("SPICE", "Salt and spice mix", "raw", "kg", "0910", 0, 0, None, "2"),
        ("POUCH200", "200 g pouch", "pack", "piece", "3923", 0, 0, None, "200"),
        ("CARTON", "Carton (holds 20 packets)", "pack", "piece", "4819", 0, 0, None, "10"),
        ("MUR200", "Murukku 200 g", "fg", "packet", "21069099", 500, paise(50), 90, "0"),
    ]
    ids = {}
    for code, name, kind, unit, hsn, bp, price, shelf, reorder in items:
        ids[code] = conn.execute(
            "INSERT INTO items(code,name,kind,unit,hsn,gst_bp,price_paise,shelf_life_days,reorder_m,bom_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (code, name, kind, unit, hsn, bp, price, shelf, to_milli(reorder), bom if kind == "fg" else None)).lastrowid
    for code, qty, rule in [("FLOUR", 40, "manual"), ("OIL", 10, "manual"), ("SPICE", "1.2", "manual"),
                            ("POUCH200", 200, "per_packet"), ("CARTON", 10, "manual")]:
        conn.execute("INSERT INTO bom_lines(bom_id,item_id,qty_m,rule) VALUES (?,?,?,?)", (bom, ids[code], to_milli(qty), rule))

    conn.execute("INSERT INTO customers(name,type,gstin,state,state_code,address,phone) VALUES (?,?,?,?,?,?,?)",
                 ("Retailer A", "retailer", "33AAAPA1234A1Z5", "Tamil Nadu", "33", "5 Gandhi Road, Coimbatore", "9000000001"))
    conn.execute("INSERT INTO customers(name,type,gstin,state,state_code,address,phone) VALUES (?,?,?,?,?,?,?)",
                 ("Distributor B", "distributor", "29AABCB5678B1Z2", "Karnataka", "29", "22 MG Road, Bengaluru", "9000000002"))
    conn.execute("INSERT INTO suppliers(name,gstin,phone) VALUES ('Sri Lakshmi Flour Mills','33AAACS9999K1Z1','9000000003')")

    # Opening stock. Rates are paise per stock unit. Opening FG cost of Rs 30 a packet is an assumption (see README).
    opening = [("FLOUR", 100, paise(42)), ("OIL", 30, paise(140)), ("SPICE", 5, paise(200)),
               ("POUCH200", 500, paise("2.50")), ("CARTON", 30, paise(15)), ("MUR200", 50, paise(30))]
    for code, qty, rate in opening:
        stock.post(conn, ids[code], to_milli(qty), "opening", None, "system", rate_paise=rate,
                   txn_date="2026-04-01", batch_no="OPENING" if code == "MUR200" else None, note="Opening balance")
    audit(conn, "system", "seed", None, "load", {"source": "brief section 5"})
    conn.commit()


def run(reset=False):
    path = dbm.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if reset and os.path.exists(path):
        os.remove(path)
    if os.path.exists(path):
        return False
    conn = dbm.connect(path)
    dbm.init_schema(conn)
    load(conn)
    conn.close()
    return True


if __name__ == "__main__":
    made = run(reset="--reset" in sys.argv)
    print("Database created at " + dbm.DB_PATH if made else "Database already exists (use --reset to rebuild)")
