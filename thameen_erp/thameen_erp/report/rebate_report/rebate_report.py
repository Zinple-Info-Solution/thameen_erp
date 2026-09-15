# Copyright (c) 2026, Thameen ERP and contributors
# For license information, please see license.txt

import frappe
from frappe import _


def execute(filters=None):
	filters = filters or {}
	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	return [
		{"label": _("Rebate"), "fieldname": "name", "fieldtype": "Link", "options": "Rebate", "width": 120},
		{"label": _("Date"), "fieldname": "date", "fieldtype": "Date", "width": 100},
		{"label": _("Supplier"), "fieldname": "supplier", "fieldtype": "Link", "options": "Supplier", "width": 150},
		{"label": _("Item"), "fieldname": "item", "fieldtype": "Link", "options": "Item", "width": 150},
		{"label": _("Purchase Invoice"), "fieldname": "invoice", "fieldtype": "Link", "options": "Purchase Invoice", "width": 150},
		{"label": _("Quantity"), "fieldname": "quantity", "fieldtype": "Float", "width": 100},
		{"label": _("Percentage"), "fieldname": "percentage", "fieldtype": "Percent", "width": 100},
		{"label": _("Amount"), "fieldname": "amount", "fieldtype": "Currency", "width": 120},
		{"label": _("Rebate Amount"), "fieldname": "rebate_amount", "fieldtype": "Currency", "width": 120},
		{"label": _("Total Amount"), "fieldname": "total_amount", "fieldtype": "Currency", "width": 120},
	]


def get_data(filters):
	conditions, values = get_conditions(filters)

	return frappe.db.sql(
		f"""
		SELECT
			name, date, supplier, item, invoice,
			quantity, percentage, amount, rebate_amount, total_amount
		FROM `tabRebate`
		WHERE docstatus < 2 {conditions}
		ORDER BY date DESC
		""",
		values,
		as_dict=1,
	)


def get_conditions(filters):
	conditions = ""
	values = {}

	if filters.get("from_date"):
		conditions += " AND date >= %(from_date)s"
		values["from_date"] = filters["from_date"]

	if filters.get("to_date"):
		conditions += " AND date <= %(to_date)s"
		values["to_date"] = filters["to_date"]

	if filters.get("supplier"):
		conditions += " AND supplier = %(supplier)s"
		values["supplier"] = filters["supplier"]

	if filters.get("item"):
		conditions += " AND item = %(item)s"
		values["item"] = filters["item"]

	if filters.get("invoice"):
		conditions += " AND invoice = %(invoice)s"
		values["invoice"] = filters["invoice"]

	return conditions, values