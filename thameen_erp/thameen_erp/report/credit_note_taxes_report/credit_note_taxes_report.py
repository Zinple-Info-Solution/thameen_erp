"""Every Credit Note issued to a customer (the return Sales Invoice a
Customer Credit Note auto-creates on submit), with its VAT breakdown --
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
	conditions = ["ccn.docstatus = 1", "ccn.return_invoice is not null", "ccn.return_invoice != ''"]
	values = {}
	if filters.get("company"):
		conditions.append("si.company = %(company)s")
		values["company"] = filters.company
	if filters.get("customer"):
		conditions.append("si.customer = %(customer)s")
		values["customer"] = filters.customer
	if filters.get("from_date"):
		conditions.append("si.posting_date >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("si.posting_date <= %(to_date)s")
		values["to_date"] = filters.to_date

	rows = frappe.db.sql(
		f"""
		select
		    si.name as credit_note,
		    si.posting_date,
		    si.customer,
		    si.return_against as original_sales_invoice,
		    si.docstatus as si_docstatus,
		    ccn.name as customer_credit_note,
		    ccn.credit_note_number,
		    ccn.credit_note_type,
		    abs(si.net_total) as taxable_amount,
		    abs(si.total_taxes_and_charges) as vat_amount,
		    abs(si.grand_total) as total_amount
		from `tabCustomer Credit Note` ccn
		inner join `tabSales Invoice` si on si.name = ccn.return_invoice
		where {" and ".join(conditions)}
		order by si.posting_date desc, si.name desc
		""",
		values,
		as_dict=True,
	)

	for row in rows:
		row["status"] = DOCSTATUS_LABEL.get(row.pop("si_docstatus"), "")
	return rows


def get_columns():
	return [
		{"label": _("Credit Note"), "fieldname": "credit_note", "fieldtype": "Link", "options": "Sales Invoice", "width": 140},
		{"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "width": 160},
		{"label": _("Original Sales Invoice"), "fieldname": "original_sales_invoice", "fieldtype": "Link", "options": "Sales Invoice", "width": 160},
		{"label": _("Customer Credit Note"), "fieldname": "customer_credit_note", "fieldtype": "Link", "options": "Customer Credit Note", "width": 160},
		{"label": _("Credit Note No."), "fieldname": "credit_note_number", "fieldtype": "Data", "width": 130},
		{"label": _("Credit Note Type"), "fieldname": "credit_note_type", "fieldtype": "Data", "width": 130},
		{"label": _("Taxable Amount"), "fieldname": "taxable_amount", "fieldtype": "Currency", "width": 130},
		{"label": _("VAT Amount"), "fieldname": "vat_amount", "fieldtype": "Currency", "width": 120},
		{"label": _("Total Amount"), "fieldname": "total_amount", "fieldtype": "Currency", "width": 130},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
	]
