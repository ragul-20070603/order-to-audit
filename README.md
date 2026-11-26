# Order-to-audit system for a small food factory

A working prototype for the CDI NIT Tiruchirappalli internship task. A salesman's order goes in once on a phone and ends up in the stock ledger, the GST invoice, the production costing and the auditor's monthly reports, without anyone re-typing it.

Demo video: [add unlisted link here]
Live demo: [add link here, or use the run command below]

## Run it

You need Python 3.10 or newer.

```
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. The first start creates `data/factory.db` and loads the benchmark master data from section 5 of the brief. To start again from scratch run `python seed.py --reset`.

Test logins:

| Role | Username | Password |
| --- | --- | --- |
| Admin (billing desk) | admin | admin123 |
| Salesman A | salesman_a | sales123 |
| Salesman B | salesman_b | sales123 |
| Production supervisor | production | prod123 |
| Accountant (read only) | accountant | acct123 |
| Owner | owner | owner123 |

Run the tests with `python -m pytest`. They run the whole section 5 scenario and assert every expected number.

## Stack and why

Python, Flask and SQLite, with server-rendered pages and a little plain JavaScript. The whole thing runs from one command with no build step, SQLite gives real transactions (a failed production run leaves nothing behind), and all money and stock logic sits in plain Python modules that can be tested without a browser. The order screen is a normal web page that works on a phone, so there is no app to install.

## What is built

Must have: login with five roles, product/customer/raw-material/BOM masters, mobile order entry with admin approval, GST invoice as PDF, production runs that move raw and finished stock, planned vs actual with variance flags, stock summary and ledger, sales register and stock report as CSV, Excel and PDF.

Should have: purchases and suppliers, payments with outstanding and ageing, cost per packet with overheads, GST summary (GSTR-1 and 3B shape) plus an HSN summary, variance alerts, owner dashboard.

Stretch goals done: offline order capture with automatic sync (see known gaps for the limit), batch numbers and expiry dates on finished goods, reorder levels with a low-stock list on the dashboard, a WhatsApp share link for invoices, audit trail report, run reversal and stock adjustments with reasons.

Also in: CSRF tokens on every form, credit notes instead of deleting invoices, stock that can never go negative, and an audit log of edits to orders, invoices, BOMs, runs and stock.

## Benchmark results

All figures come from the automated test in `tests/test_benchmark.py`, which follows steps 1 to 8 in order.

| Check | Expected | This system |
| --- | --- | --- |
| Flour closing balance | 120 kg | 120 kg |
| Oil closing balance | 19.5 L | 19.5 L |
| Pouches closing balance | 306 | 306 |
| Finished goods after run | 238 | 238 |
| Finished goods after Retailer A dispatch | 88 | 88 |
| Yield variance | -6%, flagged | -6.00%, flagged |
| Oil variance | +5%, flagged | +5.00%, flagged |
| Retailer A invoice | 7,500 + 187.50 + 187.50 = 7,875 | same, invoice INV/2026-27/0001 |
| Retailer A outstanding | 3,875 | 3,875.00 |
| Distributor B | shortage of 32, IGST if invoiced | shortage of 32 blocks approval; an invoice for the 88 in stock carries IGST |
| Cost per packet | our own figure | Rs 31.14 |

Cost per packet: materials Rs 4,055.00 (flour 1,725 + oil 1,470 + spice 240 + pouches 485 + cartons 135) plus labour 1,200 and gas/power 600, divided by 188 good packets. That is 5,855 / 188 = Rs 31.14, a margin of Rs 18.86 (37.7%) on the Rs 50 price. Flour is valued at the weighted average after the new purchase (160 kg worth Rs 6,900, so Rs 43.125 a kg), which is why 40 kg costs 1,725 and not 1,680 (all at the opening Rs 42) or 1,800 (all at the new Rs 45).

Two things the brief did not list that the system also flags: cartons (9 used against 10 planned, -10%) and the pouch line, which is exactly -3.00% and so sits on the tolerance without breaching it. The rule is "beyond 3%".

## Assumptions

- Opening finished goods (50 packets) are valued at Rs 30 a packet. The brief gives no cost for them. It only affects stock value, not any benchmark quantity.
- Weighted average is used for valuation. FIFO would need batch-level layers for raw materials, which is more than a one-batch benchmark justifies. The reasoning is in DESIGN.md.
- Overheads are entered per run and charged to that run in full. With one product per run, this is the allocation rule.
- Murukku is given HSN 21069099 at 5% GST, as the brief says to assume 5%. Raw material HSN codes are for reference only.
- The factory's state is Tamil Nadu (code 33). A customer in any other state gets IGST.
- GSTINs in the seed data are made up.
- Pouches are issued automatically as good packets plus damaged ones, which is how the brief gets to 306. Cartons are entered by hand (9 used).
- Admin approval checks finished goods minus what is already promised to other approved but undispatched orders, so two orders cannot both be approved against the same 88 packets.
- A payment cannot exceed the invoice's outstanding amount.

## Known gaps

- Offline orders are queued in the browser and sent when the connection returns, with a retry key so nothing is duplicated. The order page has to be open (or already loaded) when the signal drops, because there is no service worker caching the page itself.
- Batch numbers and expiry dates are recorded on every finished-goods receipt, but dispatch is not first-expiry-first-out. Stock is valued per item, not per batch.
- No partial dispatch, no part returns (cancelling an invoice reverses it in full), and an invoice with payments must have them sorted out before it can be cancelled.
- Purchases take one item per bill, and input GST on purchases is not tracked.
- No Tally export, e-invoice or e-way bill format, GPS check-in, or Tamil interface.
- Flask's built-in server is fine for a demo. For a real deployment put it behind gunicorn and set `SECRET_KEY`.
- Passwords are the seeded demo ones. There is no user management screen.

## Layout

```
app.py            web routes, roles, downloads
core.py           money, quantity, unit and GST helpers
stock.py          the stock ledger and weighted-average valuation
production.py     BOM, production runs, variance, costing
sales.py          orders, invoices, dispatch, payments, credit notes
reports.py        every auditor report as (columns, rows)
invoice_pdf.py    invoice and report PDFs
schema.sql        the database
seed.py           section 5 master data and demo users
tests/            benchmark scenario, tax and unit maths, web flow
```
