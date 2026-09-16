"""Turn off background Delivery Trip creation on Sales Order submit.

Dispatch was getting a trip in Draft with no vehicle or driver the instant an
order was submitted, whether they wanted one yet or not. Trip planning is now
a deliberate action — the "Create > Delivery Trips" button on the submitted
order — so the field's default has changed from 1 to 0. A Single doctype's
value is only ever written once on install, so an existing site keeps
whatever it already had; this patch brings that value into line with the
new default. Re-enable it in Thameen Fleet Settings if a site genuinely wants
trips drafted automatically.
"""

import frappe


def execute():
	if not frappe.db.exists("DocType", "Thameen Fleet Settings"):
		return

	frappe.db.set_single_value("Thameen Fleet Settings", "auto_create_delivery_trip", 0)
