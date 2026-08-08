"""Shared cost-center resolution for vehicle expenses.

Works for Journal Entry, Expense Claim and Purchase Invoice: whichever child
table carries a `custom_vehicle`, the row's `cost_center` is forced to that
vehicle's cost center.  A header-level vehicle cascades to rows that have
none of their own.
"""

import frappe
from frappe import _

CHILD_TABLES = {
	"Journal Entry": "accounts",
	"Expense Claim": "expenses",
	"Purchase Invoice": "items",
}


def get_vehicle_cost_center(vehicle):
	if not vehicle:
		return None
	cc = frappe.get_cached_value("Vehicle", vehicle, "custom_cost_center")
	if not cc:
		frappe.msgprint(
			_("Vehicle {0} has no cost center yet. Re-save the Vehicle to generate one.").format(
				vehicle
			),
			indicator="orange",
			alert=True,
		)
	return cc


def set_cost_center_from_vehicle(doc, method=None):
	table_field = CHILD_TABLES.get(doc.doctype)
	if not table_field:
		return

	header_vehicle = doc.get("custom_vehicle")
	header_cc = get_vehicle_cost_center(header_vehicle)

	for row in doc.get(table_field) or []:
		row_vehicle = row.get("custom_vehicle") or header_vehicle
		if not row_vehicle:
			continue
		if not row.get("custom_vehicle") and header_vehicle:
			row.custom_vehicle = header_vehicle

		cc = header_cc if row_vehicle == header_vehicle else get_vehicle_cost_center(row_vehicle)
		if cc and row.meta.has_field("cost_center"):
			row.cost_center = cc

	if doc.get("custom_transport_type") == "External Transport" and not doc.get(
		"custom_external_transport_vendor"
	):
		frappe.throw(_("Select the External Transport Vendor."))
