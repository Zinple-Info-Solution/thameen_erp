"""Sales Invoice: vehicle cost centers and consolidated billing period checks.

Rebates live on the purchase side — see thameen_erp.overrides.purchase_invoice.
"""

import frappe
from frappe import _
from frappe.utils import get_link_to_form


def validate(doc, method=None):
	_apply_vehicle_cost_centers(doc)
	_validate_consolidation_period(doc)
	_validate_delivery_trip_completed(doc)


def _apply_vehicle_cost_centers(doc):
	"""Transportation rows post to the delivering vehicle's cost center.

	Three ways a row can name its vehicle, tried in order:
	  1. `custom_vehicle` already set by hand, or by an earlier pass.
	  2. `custom_delivery_trip` already set — look the vehicle up off it.
	  3. `so_detail` — a row invoiced straight off a Sales Order (see
	     `_validate_delivery_trip_completed`, which already requires the
	     trip behind it to be Completed by the time this runs) carries
	     neither of the above, since it never passed through a Delivery
	     Note. Its own Sales Order line is still enough to find the
	     Delivery Trip Item that was planned against it, and from there the
	     vehicle that actually carried it.
	"""
	for row in doc.items:
		vehicle = row.get("custom_vehicle")
		if not vehicle and row.get("custom_delivery_trip"):
			vehicle = frappe.db.get_value("Delivery Trip", row.custom_delivery_trip, "vehicle")
			row.custom_vehicle = vehicle
		if not vehicle and row.get("so_detail"):
			trip_name = frappe.db.get_value(
				"Delivery Trip Item", {"so_detail": row.so_detail}, "parent"
			)
			if trip_name:
				vehicle = frappe.db.get_value("Delivery Trip", trip_name, "vehicle")
				if vehicle:
					row.custom_vehicle = vehicle
					row.custom_delivery_trip = trip_name
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


def _validate_delivery_trip_completed(doc):
	"""An invoice made straight off a Sales Order locks in a qty and rate
	that a dispatcher could still change — redirecting a draft trip, or
	splitting a load across more trucks once a shortfall turns up — right
	up until delivery actually finishes. Billing before that is billing for
	cement that has not necessarily gone out the way the invoice says yet.

	Only checked against rows that trace to a Sales Order line (so_detail)
	AND that some Delivery Trip was actually planned against — an ordinary
	line with no trip behind it (a service charge, or an order outside the
	fleet flow entirely) is never held up by this. The server-side backstop
	for `sales_order.pending_delivery_trips`, which hides the Create ->
	Sales Invoice option on the Sales Order itself for the same reason —
	this still has to run here too, since nothing stops an invoice reaching
	`validate` some other way (the API, a script, an older cached page).
	"""
	from thameen_erp.overrides.delivery_trip import pending_trips_for_so_details

	so_details = {row.so_detail for row in doc.items if row.get("so_detail")}
	not_done = pending_trips_for_so_details(so_details)
	if not_done:
		frappe.throw(
			_(
				"This invoice covers a Sales Order line that is still being delivered. "
				"Wait until its Delivery Trip is Completed first: {0}."
			).format(
				", ".join(
					f"{get_link_to_form('Delivery Trip', t.name)} ({_(t.status)})" for t in not_done
				)
			),
			title=_("Delivery Not Complete"),
		)
