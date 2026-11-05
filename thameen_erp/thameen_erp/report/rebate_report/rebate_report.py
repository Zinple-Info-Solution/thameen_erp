# Copyright (c) 2026, Thameen ERP and contributors
# For license information, please see license.txt

"""Operational follow-up list: for each supplier, how much rebate is owed
back, how much of that has actually been received, and what is still
outstanding -- rolled up to one row per supplier so Accounts knows exactly
who to call.

Two independent rebate mechanisms feed this, since suppliers get tracked
through whichever one was actually used on their invoices:

  1. Expected Net Price -> Rebate Due -> Supplier Credit Note
     (`overrides.purchase`, `custom_expected_discount_amount`). Has a real
     Credit Note to settle against, so Received/Outstanding move as Credit
     Notes come in -- same tracking `supplier_credit_note_variance` reports
     line by line (`_credit_note_tracked_rows`).
  2. The flat "Add Rebate" amount typed in directly on an invoice line
     (`overrides.purchase_invoice`, `custom_rebate_amount`). Read straight
     off the Purchase Invoice Item, same as source 1 above -- NOT from the
     `Rebate` ledger doc that mechanism also creates, because that ledger
     is only ever created for a standalone line with no Purchase Order
     behind it (`_create_or_update_rebate` skips every PO-linked line on
     purpose); a PO-linked line is the normal case and would otherwise
     never show up here at all. Settled via the same Supplier Credit Note
     as source 1, using its separate `flat_rebate_received` field, written
     back to `custom_flat_rebate_received` on this same line
     (`_flat_rebate_rows`).
"""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, nowdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if filters.get("view") == "Credit Note Wise":
		return _execute_credit_note_wise(filters)

	rows = _credit_note_tracked_rows(filters) + _flat_rebate_rows(filters)

	credit_notes_by_pi_item = _credit_notes_for([r.pi_item for r in rows if r.get("pi_item")])

	today = nowdate()
	by_supplier = {}
	for row in rows:
		expected = flt(row.expected)
		received = flt(row.received)
		outstanding = max(expected - received, 0)

		bucket = by_supplier.setdefault(
			row.supplier,
			{
				"supplier": row.supplier, "open_lines": 0, "rebate_due": 0, "received": 0,
				"outstanding": 0, "oldest_date": None, "credit_notes": set(),
			},
		)
		bucket["rebate_due"] += expected
		bucket["received"] += received
		bucket["outstanding"] += outstanding
		if outstanding > 0:
			bucket["open_lines"] += 1
			if not bucket["oldest_date"] or row.posting_date < bucket["oldest_date"]:
				bucket["oldest_date"] = row.posting_date
		if row.get("pi_item"):
			bucket["credit_notes"].update(credit_notes_by_pi_item.get(row.pi_item, []))

	data = []
	for bucket in by_supplier.values():
		if filters.get("only_outstanding") and bucket["outstanding"] <= 0:
			continue
		data.append(
			{
				"supplier": bucket["supplier"],
				"open_lines": bucket["open_lines"],
				"rebate_due": bucket["rebate_due"],
				"received": bucket["received"],
				"outstanding": bucket["outstanding"],
				"oldest_pending_days": date_diff(today, bucket["oldest_date"]) if bucket["oldest_date"] else None,
				"credit_notes": ", ".join(sorted(bucket["credit_notes"])),
			}
		)

	data.sort(key=lambda r: r["outstanding"], reverse=True)

	return get_columns(), data, None, get_chart(data)


