"""Purchase side: expected supplier discount and credit-note reconciliation.

The cycle, end to end:
  1. A Purchase Order line carries `custom_agreed_net_price` — "Expected Net
     Price", the per-unit price the buyer and supplier actually agreed to,
     rebate included, entered by hand. `custom_expected_discount_amount` —
     "Expected Rebate" — is computed from it against this line's own rate
     ((rate - Expected Net Price) x qty): the rebate agreed with the
     supplier, to come back later as a Credit Note rather than be knocked
     off the price up front (`_compute_rebate_from_agreed_price`).
  2. Once the real Purchase Invoice comes in, its own rate — "Actual Purchase
     Price" — is written back onto the Purchase Order line it was raised
     against, so the two prices sit side by side for comparison
     (`write_back_actual_purchase_price`). The invoice line itself is flagged
     `custom_credit_note_status = "Expected"` (`flag_expected_credit_notes`,
     pre-existing).
  3. When the supplier's Credit Note actually arrives, it is recorded as a
     **Supplier Credit Note** (`thameen_erp.thameen_erp.doctype.
     supplier_credit_note`) — a dedicated doctype, not a plain `is_return`
     Purchase Invoice entered by hand: it fetches every line still owed
     against a chosen invoice, tracks expected vs. actual with a variance
     (requiring sign-off if the credit comes in over what was expected), and
     on submit writes `custom_credit_note_received` / `custom_credit_note_
     status` onto the Purchase Invoice Item itself — optionally raising the
     `is_return` Purchase Invoice ("Debit Note") that actually books it, via
     its own `return_against` link, not a guess. This module does not
     duplicate that matching; `sync_po_rebate_after_credit_note` below only
     mirrors the result of it.

Because the same formula runs separately on the PO and on the Invoice, each
against its own rate, "Expected Rebate" (PO) and "Rebate Due" (Invoice) can
legitimately differ — that gap IS the invoice-vs-PO price discrepancy the
Supplier Credit Note Variance report flags for review, not a bug. Rebate Due
is also what a Supplier Credit Note fetches as the amount owed back
(`SupplierCreditNote.fetch_expected_amounts`).

Every one of those fields — plus `custom_credit_note_outstanding`, which
Supplier Credit Note does not itself compute — is mirrored onto BOTH the
Purchase Order line AND every Purchase Receipt line raised off it too
(`_sync_po_rebate_from_pi_row`), so the rebate's status is visible from all
three documents, not only from the invoice it actually lives on.

The on_submit effect in step 2 is undone cleanly on cancel
(`reverse_actual_purchase_price`), and the mirrors above are kept in sync
through both a Supplier Credit Note's own submit and cancel.
"""

import frappe
from frappe import _
from frappe.utils import flt


def _compute_rebate_from_agreed_price(doc):
	"""Expected Net Price is what the buyer and supplier actually agreed to
	(entered by hand) — everything else about the rebate is derived from it
	against this line's own rate, never the other way round:

		Expected Rebate / Rebate Due = (this line's own rate - Expected Net
		Price) x qty

	On a Purchase Order or Purchase Receipt, "this line's own rate" is the
	PO/Receipt rate, fixed when it was raised — "Expected Rebate". On a
	Purchase Invoice it is the real invoiced rate, which may by now differ
	from the PO's own (that gap is exactly what the Supplier Credit Note
	Variance report's price-flag column exists to catch) — "Rebate Due".

	No Expected Net Price entered at all means no rebate is being tracked on
	that line, same meaning the field has always had.

	Rebate Per Unit (`custom_rebate_per_unit`) is the same gap before
	multiplying by qty — kept as its own stored field purely so the per-unit
	figure is visible at a glance, alongside the per-unit prices it is
	derived from, without anyone having to divide Expected Rebate by qty
	themselves.

	`custom_rebate_amount` ("Rebate Amount") belongs to the OTHER rebate
	mechanism (overrides.purchase_invoice — a flat amount typed in by hand)
	entirely and is never touched here, on a PO-linked line or otherwise --
	it carries across unchanged via the ordinary mapped-doc copy, same as
	every other field that isn't specific to this one.
	"""
	for row in doc.items:
		agreed = flt(row.get("custom_agreed_net_price"))
		if agreed and agreed < flt(row.rate):
			qty = flt(row.qty)
			per_unit = flt(row.rate) - agreed
			row.custom_rebate_per_unit = per_unit
			row.custom_expected_discount_amount = per_unit * qty
		else:
			row.custom_rebate_per_unit = 0
			row.custom_expected_discount_amount = 0


