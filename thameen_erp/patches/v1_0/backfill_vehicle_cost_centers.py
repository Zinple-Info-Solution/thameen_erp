"""Generate cost centers and vehicle warehouses for pre-existing Vehicles."""

import frappe

from thameen_erp.overrides.vehicle import ensure_vehicle_masters


def execute():
	vehicles = frappe.get_all(
		"Vehicle",
		filters={"custom_cost_center": ("is", "not set")},
		pluck="name",
	)
	for name in vehicles:
		try:
			doc = frappe.get_doc("Vehicle", name)
			if not doc.get("custom_company"):
				doc.db_set(
					"custom_company",
					frappe.defaults.get_global_default("company"),
					update_modified=False,
				)
				doc.reload()
			ensure_vehicle_masters(doc)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"Backfill failed for Vehicle {name}")
