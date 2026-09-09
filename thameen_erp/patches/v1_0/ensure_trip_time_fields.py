"""Repair Delivery Trip sites whose custom field columns were never created.

Frappe's `create_custom_fields` defers its `ALTER TABLE` to the end of the run
and only swallows DuplicateEntryError, so a single field it could not place
aborted the whole install and left the Delivery Trip table without the columns
the app writes to. On such a site, marking a trip Loading died with:

    OperationalError (1054, "Unknown column 'custom_trip_start' in 'SET'")

`install_customisations` is now fault-isolated and reconciles columns itself,
so re-running it is the repair. This patch does that, then re-runs the route /
start / end backfill, which bailed out on exactly these sites and is already
recorded as executed so it would never run again on its own.
"""

import frappe

TRIP_FIELDS = (
	"custom_trip_route",
	"custom_trip_start",
	"custom_trip_end",
	"custom_trip_duration_hours",
)


def execute():
	if not frappe.db.exists("DocType", "Delivery Trip"):
		return

	from thameen_erp.install import install_customisations

	if _missing_columns():
		install_customisations()
		frappe.clear_cache(doctype="Delivery Trip")
		frappe.db.commit()

	still_missing = _missing_columns()
	if still_missing:
		frappe.log_error(
			"Thameen ERP could not create these Delivery Trip columns: "
			f"{', '.join(still_missing)}. Check the Error Log for the underlying "
			"custom field failure.",
			"Thameen ERP: Delivery Trip columns missing",
		)
		return

	from thameen_erp.patches.v1_0.backfill_trip_route import execute as backfill_trip_route

	backfill_trip_route()


def _missing_columns():
	missing = []
	for fieldname in TRIP_FIELDS:
		try:
			if not frappe.db.has_column("Delivery Trip", fieldname):
				missing.append(fieldname)
		except Exception:
			missing.append(fieldname)
	return missing