def _execute_credit_note_wise(filters):
	"""One row per submitted Supplier Credit Note document, instead of
	rolled up by supplier -- the same documents that feed the by-supplier
	view above, just not summed together."""
	conditions = ["scn.docstatus = 1"]
	values = {}
	if filters.get("supplier"):
		conditions.append("scn.supplier = %(supplier)s")
		values["supplier"] = filters.supplier
	if filters.get("company"):
		conditions.append("scn.company = %(company)s")
		values["company"] = filters.company
	if filters.get("from_date"):
		conditions.append("scn.credit_note_date >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("scn.credit_note_date <= %(to_date)s")
		values["to_date"] = filters.to_date

	rows = frappe.db.sql(
		f"""
		select scn.name as credit_note, scn.supplier, scn.purchase_invoice,
		       scn.credit_note_date, scn.status, scn.total_expected as rebate_due,
		       scn.total_actual as received, scn.total_variance as variance,
		       scn.debit_note
		from `tabSupplier Credit Note` scn
		where {" and ".join(conditions)}
		order by scn.credit_note_date desc, scn.name desc
		""",
		values,
		as_dict=True,
	)

	if filters.get("only_outstanding"):
		rows = [r for r in rows if flt(r.rebate_due) - flt(r.received) > 0]

	return get_credit_note_wise_columns(), rows, None, get_credit_note_wise_chart(rows)


def get_credit_note_wise_columns():
	return [
		{"label": _("Credit Note"), "fieldname": "credit_note", "fieldtype": "Link", "options": "Supplier Credit Note", "width": 140},
		{"label": _("Supplier"), "fieldname": "supplier", "fieldtype": "Link", "options": "Supplier", "width": 180},
		{"label": _("Purchase Invoice"), "fieldname": "purchase_invoice", "fieldtype": "Link", "options": "Purchase Invoice", "width": 150},
		{"label": _("Date"), "fieldname": "credit_note_date", "fieldtype": "Date", "width": 100},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 140},
		{"label": _("Rebate Due"), "fieldname": "rebate_due", "fieldtype": "Currency", "width": 120},
		{"label": _("Received"), "fieldname": "received", "fieldtype": "Currency", "width": 120},
		{"label": _("Variance"), "fieldname": "variance", "fieldtype": "Currency", "width": 120},
		{"label": _("Debit Note"), "fieldname": "debit_note", "fieldtype": "Link", "options": "Purchase Invoice", "width": 150},
	]


def get_credit_note_wise_chart(rows):
	buckets = {}
	for row in rows:
		buckets[row.status] = buckets.get(row.status, 0) + 1
	return {
		"data": {
			"labels": list(buckets),
			"datasets": [{"name": _("Credit Notes"), "values": list(buckets.values())}],
		},
		"type": "donut",
	}


def _credit_note_tracked_rows(filters):
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

	return frappe.db.sql(
		f"""
		select pi.supplier,
		       pii.name as pi_item,
		       pii.custom_expected_discount_amount as expected,
		       pii.custom_credit_note_received as received,
		       pi.posting_date
		from `tabPurchase Invoice Item` pii
		inner join `tabPurchase Invoice` pi on pi.name = pii.parent
		where {" and ".join(conditions)}
		""",
		values,
		as_dict=True,
	)


def _credit_notes_for(pi_items):
	"""Every submitted Supplier Credit Note settling a given Purchase
	Invoice line, keyed by that line -- same lookup `supplier_credit_note_
	variance` already does per line, used here to list every Credit Note
	behind a supplier's rebate total."""
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


def _flat_rebate_rows(filters):
	"""The flat 'Add Rebate' amount, read straight off the invoice line
	itself (see module docstring for why not the `Rebate` ledger doc).
	`received` comes from `custom_flat_rebate_received`, written back by a
	Supplier Credit Note's own `flat_rebate_received` field -- a separate
	settlement from the Expected Net Price mechanism on the same line."""
	conditions = ["pi.docstatus = 1", "pii.custom_rebate_amount > 0"]
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

	return frappe.db.sql(
		f"""
		select pi.supplier,
		       pii.name as pi_item,
		       pii.custom_rebate_amount as expected,
		       pii.custom_flat_rebate_received as received,
		       pi.posting_date
		from `tabPurchase Invoice Item` pii
		inner join `tabPurchase Invoice` pi on pi.name = pii.parent
		where {" and ".join(conditions)}
		""",
		values,
		as_dict=True,
	)


def get_chart(data):
	top = sorted(data, key=lambda r: r["outstanding"], reverse=True)[:10]
	return {
		"data": {
			"labels": [r["supplier"] for r in top],
			"datasets": [{"name": _("Outstanding"), "values": [r["outstanding"] for r in top]}],
		},
		"type": "bar",
	}


def get_columns():
	return [
		{"label": _("Supplier"), "fieldname": "supplier", "fieldtype": "Link", "options": "Supplier", "width": 200},
		{"label": _("Open Lines"), "fieldname": "open_lines", "fieldtype": "Int", "width": 100},
		{"label": _("Rebate Due"), "fieldname": "rebate_due", "fieldtype": "Currency", "width": 130},
		{"label": _("Credit Notes Received"), "fieldname": "received", "fieldtype": "Currency", "width": 160},
		{"label": _("Rebate Outstanding"), "fieldname": "outstanding", "fieldtype": "Currency", "width": 130},
		{"label": _("Oldest Pending (Days)"), "fieldname": "oldest_pending_days", "fieldtype": "Int", "width": 160},
		{"label": _("Credit Notes"), "fieldname": "credit_notes", "fieldtype": "Data", "width": 220},
	]
