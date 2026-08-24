"""Sales Invoice: split cement revenue from transportation revenue."""

import frappe
from frappe import _



def validate(doc, method=None):
	_apply_vehicle_cost_centers(doc)
	_validate_consolidation_period(doc)


def _apply_vehicle_cost_centers(doc):
	"""Transportation rows post to the delivering vehicle's cost center."""
	for row in doc.items:
		vehicle = row.get("custom_vehicle")
		if not vehicle and row.get("custom_delivery_trip"):
			vehicle = frappe.db.get_value("Delivery Trip", row.custom_delivery_trip, "vehicle")
			row.custom_vehicle = vehicle
		if not vehicle:
			continue
		cc = frappe.get_cached_value("Vehicle", vehicle, "custom_cost_center")
		if cc:
			row.cost_center = cc


def _validate_consolidation_period(doc):
	if not doc.get("custom_is_consolidated_run"):
		return
	if not (doc.get("custom_billing_from_date") and doc.get("custom_billing_to_date")):
		frappe.throw(_("Set the billing period on a consolidated invoice."))
	if doc.custom_billing_from_date > doc.custom_billing_to_date:
		frappe.throw(_("Billing period From date must precede the To date."))
