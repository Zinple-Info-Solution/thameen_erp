"""The exact General Ledger report -- same filters, same columns, same
grouping/currency/dimension behaviour, since this literally calls
ERPNext's own `general_ledger.execute()` -- narrowed down to only the
rows a rebate (Supplier or Customer Credit Note) actually produced, with
the rebate's own context appended as extra columns at the end.

Calling the real General Ledger function, rather than re-querying GL
Entry by hand, is what keeps this in lockstep with it: any filter or
column ERPNext adds to General Ledger later is automatically available
here too.
"""

import frappe
from frappe import _
from frappe.utils import flt

from erpnext.accounts.report.general_ledger.general_ledger import execute as gl_execute

EXTRA_COLUMNS = [
	{"label": _("Side"), "fieldname": "side", "fieldtype": "Data", "width": 90},
	{"label": _("Credit Note Type"), "fieldname": "credit_note_type", "fieldtype": "Data", "width": 150},
	{
		"label": _("Supplier Credit Note"),
		"fieldname": "supplier_credit_note",
		"fieldtype": "Link",
		"options": "Supplier Credit Note",
		"width": 150,
	},
	{
		"label": _("Customer Credit Note"),
		"fieldname": "customer_credit_note",
		"fieldtype": "Link",
		"options": "Customer Credit Note",
		"width": 150,
	},
	{"label": _("Credit Note No."), "fieldname": "credit_note_number", "fieldtype": "Data", "width": 120},
	{"label": _("Credit Note Date"), "fieldname": "credit_note_date", "fieldtype": "Date", "width": 110},
	{
		"label": _("Original Purchase Invoice"),
		"fieldname": "original_purchase_invoice",
		"fieldtype": "Link",
		"options": "Purchase Invoice",
		"width": 150,
	},
	{
		"label": _("Original Sales Invoice"),
		"fieldname": "original_sales_invoice",
		"fieldtype": "Link",
		"options": "Sales Invoice",
		"width": 150,
	},
	{"label": _("Credit Note Status"), "fieldname": "credit_note_status", "fieldtype": "Data", "width": 130},
	{"label": _("Rebate Due"), "fieldname": "rebate_due", "fieldtype": "Currency", "width": 120},
	{"label": _("Rebate Amount"), "fieldname": "rebate_amount", "fieldtype": "Currency", "width": 120},
	{"label": _("Rebate Amount Received"), "fieldname": "rebate_amount_received", "fieldtype": "Currency", "width": 150},
	{"label": _("Rebate Amount Outstanding"), "fieldname": "rebate_amount_outstanding", "fieldtype": "Currency", "width": 160},
	{"label": _("Rebate Due Settled"), "fieldname": "actual_amount", "fieldtype": "Currency", "width": 130},
	{"label": _("Rebate Variance"), "fieldname": "rebate_variance", "fieldtype": "Currency", "width": 120},
	{"label": _("Credit Amount"), "fieldname": "credit_amount", "fieldtype": "Currency", "width": 120},
]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.get("company"):
		filters.company = frappe.defaults.get_user_default("Company")

	side = filters.pop("side", None) or "All"
	rebate_map = get_rebate_voucher_map()

	columns, rows = gl_execute(filters)
	for col in columns:
		if col.get("fieldname") == "balance":
			# Balance is a running total -- summing it down the column (what
			# the total row does for every other Currency column) produces a
			# meaningless number, since filtering to only the rebate-related
			# rows already breaks the running sequence it was computed over.
			col["disable_total"] = 1

	if not rows:
		return columns + EXTRA_COLUMNS, []

	data = []
	for row in rows:
		meta = rebate_map.get(row.get("voucher_no"))
		if not meta:
			continue
		if side not in ("All", "", None) and meta["side"] != _(side):
			continue
		row = dict(row)
		row.update(meta)
		data.append(row)

	return columns + EXTRA_COLUMNS, data, None, get_chart(data)


def get_rebate_voucher_map():
	"""voucher_no -> rebate context, for every return document a submitted
	Supplier or Customer Credit Note actually raised -- including each
	credit note's own component amounts (Rebate Due, Rebate Amount,
	Received, Outstanding, Variance), not just the GL total, since the GL
	total alone hides which of the two settlement mechanisms it came from."""
	out = {}
	scn_rows = frappe.get_all(
		"Supplier Credit Note",
		filters={"docstatus": 1, "debit_note": ("is", "set")},
		fields=[
			"name", "debit_note", "purchase_invoice", "credit_note_number", "credit_note_date",
			"status", "total_expected", "total_actual", "total_variance",
		],
	)
	scn_names = [r.name for r in scn_rows]
	flat_sums = {}
	if scn_names:
		for row in frappe.db.sql(
			"""
			select parent,
			       sum(flat_rebate_amount) as flat_rebate_amount,
			       sum(flat_rebate_received) as flat_rebate_received,
			       sum(flat_rebate_outstanding) as flat_rebate_outstanding
			from `tabSupplier Credit Note Item`
			where parent in %(names)s
			group by parent
			""",
			{"names": scn_names},
			as_dict=True,
		):
			flat_sums[row.parent] = row

	for r in scn_rows:
		flat = flat_sums.get(r.name, {})
		out[r.debit_note] = {
			"side": _("Supplier"),
			"credit_note_type": _("Purchase Rebate"),
			"credit_note_status": r.status,
			"supplier_credit_note": r.name,
			"customer_credit_note": None,
			"credit_note_number": r.credit_note_number,
			"credit_note_date": r.credit_note_date,
			"original_purchase_invoice": r.purchase_invoice,
			"original_sales_invoice": None,
			"rebate_due": flt(r.total_expected),
			"actual_amount": flt(r.total_actual),
			"rebate_variance": flt(r.total_variance),
			"rebate_amount": flt(flat.get("flat_rebate_amount")),
			"rebate_amount_received": flt(flat.get("flat_rebate_received")),
			"rebate_amount_outstanding": flt(flat.get("flat_rebate_outstanding")),
			"credit_amount": None,
		}

	ccn_rows = frappe.get_all(
		"Customer Credit Note",
		filters={"docstatus": 1, "return_invoice": ("is", "set")},
		fields=[
			"name", "return_invoice", "sales_invoice", "credit_note_number", "credit_note_date",
			"credit_note_type", "status", "total_amount",
		],
	)
	for r in ccn_rows:
		out[r.return_invoice] = {
			"side": _("Customer"),
			"credit_note_type": r.credit_note_type,
			"credit_note_status": r.status,
			"supplier_credit_note": None,
			"customer_credit_note": r.name,
			"credit_note_number": r.credit_note_number,
			"credit_note_date": r.credit_note_date,
			"original_purchase_invoice": None,
			"original_sales_invoice": r.sales_invoice,
			"rebate_due": None,
			"actual_amount": None,
			"rebate_variance": None,
			"rebate_amount": None,
			"rebate_amount_received": None,
			"rebate_amount_outstanding": None,
			"credit_amount": flt(r.total_amount),
		}
	return out


def get_chart(rows):
	buckets = {}
	for row in rows:
		buckets[row.get("side")] = buckets.get(row.get("side"), 0) + flt(row.get("credit")) - flt(row.get("debit"))
	return {
		"data": {
			"labels": list(buckets),
			"datasets": [{"name": _("Net Rebate/Credit Impact"), "values": list(buckets.values())}],
		},
		"type": "bar",
		"colors": ["#2490ef"],
	}