def _sync_credit_note_outstanding(doc):
	"""Expected Discount minus what has actually come back, floored at 0 —
	kept correct on every save of the invoice itself. The other path that
	changes `custom_credit_note_received` — a Supplier Credit Note applying
	against this line — recomputes Outstanding its own way, straight after
	submit/cancel (`sync_po_rebate_after_credit_note`)."""
	for row in doc.items:
		expected = flt(row.get("custom_expected_discount_amount"))
		received = flt(row.get("custom_credit_note_received"))
		row.custom_credit_note_outstanding = max(expected - received, 0)


def validate_expected_discount(doc, method=None):
	_compute_rebate_from_agreed_price(doc)


def validate_purchase_receipt(doc, method=None):
	"""Same comparison as the Purchase Order and Purchase Invoice, one stage
	earlier — the goods are in, but there is not necessarily an invoice yet
	to compare against. Expected Net Price carries across automatically
	from the Purchase Order line when the receipt is made off it; Actual
	Purchase Price is simply this receipt's own rate, mirrored under the
	same label the other two documents use.

	The usual order is PO -> Receipt -> Invoice, so a receipt is normally
	made before there is anything to track yet — Credit Note Status stays
	"Not Applicable" here until the invoice (and later a Credit Note)
	actually exist, same as any other new line, synced in from there by
	`_sync_po_rebate_from_pi_row`. The rarer case — a receipt made AFTER
	the invoice already has tracking on it — is covered here too, so this
	line does not sit stale just because it missed that sync.
	"""
	_compute_rebate_from_agreed_price(doc)
	for row in doc.items:
		row.custom_actual_purchase_price = flt(row.rate)
		if not row.get("purchase_order_item"):
			continue
		pi_row = frappe.db.get_value(
			"Purchase Invoice Item",
			{"po_detail": row.purchase_order_item, "docstatus": 1},
			["custom_credit_note_status", "custom_credit_note_received", "custom_credit_note_outstanding"],
			as_dict=True,
			order_by="creation desc",
		)
		if pi_row:
			row.custom_credit_note_status = pi_row.custom_credit_note_status
			row.custom_credit_note_received = flt(pi_row.custom_credit_note_received)
			row.custom_credit_note_outstanding = flt(pi_row.custom_credit_note_outstanding)


def validate_purchase_invoice(doc, method=None):
	if doc.get("is_return"):
		# A return/debit note settles a previous invoice -- it is not a
		# fresh purchase and carries no rebate tracking of its own,
		# however much `custom_agreed_net_price` it inherited via the
		# ordinary mapped-doc copy from the invoice it returns against.
		# What a return DOES affect -- the eligible quantity left on that
		# original invoice -- is handled separately, on submit/cancel, by
		# `adjust_rebate_for_return` below.
		for row in doc.items:
			row.custom_agreed_net_price = 0
			row.custom_expected_discount_amount = 0
			row.custom_actual_purchase_price = 0
			row.custom_credit_note_status = "Not Applicable"
			row.custom_credit_note_received = 0
			row.custom_credit_note_outstanding = 0
	else:
		_compute_rebate_from_agreed_price(doc)
		_sync_credit_note_outstanding(doc)
		for row in doc.items:
			# On the invoice itself, "actual" is simply its own rate — mirrored
			# under the same label the Purchase Order line shows once this
			# invoice's rate is written back onto it, so Agreed Net Price and
			# Actual Purchase Price read the same way in both places.
			row.custom_actual_purchase_price = flt(row.rate)
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


