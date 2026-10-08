"""Printable reports built only from the saved payroll snapshot."""
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def render_payroll_pdf(rows, *, run=None):
    from .services import payroll_snapshot_summary

    font_dir = Path(reportlab.__file__).parent / "fonts"
    for name, filename in (("Payroll", "Vera.ttf"), ("PayrollBold", "VeraBd.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_dir / filename)))
    pdfmetrics.registerFontFamily("Payroll", normal="Payroll", bold="PayrollBold")
    ink = colors.HexColor("#16324f")
    style = ParagraphStyle("Payroll", fontName="Payroll", fontSize=8, leading=11, textColor=ink)
    right = ParagraphStyle("Number", parent=style, alignment=TA_RIGHT)
    title = ParagraphStyle("Title", parent=style, fontName="PayrollBold", fontSize=19, leading=24)
    heading = ParagraphStyle("Heading", parent=style, fontName="PayrollBold", fontSize=11, leading=15)

    def text(value, cell_style=style):
        return Paragraph(escape(str(value if value is not None else "")), cell_style)

    def number(value):
        return "Not set" if value in ("", None) else f"{Decimal(str(value)):,.2f}"

    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=landscape(letter), leftMargin=36, rightMargin=36,
                                 topMargin=42, bottomMargin=42, title="Payroll report",
                                 author=str(run.organization) if run else "TSCM", pdfVersion=(1, 4))
    summary = payroll_snapshot_summary(rows)
    story: list[Flowable] = [text("Payroll report", title)]
    if run:
        start, end = timezone.localtime(run.period_start), timezone.localtime(run.period_end)
        story += [text(run.organization.display_name or run.organization.legal_name, heading),
                  text(f"Period: {start:%b %d, %Y %H:%M} to {end:%b %d, %Y %H:%M %Z} (end not included)"),
                  text(f"Run: {run.pk} | Status: {run.get_status_display()}"),
                  text(f"Approved by: {run.approved_by or 'Not approved'} | "
                       f"Approved at: {timezone.localtime(run.approved_at).strftime('%b %d, %Y %H:%M %Z') if run.approved_at else 'Not approved'}")]
    story += [Spacer(1, 12), text(
        f"{summary['employee_count']} employees | {summary['row_count']} payroll lines | "
        f"Hours: {number(summary['total_hours'])} | Regular: {number(summary['regular_hours'])} | "
        f"Overtime: {number(summary['overtime_hours'])}", heading),
        text(f"Recorded estimates (USD): Pay {number(summary['estimated_pay'])} | "
             f"Bill {number(summary['estimated_bill'])} | Margin {number(summary['margin'])}"),
        text("Saved snapshot only. Estimates are not a payslip, tax calculation, or invoice. "
             "Blank rates/amounts are shown as Not set, not assumed to be zero."),
        Spacer(1, 12)]
    if any(summary["missing"][field] for field in ("estimated_pay", "estimated_bill", "margin")):
        story += [text(f"Totals exclude unset amounts: {summary['missing']['estimated_pay']} pay, "
                       f"{summary['missing']['estimated_bill']} bill, {summary['missing']['margin']} margin."),
                  Spacer(1, 8)]
    header_style = ParagraphStyle("Header", parent=style, fontName="PayrollBold", textColor=colors.white)
    data = [[text(label, header_style) for label in
             ("Employee / category / code", "Client / site", "Hours", "OT", "Pay / bill rate", "Est. pay", "Est. bill", "Margin")]]
    for row in rows:
        identity = "<br/>".join(escape(str(row.get(field) or "-")) for field in ("employee", "pay_category", "pay_code"))
        location = "<br/>".join(escape(str(row.get(field) or "-")) for field in ("client", "site"))
        data.append([Paragraph(identity, style), Paragraph(location, style),
                     text(number(row.get("total_hours")), right), text(number(row.get("overtime_hours")), right),
                     Paragraph(f"{number(row.get('pay_rate'))}<br/>{number(row.get('bill_rate'))}", right),
                     *[text(number(row.get(field)), right) for field in ("estimated_pay", "estimated_bill", "margin")]])
    if rows:
        data.append([text("TOTAL", heading), text("Recorded amounts"), text(number(summary["total_hours"]), right),
                     text(number(summary["overtime_hours"]), right), text("-"),
                     *[text(number(summary[field]), right) for field in ("estimated_pay", "estimated_bill", "margin")]])
    else:
        story.append(text("No payroll lines in this snapshot."))
    table = Table(data, colWidths=[142, 138, 48, 44, 64, 62, 62, 60], repeatRows=1, hAlign="LEFT",
                  splitInRow=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ink), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4f8")]),
        ("LINEBELOW", (0, 0), (-1, 0), .5, ink),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(table)
    notes = [row for row in rows if row.get("note") or row.get("exception")]
    if notes:
        story += [Spacer(1, 14), text("Snapshot notes", heading)]
        for row in notes:
            for field in ("note", "exception"):
                if row.get(field):
                    story.append(text(f"{row.get('employee', '')}: {row[field]}"))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Payroll", 8)
        canvas.setFillColor(ink)
        canvas.drawString(36, 24, "TSCM | Saved payroll snapshot | Confidential")
        canvas.drawRightString(756, 24, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
