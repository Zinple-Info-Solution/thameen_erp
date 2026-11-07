"""QR code for the Debit Note print format -- an informational code
summarising the note (not a ZATCA QR, which only applies to documents
issued to customers, not to vendors)."""

import base64
from io import BytesIO

import frappe
import pyqrcode
from frappe.utils import fmt_money


def get_debit_note_qr(doc):
	if isinstance(doc, str):
		doc = frappe.get_doc("Purchase Invoice", doc)

	payload = "\n".join(
		filter(
			None,
			[
				f"Debit Note: {doc.name}",
				f"Supplier: {doc.supplier_name or doc.supplier}",
				f"Date: {doc.posting_date}",
				f"Original Purchase Invoice: {doc.return_against}" if doc.return_against else None,
				f"Credit Note No.: {doc.get('custom_credit_note_number')}"
				if doc.get("custom_credit_note_number")
				else None,
				f"Amount: {fmt_money(abs(doc.grand_total), currency=doc.currency)}",
			],
		)
	)

	qr = pyqrcode.create(payload, encoding="utf-8")
	with BytesIO() as buffer:
		qr.png(buffer, scale=4)
		buffer.seek(0)
		return base64.b64encode(buffer.getvalue()).decode("utf-8")
