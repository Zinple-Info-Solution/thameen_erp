"""Delivery Note extensions: freight line + trip write-back."""

import frappe

from frappe.utils import flt


def validate(doc, method=None):
	if doc.get("custom_vehicle") and not doc.get("custom_delivery_trip"):
		cc = frappe.get_cached_value("Vehicle", doc.custom_vehicle, "custom_cost_center")
		for row in doc.items:
			if cc and not row.cost_center:
				row.cost_center = cc


def on_submit(doc, method=None):
	trip = doc.get("custom_delivery_trip")
	if not trip:
		return
	delivered = sum(flt(row.qty) for row in doc.items)
	frappe.db.set_value("Delivery Trip", trip, "custom_delivered_qty", delivered, update_modified=False)


def on_cancel(doc, method=None):
	trip = doc.get("custom_delivery_trip")
	if trip:
		frappe.db.set_value("Delivery Trip", trip, "custom_delivered_qty", 0, update_modified=False)
