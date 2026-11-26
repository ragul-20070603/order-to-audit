"""GST tax invoice and report PDFs, built with reportlab."""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from core import amount_in_words, fmt_qty, money
from db import setting

S = getSampleStyleSheet()
SMALL = ParagraphStyle("small", parent=S["Normal"], fontSize=8, leading=10)
BOLD = ParagraphStyle("bold", parent=SMALL, fontName="Helvetica-Bold")
GRID = TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 8),
                   ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")), ("VALIGN", (0, 0), (-1, -1), "TOP")])


def invoice_pdf(db, invoice_id):
    inv = db.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    cust = db.execute("SELECT * FROM customers WHERE id=?", (inv["customer_id"],)).fetchone()
    lines = db.execute("SELECT l.*, i.name, i.unit FROM invoice_lines l JOIN items i ON i.id=l.item_id "
                       "WHERE l.invoice_id=? ORDER BY l.id", (invoice_id,)).fetchall()
    cn = db.execute("SELECT * FROM credit_notes WHERE invoice_id=?", (invoice_id,)).fetchone()
    inter = bool(inv["interstate"])
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=f"Tax Invoice {inv['inv_no']}")
    story = [Paragraph("TAX INVOICE" + (" (CANCELLED)" if inv["status"] == "cancelled" else ""), S["Title"]),
             Paragraph(f"<b>{setting(db, 'company_name')}</b><br/>{setting(db, 'company_address')}<br/>"
                       f"GSTIN: {setting(db, 'company_gstin')} &nbsp; State: {setting(db, 'company_state')} "
                       f"({setting(db, 'company_state_code')})", SMALL), Spacer(1, 6)]
    head = [[Paragraph(f"<b>Invoice no:</b> {inv['inv_no']}<br/><b>Date:</b> {inv['inv_date']}<br/>"
                       f"<b>Place of supply:</b> {inv['place_of_supply']}<br/>"
                       f"<b>Supply type:</b> {'Inter-state (IGST)' if inter else 'Intra-state (CGST + SGST)'}", SMALL),
             Paragraph(f"<b>Bill to:</b><br/>{cust['name']}<br/>{cust['address'] or ''}<br/>"
                       f"GSTIN: {cust['gstin'] or 'Unregistered'}<br/>State: {cust['state']} ({cust['state_code']})", SMALL)]]
    t = Table(head, colWidths=[90 * mm, 92 * mm])
    t.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.4, colors.grey), ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.grey)]))
    story += [t, Spacer(1, 8)]
    tax_hdr = ["IGST"] if inter else ["CGST", "SGST"]
    rows = [["#", "Description", "HSN", "Qty", "Rate", "Taxable", "GST %"] + tax_hdr + ["Amount"]]
    for n, l in enumerate(lines, 1):
        tax = [money(l["igst_paise"])] if inter else [money(l["cgst_paise"]), money(l["sgst_paise"])]
        amt = l["taxable_paise"] + l["cgst_paise"] + l["sgst_paise"] + l["igst_paise"]
        rows.append([n, l["name"], l["hsn"] or "", f"{fmt_qty(l['qty_m'])} {l['unit']}", money(l["rate_paise"]),
                     money(l["taxable_paise"]), f"{l['gst_bp'] / 100:g}"] + tax + [money(amt)])
    tax_tot = [money(inv["igst_paise"])] if inter else [money(inv["cgst_paise"]), money(inv["sgst_paise"])]
    rows.append(["", "Total", "", "", "", money(inv["taxable_paise"]), ""] + tax_tot + [money(inv["total_paise"])])
    widths = [8, 38, 16, 18, 15, 20, 11] + ([18] if inter else [16, 16]) + [22]
    scale = 182 / sum(widths)
    t = Table(rows, colWidths=[w * scale * mm for w in widths])
    t.setStyle(GRID)
    t.setStyle(TableStyle([("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("ALIGN", (3, 1), (-1, -1), "RIGHT")]))
    story += [t, Spacer(1, 8),
              Paragraph(f"<b>Amount in words:</b> {amount_in_words(inv['total_paise'])}", SMALL), Spacer(1, 4)]
    if cn:
        story.append(Paragraph(f"<b>Cancelled by credit note {cn['cn_no']}</b> dated {cn['cn_date']}. Reason: {cn['reason']}", SMALL))
    story += [Spacer(1, 14), Paragraph("Computer-generated demo invoice. Goods once sold are not returnable without a credit note.", SMALL)]
    doc.build(story)
    return buf.getvalue()


def table_pdf(title, cols, rows, subtitle=""):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=10 * mm, bottomMargin=10 * mm)
    cell = ParagraphStyle("cell", parent=SMALL, fontSize=7, leading=8.5)
    data = [[Paragraph(f"<b>{c}</b>", cell) for c in cols]] + [[Paragraph(str(v), cell) for v in r] for r in rows]
    t = Table(data, repeatRows=1)
    t.setStyle(GRID)
    doc.build([Paragraph(title, S["Heading2"]), Paragraph(subtitle, SMALL), Spacer(1, 6), t])
    return buf.getvalue()
