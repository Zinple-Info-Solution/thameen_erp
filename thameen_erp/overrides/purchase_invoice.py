"""Supplier rebate tracking (Purchase Order / Receipt / Invoice) and the
Purchase Invoice's own Rebate ledger-document lifecycle.

A rebate is a flat amount the supplier has verbally agreed to knock off
each line, to come back later as a Credit Note -- same spirit as the
"Expected Net Price" mechanism in `overrides.purchase`, just entered
directly as a currency amount per line (`custom_rebate_amount`) instead of
a net price, and only kept when `Add Rebate` is checked on that document.

It NEVER touches `item.rate`, `amount` or any document total -- the
Purchase Order, Purchase Receipt and Purchase Invoice stay at the price the
supplier actually bills and is actually paid. `custom_rebate_amount` is
purely an internal-tracking number computed off the line's own (untouched)
rate, same as `custom_expected_discount_amount` in `overrides.purchase`.

It is only ever actually computed once, at the top of the chain: on a
Purchase Order line with no Purchase Order/Receipt behind it, or on a
standalone Purchase Receipt/Invoice line raised with no Purchase Order at
all. A Receipt or Invoice line raised FROM a Purchase Order already carries
that PO's own rate via the ordinary same-fieldname mapped-doc copy -- it is
skipped here, same reasoning as `overrides.purchase`: a line raised off a
Purchase Order is paid at that PO's own price, never adjusted afterwards
except via a Credit Note.

This mirrors what previously lived on Sales Invoice; rebates are a
purchase-side concept, so the whole calculation now hangs off purchasing.
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


def _raised_from_purchase_order(item):
	"""True if this line's rate already comes from a Purchase Order/Receipt
	line, under whichever fieldname this doctype uses for that link:
	Purchase Invoice Item -> `po_detail` (PO) / `pr_detail` (Receipt);
	Purchase Receipt Item -> `purchase_order_item`. A Purchase Order Item has
	none of these, so this is always False there -- a PO line is always the
	top of its own chain."""
	return bool(item.get("po_detail") or item.get("pr_detail") or item.get("purchase_order_item"))


def _apply_rebate_to_items(doc):
	"""`custom_rebate_amount` is typed in directly per line -- this only
	enforces where it is and isn't allowed to stand: zero when Add Rebate
	isn't checked, and never negative. `item.rate` and all totals are never
	touched. See module docstring.

	A line raised from a Purchase Order is left alone either way: it
	carries its own value across from the PO/Receipt it was raised from
	(the ordinary same-fieldname mapped-doc copy, same as
	`custom_agreed_net_price`) and is never force-zeroed or recomputed here
	-- the figure a dispatcher sees on the PO is the same figure they see
	all the way through, same as every other rebate field.
	"""
	for item in doc.items:
		if _raised_from_purchase_order(item):
			continue
		if not doc.get("custom_add_rebate"):
			item.custom_rebate_amount = 0
		else:
			item.custom_rebate_amount = max(flt(item.get("custom_rebate_amount")), 0)


def _create_or_update_rebate(doc):
	"""Create/update a draft Rebate per item when Add Rebate is checked."""
	if not doc.get("custom_add_rebate"):
		return

	for item in doc.items:
		if _raised_from_purchase_order(item):
			continue
		rebate_amount = flt(item.get("custom_rebate_amount"))
		if not rebate_amount:
			continue

		gross_amount = flt(item.rate) * flt(item.qty)
		net_amount = gross_amount - rebate_amount
		# Informational only now that the amount is typed in directly --
		# the Rebate ledger's own "percentage" column still shows what
		# share of the line that amount works out to.
		percentage = (rebate_amount / gross_amount * 100) if gross_amount else 0

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
