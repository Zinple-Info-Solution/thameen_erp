"""Stamp the existing default transportation item onto historic freight records.

Correctness does not depend on this — `_append_freight_lines` falls back to the
Settings default when the field is blank. It runs so that the Vehicle
Profitability account set and any reporting on the new column see history too.

Only safe while the default has never changed. If it has, freight billed under
the old item would be relabelled with today's item, so the patch bails out when
it finds more than one transportation item already in use.
"""

import frappe


def execute():
	default_item = frappe.db.get_single_value(
		"Thameen Fleet Settings", "transportation_item"
	)
	if not default_item:
		return

	for doctype, amount_field in (
		("Delivery Trip", "custom_transportation_cost"),
		("Delivery Note", "custom_transportation_amount"),
	):
		if not frappe.db.has_column(doctype, "custom_transportation_item"):
			continue

		distinct = frappe.db.sql(
			f"""
			select distinct custom_transportation_item
			from `tab{doctype}`
			where custom_transportation_item is not null
			  and custom_transportation_item != ''
			"""
		)
		if len(distinct) > 1:
			frappe.log_error(
				title="backfill_transportation_item skipped",
				message=(
					f"{doctype} already uses {len(distinct)} different transportation items. "
					"Backfill skipped to avoid relabelling history."
				),
			)
			continue

		frappe.db.sql(
			f"""
			update `tab{doctype}`
			set custom_transportation_item = %(item)s
			where (custom_transportation_item is null or custom_transportation_item = '')
			  and ifnull({amount_field}, 0) > 0
			""",
			{"item": default_item},
		)

	frappe.db.commit()
