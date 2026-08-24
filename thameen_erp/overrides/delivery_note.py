"""Delivery Note extensions: vehicle cost centers + trip write-back.

The write-back is keyed on `so_detail`, not on a position in the items table, so
it stays correct when a trip carries several items or several Sales Orders.
"""

import frappe
from frappe.utils import flt


def validate(doc, method=None):
	if not doc.get("custom_vehicle"):
		return

	cost_center = frappe.get_cached_value("Vehicle", doc.custom_vehicle, "custom_cost_center")
	if not cost_center:
		return

	for row in doc.items:
		if not row.cost_center:
			row.cost_center = cost_center


def on_submit(doc, method=None):
	_write_back(doc, delete=False)


def on_cancel(doc, method=None):
	_write_back(doc, delete=True)


def _write_back(doc, delete=False):
	trip = doc.get("custom_delivery_trip")
	if not trip:
		return

	rows = frappe.get_all(
		"Delivery Trip Item",
		filters={"parent": trip, "parenttype": "Delivery Trip"},
		fields=["name", "so_detail", "qty", "delivered_qty"],
	)

	if not rows:
		# Trip predates the child table — keep the old header behaviour.
		delivered = 0 if delete else sum(flt(row.qty) for row in doc.items)
		frappe.db.set_value(
			"Delivery Trip", trip, "custom_delivered_qty", delivered, update_modified=False
		)
		return

	by_detail = {row.so_detail: row for row in rows if row.so_detail}

	for dn_row in doc.items:
		trip_row = by_detail.get(dn_row.get("so_detail"))
		if not trip_row:
			continue

		frappe.db.set_value(
			"Delivery Trip Item",
			trip_row.name,
			{
				"delivered_qty": 0 if delete else flt(dn_row.qty),
				"delivery_note": None if delete else doc.name,
				"delivery_note_item": None if delete else dn_row.name,
			},
			update_modified=False,
		)
		trip_row.delivered_qty = 0 if delete else flt(dn_row.qty)

	frappe.db.set_value(
		"Delivery Trip",
		trip,
		"custom_delivered_qty",
		sum(flt(row.delivered_qty) for row in rows),
		update_modified=False,
	)
