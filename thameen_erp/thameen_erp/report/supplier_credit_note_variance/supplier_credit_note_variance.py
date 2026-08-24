"""Every purchase line that expects a supplier credit note, and where it stands."""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, nowdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	conditions, values = build_conditions(filters)

	rows = frappe.db.sql(
		f"""
		select pi.name as purchase_invoice, pi.supplier, pi.posting_date,
		       pii.name as pi_item, pii.item_code, pii.qty, pii.rate,
		       pii.custom_expected_discount_amount as expected,
		       pii.custom_credit_note_received as received,
		       pii.custom_credit_note_status as status
		from `tabPurchase Invoice Item` pii
		inner join `tabPurchase Invoice` pi on pi.name = pii.parent
		where {conditions}
		order by pi.posting_date
		""",
		values,
		as_dict=True,
	)

	credit_notes = get_credit_notes([r.pi_item for r in rows])
	today = nowdate()

	data = []
	for row in rows:
		expected = flt(row.expected)
		received = flt(row.received)
		variance = received - expected
		data.append(
			{
				**row,
				"expected": expected,
				"received": received,
				"variance": variance,
				"pending": max(expected - received, 0),
				"age": date_diff(today, row.posting_date),
				"credit_notes": ", ".join(credit_notes.get(row.pi_item, [])),
			}
		)

	if filters.get("only_variance"):
		data = [r for r in data if abs(r["variance"]) > 0.01]

	return get_columns(), data, None, get_chart(data)


def build_conditions(filters):
	conditions = ["pi.docstatus = 1", "pii.custom_expected_discount_amount > 0"]
	values = {}
	if filters.get("supplier"):
		conditions.append("pi.supplier = %(supplier)s")
		values["supplier"] = filters.supplier
	if filters.get("company"):
		conditions.append("pi.company = %(company)s")
		values["company"] = filters.company
	if filters.get("from_date"):
		conditions.append("pi.posting_date >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("pi.posting_date <= %(to_date)s")
		values["to_date"] = filters.to_date
	if filters.get("status"):
		conditions.append("pii.custom_credit_note_status = %(status)s")
		values["status"] = filters.status
	return " and ".join(conditions), values


def get_credit_notes(pi_items):
	if not pi_items:
		return {}
	rows = frappe.db.sql(
		"""
		select scni.purchase_invoice_item as pi_item, scn.name
		from `tabSupplier Credit Note Item` scni
		inner join `tabSupplier Credit Note` scn on scn.name = scni.parent
		where scn.docstatus = 1 and scni.purchase_invoice_item in %(pi_items)s
		""",
		{"pi_items": pi_items},
		as_dict=True,
	)
	out = {}
	for row in rows:
		out.setdefault(row.pi_item, []).append(row.name)
	return out


def get_chart(data):
	buckets = {}
	for row in data:
		buckets[row["status"]] = buckets.get(row["status"], 0) + flt(row["pending"])
	return {
		"data": {
			"labels": list(buckets),
			"datasets": [{"name": _("Pending Credit"), "values": list(buckets.values())}],
		},
		"type": "donut",
	}


def get_columns():
	return [
		{"label": _("Purchase Invoice"), "fieldname": "purchase_invoice", "fieldtype": "Link", "options": "Purchase Invoice", "width": 160},
		{"label": _("Supplier"), "fieldname": "supplier", "fieldtype": "Link", "options": "Supplier", "width": 150},
		{"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
		{"label": _("Age (Days)"), "fieldname": "age", "fieldtype": "Int", "width": 90},
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 130},
		{"label": _("Qty"), "fieldname": "qty", "fieldtype": "Float", "width": 80},
		{"label": _("Expected"), "fieldname": "expected", "fieldtype": "Currency", "width": 120},
		{"label": _("Received"), "fieldname": "received", "fieldtype": "Currency", "width": 120},
		{"label": _("Variance"), "fieldname": "variance", "fieldtype": "Currency", "width": 120},
		{"label": _("Pending"), "fieldname": "pending", "fieldtype": "Currency", "width": 120},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 150},
		{"label": _("Credit Notes"), "fieldname": "credit_notes", "fieldtype": "Data", "width": 180},
	]
