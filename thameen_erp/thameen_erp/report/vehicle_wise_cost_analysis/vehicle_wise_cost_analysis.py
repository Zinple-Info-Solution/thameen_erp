"""Cost breakdown per vehicle by expense account, straight from the GL."""

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	cost_centers = frappe.get_all(
		"Vehicle",
		filters={"custom_cost_center": ("is", "set")},
		fields=["name as vehicle", "custom_cost_center as cost_center"],
	)
	if filters.get("vehicle"):
		cost_centers = [c for c in cost_centers if c.vehicle == filters.vehicle]
	if not cost_centers:
		return [], []

	cc_to_vehicle = {c.cost_center: c.vehicle for c in cost_centers}

	conditions = ["gle.is_cancelled = 0", "gle.cost_center in %(ccs)s", "acc.root_type = 'Expense'"]
	values = {"ccs": list(cc_to_vehicle)}
	if filters.get("company"):
		conditions.append("gle.company = %(company)s")
		values["company"] = filters.company
	if filters.get("from_date"):
		conditions.append("gle.posting_date >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("gle.posting_date <= %(to_date)s")
		values["to_date"] = filters.to_date

	rows = frappe.db.sql(
		f"""
		select gle.cost_center, gle.account,
		       sum(gle.debit) - sum(gle.credit) as amount,
		       count(distinct gle.voucher_no) as entries
		from `tabGL Entry` gle
		inner join `tabAccount` acc on acc.name = gle.account
		where {" and ".join(conditions)}
		group by gle.cost_center, gle.account
		having amount != 0
		order by gle.cost_center, amount desc
		""",
		values,
		as_dict=True,
	)

	data = [
		{
			"vehicle": cc_to_vehicle.get(r.cost_center),
			"cost_center": r.cost_center,
			"account": r.account,
			"entries": r.entries,
			"amount": flt(r.amount),
		}
		for r in rows
	]
	return get_columns(), data


def get_columns():
	return [
		{"label": _("Vehicle"), "fieldname": "vehicle", "fieldtype": "Link", "options": "Vehicle", "width": 130},
		{"label": _("Cost Center"), "fieldname": "cost_center", "fieldtype": "Link", "options": "Cost Center", "width": 170},
		{"label": _("Expense Account"), "fieldname": "account", "fieldtype": "Link", "options": "Account", "width": 260},
		{"label": _("Entries"), "fieldname": "entries", "fieldtype": "Int", "width": 90},
		{"label": _("Amount"), "fieldname": "amount", "fieldtype": "Currency", "width": 140},
	]