def write_back_actual_purchase_price(doc, method=None):
	"""The real price the supplier actually billed, written back onto the
	Purchase Order line it was raised against — Expected Net Price (already
	on the PO) and this now sit side by side for comparison. A Credit Note
	(`is_return`) settles a past invoice rather than being a fresh
	purchase, so it has no price of its own to write back.

	Credit Note Status / Received / Outstanding are mirrored across too
	(`_sync_po_rebate_from_pi_row`) — at this point that is just the fresh
	"Expected" state `flag_expected_credit_notes` set a moment ago in the
	same submit, but it keeps the PO line showing the same rebate tracking
	as the invoice from the very first save, not only once a Credit Note
	first touches it.
	"""
	if doc.get("is_return"):
		return
	for row in doc.items:
		if not row.get("po_detail"):
			continue
		frappe.db.set_value(
			"Purchase Order Item",
			row.po_detail,
			"custom_actual_purchase_price",
			flt(row.rate),
			update_modified=False,
		)
		_sync_po_rebate_from_pi_row(row.po_detail)


def reverse_actual_purchase_price(doc, method=None):
	"""Cancelling the invoice undoes the write-back above. If another
	submitted invoice still stands against the same Purchase Order line
	(a partial delivery billed across more than one invoice), that one's
	rate takes over instead of blanking the field outright."""
	if doc.get("is_return"):
		return
	for row in doc.items:
		if not row.get("po_detail"):
			continue

		remaining = frappe.get_all(
			"Purchase Invoice Item",
			filters={"po_detail": row.po_detail, "docstatus": 1, "parent": ("!=", doc.name)},
			fields=["rate"],
			order_by="creation desc",
			limit_page_length=1,
		)
		# A Currency column is NOT NULL (default 0) — None would fail the
		# write outright, not just leave the field looking empty.
		latest_rate = flt(remaining[0].rate) if remaining else 0
		frappe.db.set_value(
			"Purchase Order Item", row.po_detail, "custom_actual_purchase_price", latest_rate,
			update_modified=False,
		)

		if row.get("custom_credit_note_status") == "Expected":
			frappe.db.set_value(
				"Purchase Invoice Item", row.name, "custom_credit_note_status", "Cancelled",
				update_modified=False,
			)

		_sync_po_rebate_from_pi_row(row.po_detail)


def sync_po_rebate_after_credit_note(doc, method=None):
	"""A Supplier Credit Note's own controller (`SupplierCreditNote.
	update_invoice_lines`) already writes `custom_credit_note_received` and
	`custom_credit_note_status` onto each Purchase Invoice Item it settles,
	on both submit and cancel — that part is not this module's job to
	duplicate. What it does not do is recompute `custom_credit_note_
	outstanding`, or mirror any of the three onto the Purchase Order /
	Purchase Receipt lines, so this runs straight after it (wired in
	hooks.py to the same submit and cancel) to finish both."""
	for row in doc.items:
		pi_item = row.get("purchase_invoice_item")
		if not pi_item:
			continue

		current = frappe.db.get_value(
			"Purchase Invoice Item", pi_item,
			["custom_credit_note_received", "custom_expected_discount_amount", "po_detail"],
			as_dict=True,
		)
		if not current:
			continue

		outstanding = max(flt(current.custom_expected_discount_amount) - flt(current.custom_credit_note_received), 0)
		frappe.db.set_value(
			"Purchase Invoice Item", pi_item, "custom_credit_note_outstanding", outstanding,
			update_modified=False,
		)

		if current.po_detail:
			_sync_po_rebate_from_pi_row(current.po_detail)


