"""Every purchase line that expects a supplier credit note, and where it
stands — by supplier, Purchase Order and invoice, so this doubles as the
operational list of what to request back from each supplier.

"Rebate Due" (this invoice's own rate vs. Expected Net Price) is the amount
actually owed and the one a Supplier Credit Note is raised against.
"Expected Rebate" is the same formula fixed to the Purchase Order's own
rate instead — normally the same number, but not always: a Price Flag
marks any row where the invoice was billed at a different rate than its
Purchase Order, which is exactly when the two diverge and are worth a
second look before the credit note is requested.
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, nowdate

PRICE_TOLERANCE = 0.01


def execute(filters=None):
	filters = frappe._dict(filters or {})
	conditions, values = build_conditions(filters)

	rows = frappe.db.sql(
		f"""
		select pi.name as purchase_invoice, pi.supplier, pi.posting_date,
		       pii.name as pi_item, pii.item_code, pii.qty, pii.rate,
		       pii.custom_agreed_net_price as expected_net_price,
		       pii.custom_expected_discount_amount as expected,
		       pii.custom_credit_note_received as received,
		       pii.custom_credit_note_status as status,
		       poi.parent as purchase_order, poi.rate as po_rate,
		       poi.custom_expected_discount_amount as po_expected_rebate
		from `tabPurchase Invoice Item` pii
		inner join `tabPurchase Invoice` pi on pi.name = pii.parent
		left join `tabPurchase Order Item` poi on poi.name = pii.po_detail
		where {conditions}
		order by pi.supplier, poi.parent, pi.posting_date
		""",
		values,
		as_dict=True,
	)

	credit_notes, debit_note_info = get_credit_notes([r.pi_item for r in rows])
	today = nowdate()
	docstatus_label = {0: _("Draft"), 1: _("Submitted"), 2: _("Cancelled")}

	data = []
	for row in rows:
		expected = flt(row.expected)
		received = flt(row.received)
		variance = received - expected
		po_rate = flt(row.po_rate) if row.purchase_order else None
		price_flag = bool(po_rate is not None and abs(flt(row.rate) - po_rate) > PRICE_TOLERANCE)

		scn_rows = credit_notes.get(row.pi_item, [])
		debit_note_names, debit_note_dates, debit_note_statuses = [], [], []
		for scn_row in scn_rows:
			if not scn_row.debit_note:
				debit_note_names.append(_("Not Raised"))
				continue
			info = debit_note_info.get(scn_row.debit_note)
			debit_note_names.append(scn_row.debit_note)
			debit_note_dates.append(str(info.posting_date) if info else "")
			debit_note_statuses.append(docstatus_label.get(info.docstatus, "") if info else "")

		data.append(
			{
				**row,
				"expected": expected,
				"received": received,
				"variance": variance,
				"pending": max(expected - received, 0),
				"age": date_diff(today, row.posting_date),
				"credit_notes": ", ".join(scn_row.scn_name for scn_row in scn_rows),
				"debit_note": ", ".join(debit_note_names),
				"debit_note_date": ", ".join(debit_note_dates),
				"debit_note_status": ", ".join(debit_note_statuses),
				"po_expected_rebate": flt(row.po_expected_rebate) if row.purchase_order else None,
				"price_flag": _("Review") if price_flag else "",
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
	"""For each invoice line: every submitted Supplier Credit Note covering
	it, each paired with the debit note (return Purchase Invoice) it raised
	-- the exact number/date/status question a credit note's accounting
	backing is routinely asked about, answered straight from the report."""
	if not pi_items:
		return {}, {}
	rows = frappe.db.sql(
		"""
		select scni.purchase_invoice_item as pi_item, scn.name as scn_name, scn.debit_note
		from `tabSupplier Credit Note Item` scni
		inner join `tabSupplier Credit Note` scn on scn.name = scni.parent
		where scn.docstatus = 1 and scni.purchase_invoice_item in %(pi_items)s
		""",
		{"pi_items": pi_items},
		as_dict=True,
	)
	out = {}
	for row in rows:
		out.setdefault(row.pi_item, []).append(row)

	debit_notes = {row.debit_note for row in rows if row.debit_note}
	debit_note_info = {}
	if debit_notes:
		debit_note_info = {
			d.name: d
			for d in frappe.get_all(
				"Purchase Invoice",
				filters={"name": ("in", list(debit_notes))},
				fields=["name", "posting_date", "docstatus"],
			)
		}
	return out, debit_note_info


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
		{"label": _("Supplier"), "fieldname": "supplier", "fieldtype": "Link", "options": "Supplier", "width": 150},
		{"label": _("Purchase Order"), "fieldname": "purchase_order", "fieldtype": "Link", "options": "Purchase Order", "width": 140},
		{"label": _("Purchase Invoice"), "fieldname": "purchase_invoice", "fieldtype": "Link", "options": "Purchase Invoice", "width": 140},
		{"label": _("Date"), "fieldname": "posting_date", "fieldtype": "Date", "width": 100},
		{"label": _("Age (Days)"), "fieldname": "age", "fieldtype": "Int", "width": 90},
		{"label": _("Item"), "fieldname": "item_code", "fieldtype": "Link", "options": "Item", "width": 130},
		{"label": _("Qty"), "fieldname": "qty", "fieldtype": "Float", "width": 80},
		{"label": _("Expected Net Price"), "fieldname": "expected_net_price", "fieldtype": "Currency", "width": 120},
		{"label": _("Invoice Rate"), "fieldname": "rate", "fieldtype": "Currency", "width": 110},
		{"label": _("PO Rate"), "fieldname": "po_rate", "fieldtype": "Currency", "width": 110},
		{"label": _("Price Flag"), "fieldname": "price_flag", "fieldtype": "Data", "width": 90},
		{"label": _("Expected Rebate (PO)"), "fieldname": "po_expected_rebate", "fieldtype": "Currency", "width": 140},
		{"label": _("Rebate Due"), "fieldname": "expected", "fieldtype": "Currency", "width": 120},
		{"label": _("Received"), "fieldname": "received", "fieldtype": "Currency", "width": 120},
		{"label": _("Variance"), "fieldname": "variance", "fieldtype": "Currency", "width": 120},
		{"label": _("Outstanding"), "fieldname": "pending", "fieldtype": "Currency", "width": 120},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 150},
		{"label": _("Credit Notes"), "fieldname": "credit_notes", "fieldtype": "Data", "width": 180},
		{"label": _("Debit Note"), "fieldname": "debit_note", "fieldtype": "Data", "width": 140},
		{"label": _("Debit Note Date"), "fieldname": "debit_note_date", "fieldtype": "Data", "width": 120},
		{"label": _("Debit Note Status"), "fieldname": "debit_note_status", "fieldtype": "Data", "width": 120},
	]
