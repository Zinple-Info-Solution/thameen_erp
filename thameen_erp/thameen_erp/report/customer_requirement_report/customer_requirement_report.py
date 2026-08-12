"""Customer Requirement pipeline with approval ageing."""

import frappe
from frappe import _
from frappe.utils import date_diff, nowdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	conditions = ["cr.docstatus < 2"]
	values = {}

	if filters.get("customer"):
		conditions.append("cr.customer = %(customer)s")
		values["customer"] = filters.customer
	if filters.get("status"):
		conditions.append("cr.status = %(status)s")
		values["status"] = filters.status
	if filters.get("from_date"):
		conditions.append("cr.transaction_date >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("cr.transaction_date <= %(to_date)s")
		values["to_date"] = filters.to_date
	if filters.get("pending_only"):
		conditions.append("cr.status in ('Pending Sales Approval', 'Pending Finance Approval')")

	rows = frappe.db.sql(
		f"""
		select cr.name, cr.customer, cr.customer_name, cr.transaction_date,
		       cr.requested_delivery_date, cr.delivery_location, cr.project,
		       cr.total_qty, cr.estimated_value, cr.status,
		       cr.credit_limit, cr.outstanding_amount, cr.credit_check_passed,
		       cr.sales_approved_by, cr.finance_approved_by, cr.sales_order
		from `tabCustomer Requirement` cr
		where {" and ".join(conditions)}
		order by cr.transaction_date desc
		""",
		values,
		as_dict=True,
	)

	today = nowdate()
	for row in rows:
		row["age"] = date_diff(today, row.transaction_date)
		row["credit_flag"] = _("Within Limit") if row.credit_check_passed else _("Over Limit")

	return get_columns(), rows


def get_columns():
	return [
		{"label": _("Requirement"), "fieldname": "name", "fieldtype": "Link", "options": "Customer Requirement", "width": 150},
		{"label": _("Customer"), "fieldname": "customer", "fieldtype": "Link", "options": "Customer", "width": 160},
		{"label": _("Date"), "fieldname": "transaction_date", "fieldtype": "Date", "width": 100},
		{"label": _("Age"), "fieldname": "age", "fieldtype": "Int", "width": 70},
		{"label": _("Required By"), "fieldname": "requested_delivery_date", "fieldtype": "Date", "width": 110},
		{"label": _("Location"), "fieldname": "delivery_location", "fieldtype": "Data", "width": 150},
		{"label": _("Project"), "fieldname": "project", "fieldtype": "Link", "options": "Project", "width": 130},
		{"label": _("Qty"), "fieldname": "total_qty", "fieldtype": "Float", "width": 90},
		{"label": _("Value"), "fieldname": "estimated_value", "fieldtype": "Currency", "width": 130},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 160},
		{"label": _("Credit"), "fieldname": "credit_flag", "fieldtype": "Data", "width": 110},
		{"label": _("Outstanding"), "fieldname": "outstanding_amount", "fieldtype": "Currency", "width": 130},
		{"label": _("Sales Order"), "fieldname": "sales_order", "fieldtype": "Link", "options": "Sales Order", "width": 150},
	]
