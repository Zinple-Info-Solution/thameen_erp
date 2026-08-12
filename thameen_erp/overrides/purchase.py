"""Purchase side: expected supplier discount and credit-note expectation."""

import frappe
from frappe import _
from frappe.utils import flt


def _set_agreed_net_price(doc):
	for row in doc.items:
		expected = flt(row.get("custom_expected_discount_amount"))
		if expected:
			if expected > flt(row.amount):
				frappe.throw(
					_("Row {0}: expected discount {1} cannot exceed the line amount {2}.").format(
						row.idx, expected, row.amount
					)
				)
			qty = flt(row.qty) or 1
			row.custom_agreed_net_price = flt(row.rate) - (expected / qty)
		else:
			row.custom_agreed_net_price = flt(row.rate)


def validate_expected_discount(doc, method=None):
	_set_agreed_net_price(doc)


def validate_purchase_invoice(doc, method=None):
	_set_agreed_net_price(doc)
	for row in doc.items:
		if flt(row.get("custom_expected_discount_amount")) and row.get(
			"custom_credit_note_status"
		) in (None, "", "Not Applicable"):
			row.custom_credit_note_status = "Expected"

	# Vehicle expense rows drive cost centers on purchase invoices too.
	from thameen_erp.overrides.fleet_expense import set_cost_center_from_vehicle

	set_cost_center_from_vehicle(doc)


def flag_expected_credit_notes(doc, method=None):
	"""On submit, make sure every discounted line is tracked as Expected."""
	for row in doc.items:
		if flt(row.get("custom_expected_discount_amount")):
			frappe.db.set_value(
				"Purchase Invoice Item",
				row.name,
				"custom_credit_note_status",
				"Expected",
				update_modified=False,
			)
