"""Write FAKE test documents (pdf, docx, odt, xls, eml, nested zip) to tests/fixtures.
Every name, number and address is invented. python tools/make_doc_fixtures.py"""
import io
import os
import zipfile
from email.message import EmailMessage

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures")

REPORT = [
    "Daily Report - Job 2207 Riverbend Clinic Renovation",
    "Date: 07/18/2023",
    "Superintendent: Darnell Okafor",
    "Crew of 6 on site. Framed north wall, set 4 door frames.",
    "Inspector Priya Raman signed off on rough electrical.",
    "Call Darnell at (504) 555-0187 or darnell.okafor@riverbend-builders.example.",
    "Sub: Delta Glass LLC, EIN 72-1234567, 4410 Magazine St, New Orleans, LA 70115.",
    "Thanks,",
    "Darnell Okafor",
    "Project Superintendent",
]


def pdf(lines):
    """Minimal one-page PDF with a real text layer (Helvetica)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    body = "BT /F1 11 Tf 50 750 Td 14 TL " + " ".join(f"({esc(l)}) '" for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(body)} >>\nstream\n{body}\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(out.tell())
        out.write(f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for o in offs:
        out.write(f"{o:010d} 00000 n \n".encode())
    out.write(f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def docx(lines):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    paras = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{l.replace('&', '&amp;')}</w:t></w:r></w:p>"
                    for l in lines)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {ns}><w:body>{paras}</w:body></w:document>')
        z.writestr("docProps/core.xml",
                   '<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>Darnell Okafor</dc:creator></cp:coreProperties>')
    return buf.getvalue()


def odt(lines):
    paras = "".join(f"<text:p>{l.replace('&', '&amp;')}</text:p>" for l in lines)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        z.writestr("content.xml",
                   '<?xml version="1.0"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                   'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text>'
                   f'{paras}</office:text></office:body></office:document-content>')
    return buf.getvalue()


def xls():
    import datetime

    import xlwt

    wb = xlwt.Workbook()
    ws = wb.add_sheet("PO Log")
    rows = [["PO Number", "Vendor", "Contact Name", "Contact Phone", "Amount", "Order Date", "Description"],
            ["PO-2207-01", "Delta Glass LLC", "Marisol Vega", "(504) 555-0110", 18250, datetime.date(2023, 3, 2), "Storefront glazing"],
            ["PO-2207-02", "Crescent Lumber", "Tobias Hale", "(504) 555-0122", 9420.5, datetime.date(2023, 3, 9), "Framing lumber"],
            ["PO-2207-03", "Bayou Electric", "Priya Raman", "(504) 555-0133", 31800, datetime.date(2023, 4, 11), "Rough electrical"]]
    datefmt = xlwt.easyxf(num_format_str="MM/DD/YYYY")
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            if isinstance(v, datetime.date):
                ws.write(r, c, v, datefmt)
            else:
                ws.write(r, c, v)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def eml():
    m = EmailMessage()
    m["Subject"] = "RE: Punch list for Riverbend Clinic - Job 2207"
    m["From"] = "Darnell Okafor <darnell.okafor@riverbend-builders.example>"
    m["To"] = "Marisol Vega <marisol.vega@deltaglass.example>"
    m["Cc"] = "Tobias Hale <t.hale@crescentlumber.example>"
    m["Date"] = "Tue, 01 Aug 2023 09:14:00 -0500"
    m.set_content("Hi Marisol,\n\nThe storefront at the east entry still leaks at the sill. "
                  "Please have your crew back on 08/02/2023. My cell is 504-555-0187.\n\n"
                  "Thanks,\nDarnell Okafor\nRiverbend Builders\n")
    m.add_attachment(pdf(REPORT), maintype="application", subtype="pdf",
                     filename="Daily Report 07-18-2023.pdf")
    return bytes(m)


def main():
    os.makedirs(OUT, exist_ok=True)
    files = {"daily_report.pdf": pdf(REPORT), "daily_report.docx": docx(REPORT),
             "daily_report.odt": odt(REPORT), "po_log.xls": xls(), "punch_list.eml": eml()}
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("Photos Week 1/notes from Darnell Okafor.docx", files["daily_report.docx"])
        z.writestr("Photos Week 1/../../escape.txt", "zip-slip attempt must be ignored")
    files["job_2207_extras.zip"] = inner.getvalue()
    for name, data in files.items():
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(data)
    print("fixtures:", ", ".join(sorted(files)))


if __name__ == "__main__":
    main()
