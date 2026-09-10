"""Purchase Invoice: supplier rebate calculation and Rebate document lifecycle.

A rebate is a percentage the supplier knocks off each line's price. The
percentage is held per Item (Item.custom_rebate_percentage), fetched onto the
invoice line, and applied only when `Add Rebate` is checked on the invoice.

This mirrors what previously lived on Sales Invoice; rebates are a purchase-side
concept, so the whole calculation now hangs off Purchase Invoice.
"""

import frappe
from frappe.utils import flt


def before_validate(doc, method=None):
	_apply_rebate_to_items(doc)


def on_update(doc, method=None):
	_create_or_update_rebate(doc)


def on_submit(doc, method=None):
	if not doc.get("custom_add_rebate"):
		return
	for r in frappe.get_all("Rebate", filters={"invoice": doc.name, "docstatus": 0}):
		rebate_doc = frappe.get_doc("Rebate", r.name)
		rebate_doc.flags.ignore_permissions = True
		rebate_doc.submit()


def on_cancel(doc, method=None):
	for r in frappe.get_all("Rebate", filters={"invoice": doc.name, "docstatus": 1}):
		rebate_doc = frappe.get_doc("Rebate", r.name)
		rebate_doc.flags.ignore_permissions = True
		rebate_doc.cancel()


def on_trash(doc, method=None):
	for r in frappe.get_all("Rebate", filters={"invoice": doc.name, "docstatus": 0}):
		frappe.delete_doc("Rebate", r.name, ignore_permissions=True)


def _apply_rebate_to_items(doc):
	"""Reduce each item's rate by its rebate % BEFORE Frappe computes amount/totals.

	Runs on before_validate, so item.amount, net_total, grand_total etc. calculated
	later in the same save will already reflect the reduced rate.

	price_list_rate is used as the stable "original price" reference so repeated
	saves never compound the discount.
	"""
	for item in doc.items:
		if not item.get("price_list_rate"):
			# capture a baseline once if it isn't already set
			item.price_list_rate = flt(item.rate)

		original_rate = flt(item.price_list_rate)
		percentage = flt(item.get("custom_rebate_percentage")) if doc.get("custom_add_rebate") else 0

		if percentage:
			rebate_per_unit = original_rate * percentage / 100
			item.rate = original_rate - rebate_per_unit
			item.custom_rebate_amount = rebate_per_unit * flt(item.qty)
		else:
			# rebate off, or no percentage on this item -> restore full price
			item.rate = original_rate
			item.custom_rebate_amount = 0


def _create_or_update_rebate(doc):
	"""Create/update a draft Rebate per item when Add Rebate is checked."""
	if not doc.get("custom_add_rebate"):
		return

	for item in doc.items:
		percentage = flt(item.get("custom_rebate_percentage"))
		if not percentage:
			continue

		original_rate = flt(item.get("price_list_rate") or item.rate)
		gross_amount = original_rate * flt(item.qty)
		rebate_amount = flt(item.get("custom_rebate_amount"))
		net_amount = flt(item.amount)  # already net of rebate, thanks to before_validate

		existing = frappe.db.get_value(
			"Rebate",
			{"invoice": doc.name, "item": item.item_code},
			["name", "docstatus"],
			as_dict=True,
		)

		if existing:
			if existing.docstatus == 1:
				continue
			rebate = frappe.get_doc("Rebate", existing.name)
		else:
			rebate = frappe.new_doc("Rebate")
			rebate.supplier = doc.supplier
			rebate.item = item.item_code
			rebate.invoice = doc.name

		rebate.percentage = percentage
		rebate.quantity = item.qty
		rebate.amount = gross_amount
		rebate.rebate_amount = rebate_amount
		rebate.total_amount = net_amount
		rebate.date = doc.posting_date

		rebate.flags.ignore_permissions = True
		rebate.save(ignore_permissions=True)
