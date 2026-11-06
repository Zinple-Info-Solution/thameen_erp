"""Every Debit Note issued to a vendor (the return Purchase Invoice a
Supplier Credit Note auto-creates on submit), with its VAT breakdown --
built for quarterly VAT return preparation, so it only needs the tax
figures, not the full rebate reconciliation the Rebate Ledger already
covers."""

import frappe
from frappe import _

DOCSTATUS_LABEL = {0: _("Draft"), 1: _("Submitted"), 2: _("Cancelled")}


def execute(filters=None):
	filters = frappe._dict(filters or {})
	return get_columns(), get_data(filters)


def get_data(filters):
	conditions = ["scn.docstatus = 1", "scn.debit_note is not null", "scn.debit_note != ''"]
	values = {}
	if filters.get("company"):
		conditions.append("pi.company = %(company)s")
		values["company"] = filters.company
	if filters.get("supplier"):
		conditions.append("pi.supplier = %(supplier)s")
		values["supplier"] = filters.supplier
	if filters.get("from_date"):
		conditions.append("pi.posting_date >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("pi.posting_date <= %(to_date)s")
		values["to_date"] = filters.to_date

	rows = frappe.db.sql(
		f"""
		select
		    pi.name as debit_note,
		    pi.posting_date,
		    pi.supplier,
		    pi.return_against as original_purchase_invoice,
		    pi.docstatus as pi_docstatus,
		    scn.name as supplier_credit_note,
		    scn.credit_note_number,
		    abs(pi.net_total) as taxable_amount,
		    abs(pi.total_taxes_and_charges) as vat_amount,
		    abs(pi.grand_total) as total_amount
		from `tabSupplier Credit Note` scn
		inner join `tabPurchase Invoice` pi on pi.name = scn.debit_note
		where {" and ".join(conditions)}
		order by pi.posting_date desc, pi.name desc
		""",
		values,
		as_dict=True,
	)

	for row in rows:
		row["status"] = DOCSTATUS_LABEL.get(row.pop("pi_docstatus"), "")
	return rows


def get_columns():
	return [
		{"label": _("Debit Note"), "fieldname": "debit_note", "fieldtype": "Link", "options": "Purchase Invoice", "width": 140},
		{"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
		{"label": _("Supplier"), "fieldname": "supplier", "fieldtype": "Link", "options": "Supplier", "width": 160},
		{"label": _("Original Purchase Invoice"), "fieldname": "original_purchase_invoice", "fieldtype": "Link", "options": "Purchase Invoice", "width": 160},
		{"label": _("Supplier Credit Note"), "fieldname": "supplier_credit_note", "fieldtype": "Link", "options": "Supplier Credit Note", "width": 160},
		{"label": _("Credit Note No."), "fieldname": "credit_note_number", "fieldtype": "Data", "width": 130},
		{"label": _("Taxable Amount"), "fieldname": "taxable_amount", "fieldtype": "Currency", "width": 130},
		{"label": _("VAT Amount"), "fieldname": "vat_amount", "fieldtype": "Currency", "width": 120},
		{"label": _("Total Amount"), "fieldname": "total_amount", "fieldtype": "Currency", "width": 130},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
	]
