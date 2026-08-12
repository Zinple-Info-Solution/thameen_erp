"""Vehicle Log (HRMS standard) turned into a full maintenance log."""

import frappe
from frappe import _
from frappe.utils import flt


def validate(doc, method=None):
	_set_total_cost(doc)
	_validate_odometer(doc)


def _set_total_cost(doc):
	service_cost = sum(flt(row.expense_amount) for row in doc.get("service_detail") or [])
	fuel_cost = flt(doc.get("price"))
	doc.custom_total_cost = fuel_cost + service_cost + flt(doc.get("custom_labour_cost"))


def _validate_odometer(doc):
	if not doc.get("license_plate"):
		return
	last = frappe.db.get_value("Vehicle", doc.license_plate, "last_odometer") or 0
	if doc.get("odometer") and flt(doc.odometer) < flt(last):
		frappe.throw(
			_("Odometer reading {0} is lower than the vehicle's last reading {1}").format(
				doc.odometer, last
			)
		)


def on_submit(doc, method=None):
	if doc.get("custom_log_type") and doc.custom_log_type != "Refuelling":
		frappe.db.set_value("Vehicle", doc.license_plate, "custom_status", "Under Maintenance")
	if doc.get("custom_next_service_due"):
		frappe.db.set_value(
			"Vehicle", doc.license_plate, "custom_next_service_due", doc.custom_next_service_due
		)


def on_cancel(doc, method=None):
	open_maintenance = frappe.db.count(
		"Vehicle Log",
		{
			"license_plate": doc.license_plate,
			"docstatus": 1,
			"custom_log_type": ("!=", "Refuelling"),
			"name": ("!=", doc.name),
		},
	)
	if not open_maintenance:
		frappe.db.set_value("Vehicle", doc.license_plate, "custom_status", "Available")