def _sync_po_rebate_from_pi_row(po_detail):
	"""Mirror a Purchase Invoice line's own rebate tracking back onto both
	the Purchase Order line it was raised against AND every Purchase
	Receipt line raised off the same PO line — Credit Note Status /
	Received / Outstanding end up visible on all three documents, not only
	on the invoice where the tracking actually lives. If more than one
	submitted invoice stands against the same PO line, the most recently
	created one wins, same rule `reverse_actual_purchase_price` already
	uses for Actual Purchase Price. Nothing submitted left at all (the
	invoice was cancelled) resets both back to untouched.

	The Purchase Order line is found by its own `name` (`po_detail` IS
	that name); a Purchase Receipt line names it instead through its own
	`purchase_order_item` field — different fieldname, same value, and
	there can be more than one receipt (or more than one row) against a
	single PO line, so every match gets the same update, not just the
	newest.
	"""
	pi_row = frappe.db.get_value(
		"Purchase Invoice Item",
		{"po_detail": po_detail, "docstatus": 1},
		["custom_credit_note_status", "custom_credit_note_received", "custom_credit_note_outstanding"],
		as_dict=True,
		order_by="creation desc",
	)
	values = (
		{
			"custom_credit_note_status": pi_row.custom_credit_note_status,
			"custom_credit_note_received": flt(pi_row.custom_credit_note_received),
			"custom_credit_note_outstanding": flt(pi_row.custom_credit_note_outstanding),
		}
		if pi_row
		else {
			"custom_credit_note_status": "Not Applicable",
			"custom_credit_note_received": 0,
			"custom_credit_note_outstanding": 0,
		}
	)
	frappe.db.set_value("Purchase Order Item", po_detail, values, update_modified=False)

	pr_rows = frappe.get_all(
		"Purchase Receipt Item", filters={"purchase_order_item": po_detail}, pluck="name"
	)
	for pr_row_name in pr_rows:
		frappe.db.set_value("Purchase Receipt Item", pr_row_name, values, update_modified=False)


def adjust_rebate_for_return(doc, method=None):
	""""Eligible invoiced quantity, excluding returns" — when a Purchase
	Invoice return settles against an earlier invoice, any returned
	quantity shrinks that invoice line's own eligible quantity for rebate
	purposes: Rebate Due is recomputed using (that line's own qty minus
	whatever has been returned against it so far), not the line's full
	original qty. Run on both a return's submit and its cancel — the sum
	below only ever counts currently-submitted (docstatus 1) returns, so
	cancelling one naturally restores the eligible quantity it had taken
	away.

	The Purchase Order's own Expected Rebate is deliberately left alone —
	it is fixed to the ordered quantity by design (see
	_compute_rebate_from_agreed_price's docstring), never adjusted after
	the fact by what happens downstream on the invoice.

	Only lines raised off a Purchase Order are handled (matched by
	`po_detail`, the only reliable link back to a specific original line —
	a standalone invoice with no Purchase Order behind it has no such link
	to correlate a return against)."""
	if not doc.get("is_return") or not doc.get("return_against"):
		return

	po_details = {row.po_detail for row in doc.items if row.get("po_detail")}
	for po_detail in po_details:
		original = frappe.db.get_value(
			"Purchase Invoice Item",
			{"parent": doc.return_against, "po_detail": po_detail},
			["name", "qty", "rate", "custom_agreed_net_price", "custom_credit_note_received"],
			as_dict=True,
		)
		if not original:
			continue

		returned = flt(
			frappe.db.sql(
				"""
				select sum(pii.qty)
				from `tabPurchase Invoice Item` pii
				inner join `tabPurchase Invoice` pi on pi.name = pii.parent
				where pi.is_return = 1 and pi.docstatus = 1
				  and pi.return_against = %(original_invoice)s
				  and pii.po_detail = %(po_detail)s
				""",
				{"original_invoice": doc.return_against, "po_detail": po_detail},
			)[0][0]
		)  # negative, or 0/None if nothing currently stands returned

		eligible_qty = max(flt(original.qty) + returned, 0)
		agreed = flt(original.custom_agreed_net_price)
		rate = flt(original.rate)
		expected = (rate - agreed) * eligible_qty if agreed and agreed < rate else 0
		outstanding = max(expected - flt(original.custom_credit_note_received), 0)

		frappe.db.set_value(
			"Purchase Invoice Item",
			original.name,
			{
				"custom_expected_discount_amount": expected,
				"custom_credit_note_outstanding": outstanding,
			},
			update_modified=False,
		)
		_sync_po_rebate_from_pi_row(po_detail)
