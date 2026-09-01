"""Fill Trip Route, Trip Start and Trip End on trips that predate them.

Route is derived from the (Supply Source, Destination) pair every existing trip
already carries, so nothing has to be guessed.

Start and End are a best effort from what the site already recorded. A trip that
never had these fields has no true clock times, so the honest fallback is the
document's own timeline: `departure_time` for a trip that got as far as Loading,
`modified` for one that finished. Both are written only where the field is empty,
so a hand-corrected time is never overwritten.
"""

import frappe
from frappe.utils import flt, time_diff_in_hours

ROUTE_OF_PAIR = {
	("Own Warehouse", "Customer"): "Warehouse to Customer",
	("Direct from Supplier", "Customer"): "Supplier to Customer",
	("Direct from Supplier", "Own Warehouse"): "Supplier to Warehouse",
	("Direct from Supplier", "Decide After Loading"): "Supplier to Decide After Loading",
}

# Statuses at or past which the truck has certainly started / finished.
STARTED = ("Loading", "In Transit", "Delivered", "POD Pending", "Completed")
FINISHED = ("Delivered", "POD Pending", "Completed")


def execute():
	if not frappe.db.exists("DocType", "Delivery Trip"):
		return

	meta = frappe.get_meta("Delivery Trip")
	for field in ("custom_trip_route", "custom_trip_start", "custom_trip_end"):
		if not meta.has_field(field):
			# install.py has not run yet on this site; after_migrate will.
			return

	trips = frappe.get_all(
		"Delivery Trip",
		fields=[
			"name",
			"status",
			"departure_time",
			"modified",
			"custom_supply_source",
			"custom_destination_type",
			"custom_trip_route",
			"custom_trip_start",
			"custom_trip_end",
		],
		limit_page_length=0,
	)

	for trip in trips:
		updates = {}

		if not trip.custom_trip_route:
			pair = (
				trip.custom_supply_source or "Own Warehouse",
				trip.custom_destination_type or "Customer",
			)
			route = ROUTE_OF_PAIR.get(pair)
			if route:
				updates["custom_trip_route"] = route

		if not trip.custom_trip_start and trip.status in STARTED and trip.departure_time:
			updates["custom_trip_start"] = trip.departure_time

		if not trip.custom_trip_end and trip.status in FINISHED and trip.modified:
			updates["custom_trip_end"] = trip.modified

		start = updates.get("custom_trip_start") or trip.custom_trip_start
		end = updates.get("custom_trip_end") or trip.custom_trip_end
		if start and end:
			hours = flt(time_diff_in_hours(end, start), 2)
			if hours >= 0:
				updates["custom_trip_duration_hours"] = hours
			else:
				# Clock times that run backwards are worse than none at all.
				updates.pop("custom_trip_end", None)

		if updates:
			frappe.db.set_value("Delivery Trip", trip.name, updates, update_modified=False)

	frappe.db.commit()
