-- Money is stored as integer paise. Quantities are integer thousandths of the
-- item's stock unit (40 kg is stored as 40000). No floats anywhere.

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('salesman','admin','production','accountant','owner'))
);

CREATE TABLE boms (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  yield_m INTEGER NOT NULL CHECK (yield_m > 0)      -- finished packets per batch
);

CREATE TABLE items (
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('raw','pack','fg')),
  unit TEXT NOT NULL,                               -- stock unit: kg, l, piece, packet
  hsn TEXT,
  gst_bp INTEGER NOT NULL DEFAULT 0,                -- 500 = 5%
  price_paise INTEGER NOT NULL DEFAULT 0,           -- selling price per unit (fg only)
  shelf_life_days INTEGER,
  reorder_m INTEGER NOT NULL DEFAULT 0,
  bom_id INTEGER REFERENCES boms(id)                -- fg items point at their BOM (can be shared)
);

CREATE TABLE bom_lines (
  id INTEGER PRIMARY KEY,
  bom_id INTEGER NOT NULL REFERENCES boms(id) ON DELETE CASCADE,
  item_id INTEGER NOT NULL REFERENCES items(id),
  qty_m INTEGER NOT NULL CHECK (qty_m >= 0),        -- per batch, in the item's stock unit
  rule TEXT NOT NULL DEFAULT 'manual' CHECK (rule IN ('manual','per_packet')),
  UNIQUE (bom_id, item_id)
);

CREATE TABLE customers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  type TEXT NOT NULL CHECK (type IN ('retailer','distributor')),
  gstin TEXT,
  state TEXT NOT NULL,
  state_code TEXT NOT NULL,
  address TEXT,
  phone TEXT
);

CREATE TABLE suppliers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  gstin TEXT,
  phone TEXT
);

-- Single source of truth for stock. Balances are always SUM() over this table.
CREATE TABLE stock_ledger (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  txn_date TEXT NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items(id),
  qty_m INTEGER NOT NULL,                           -- + receipt, - issue
  value_paise INTEGER NOT NULL,                     -- signed, same direction as qty
  source_type TEXT NOT NULL,                        -- opening, purchase, production_run, run_overhead, run_reversal, invoice, credit_note, adjust
  source_id INTEGER,
  batch_no TEXT,
  expiry TEXT,
  note TEXT,
  user TEXT
);
CREATE INDEX ix_ledger_item ON stock_ledger(item_id, txn_date);
CREATE INDEX ix_ledger_src ON stock_ledger(source_type, source_id);

CREATE TABLE purchases (
  id INTEGER PRIMARY KEY,
  txn_date TEXT NOT NULL,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  ref TEXT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  qty_m INTEGER NOT NULL CHECK (qty_m > 0),
  rate_paise INTEGER NOT NULL,                      -- paise per stock unit
  amount_paise INTEGER NOT NULL,
  user TEXT
);

CREATE TABLE production_runs (
  id INTEGER PRIMARY KEY,
  run_no TEXT UNIQUE,
  txn_date TEXT NOT NULL,
  product_id INTEGER NOT NULL REFERENCES items(id),
  bom_id INTEGER NOT NULL REFERENCES boms(id),
  batches INTEGER NOT NULL CHECK (batches >= 1),
  expected_m INTEGER NOT NULL,
  good_m INTEGER NOT NULL,
  damaged_m INTEGER NOT NULL DEFAULT 0,
  batch_no TEXT,
  expiry TEXT,
  status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','reversed')),
  note TEXT,
  user TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE run_lines (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES production_runs(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  planned_m INTEGER NOT NULL,
  actual_m INTEGER NOT NULL,
  cost_paise INTEGER NOT NULL
);

CREATE TABLE run_overheads (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES production_runs(id),
  kind TEXT NOT NULL,
  amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
  note TEXT,
  user TEXT
);

CREATE TABLE orders (
  id INTEGER PRIMARY KEY,
  order_no TEXT UNIQUE,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  salesman_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK (status IN ('submitted','approved','rejected','invoiced','dispatched','cancelled')),
  note TEXT,
  client_ref TEXT UNIQUE,                           -- makes offline re-sync idempotent
  created_at TEXT NOT NULL
);

CREATE TABLE order_lines (
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  qty_m INTEGER NOT NULL CHECK (qty_m > 0),
  price_paise INTEGER NOT NULL
);

CREATE TABLE doc_seq (key TEXT PRIMARY KEY, last INTEGER NOT NULL);

CREATE TABLE invoices (
  id INTEGER PRIMARY KEY,
  inv_no TEXT UNIQUE NOT NULL,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  inv_date TEXT NOT NULL,
  place_of_supply TEXT NOT NULL,
  interstate INTEGER NOT NULL,
  taxable_paise INTEGER NOT NULL,
  cgst_paise INTEGER NOT NULL,
  sgst_paise INTEGER NOT NULL,
  igst_paise INTEGER NOT NULL,
  total_paise INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('issued','dispatched','cancelled')),
  dispatched_at TEXT
);

CREATE TABLE invoice_lines (
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  item_id INTEGER NOT NULL REFERENCES items(id),
  hsn TEXT,
  qty_m INTEGER NOT NULL,
  rate_paise INTEGER NOT NULL,
  taxable_paise INTEGER NOT NULL,
  gst_bp INTEGER NOT NULL,
  cgst_paise INTEGER NOT NULL,
  sgst_paise INTEGER NOT NULL,
  igst_paise INTEGER NOT NULL
);

CREATE TABLE credit_notes (
  id INTEGER PRIMARY KEY,
  cn_no TEXT UNIQUE NOT NULL,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  cn_date TEXT NOT NULL,
  amount_paise INTEGER NOT NULL,
  reason TEXT NOT NULL,
  user TEXT
);

CREATE TABLE payments (
  id INTEGER PRIMARY KEY,
  invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  pay_date TEXT NOT NULL,
  amount_paise INTEGER NOT NULL CHECK (amount_paise > 0),
  mode TEXT NOT NULL,
  ref TEXT,
  user TEXT
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  user TEXT NOT NULL,
  entity TEXT NOT NULL,
  entity_id INTEGER,
  action TEXT NOT NULL,
  detail TEXT
);
