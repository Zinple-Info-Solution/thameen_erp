"""Driver (ERPNext standard) extensions.

Frappe HR posts salary via Salary Structure Assignment -> payroll_cost_centers
(child table 'Employee Cost Center').  We rewrite that table so a driver's
salary lands on the assigned vehicle's cost center, rather than customising
Salary Slip itself.
"""

import frappe
from frappe import _
from frappe.utils import nowdate


def on_update(doc, method=None):
	_sync_vehicle_link(doc)
	if doc.get("custom_sync_payroll_cost_center"):
		sync_payroll_cost_center(doc)


def _sync_vehicle_link(doc):
	vehicle = doc.get("custom_assigned_vehicle")
	if not vehicle:
		return
	current = frappe.db.get_value("Vehicle", vehicle, "custom_assigned_driver")
	if current != doc.name:
		frappe.db.set_value(
			"Vehicle",
			vehicle,
			{"custom_assigned_driver": doc.name, "custom_status": "Assigned"},
			update_modified=False,
		)


def sync_payroll_cost_center(doc):
	if not (doc.get("employee") and doc.get("custom_assigned_vehicle")):
		return

	cost_center = frappe.db.get_value("Vehicle", doc.custom_assigned_vehicle, "custom_cost_center")
	if not cost_center:
		return

	ssa = frappe.db.get_value(
		"Salary Structure Assignment",
		{"employee": doc.employee, "docstatus": 1, "from_date": ("<=", nowdate())},
		"name",
		order_by="from_date desc",
	)
	if not ssa:
		return

	assignment = frappe.get_doc("Salary Structure Assignment", ssa)
	existing = [row.cost_center for row in assignment.get("payroll_cost_centers") or []]
	if existing == [cost_center]:
		return

	# Submitted document: child rows on an allow-on-submit table must be
	# written directly, then the parent notified.
	assignment.set("payroll_cost_centers", [])
	assignment.append("payroll_cost_centers", {"cost_center": cost_center, "percentage": 100})
	try:
		assignment.save(ignore_permissions=True)
	except frappe.ValidationError:
		frappe.msgprint(
			_("Could not update payroll cost center on {0}. Update it manually to {1}.").format(
				ssa, cost_center
			),
			indicator="orange",
			alert=True,
		)
