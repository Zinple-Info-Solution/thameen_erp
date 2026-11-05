import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now_datetime

TOLERANCE = 0.01  # currency rounding tolerance


class SupplierCreditNote(Document):
	def validate(self):
		self.validate_invoice()
		self.fetch_expected_amounts()
		self.calculate_variance()
		self.set_status()
		self.validate_variance_approval()

	def validate_invoice(self):
		if not self.purchase_invoice:
			return
		pi_supplier, docstatus = frappe.db.get_value(
			"Purchase Invoice", self.purchase_invoice, ["supplier", "docstatus"]
		)
		if pi_supplier != self.supplier:
			frappe.throw(
				_("Purchase Invoice {0} belongs to supplier {1}, not {2}.").format(
					self.purchase_invoice, pi_supplier, self.supplier
				)
			)
		if docstatus != 1:
			frappe.throw(_("Purchase Invoice {0} must be submitted.").format(self.purchase_invoice))

	def fetch_expected_amounts(self):
		"""Expected amount always comes from the invoice, never typed by hand.
		`flat_rebate_amount` is the OTHER, unrelated rebate mechanism's own
		amount -- settled independently of Rebate Due/Actual via
		`flat_rebate_received`/`flat_rebate_outstanding` below."""
		for row in self.items:
			if not row.purchase_invoice_item:
				continue
			pi_row = frappe.db.get_value(
				"Purchase Invoice Item",
				row.purchase_invoice_item,
				["item_code", "custom_expected_discount_amount", "custom_rebate_amount", "parent"],
				as_dict=True,
			)
			if not pi_row:
				frappe.throw(
					_("Row {0}: purchase invoice line {1} not found.").format(
						row.idx, row.purchase_invoice_item
					)
				)
			if pi_row.parent != self.purchase_invoice:
				frappe.throw(
					_("Row {0}: line belongs to invoice {1}.").format(row.idx, pi_row.parent)
				)
			row.item_code = pi_row.item_code
			row.expected_amount = flt(pi_row.custom_expected_discount_amount)
			row.flat_rebate_amount = flt(pi_row.custom_rebate_amount)

	def calculate_variance(self):
		for row in self.items:
			already = self.get_previously_received(row.purchase_invoice_item)
			cumulative = already + flt(row.actual_amount)
			row.variance = cumulative - flt(row.expected_amount)
			row.row_status = classify(flt(row.expected_amount), cumulative)

			already_flat = self.get_previously_received(
				row.purchase_invoice_item, fieldname="flat_rebate_received"
			)
			cumulative_flat = already_flat + flt(row.flat_rebate_received)
			row.flat_rebate_outstanding = max(flt(row.flat_rebate_amount) - cumulative_flat, 0)

		self.total_expected = sum(flt(r.expected_amount) for r in self.items)
		self.total_actual = sum(flt(r.actual_amount) for r in self.items)
		self.total_variance = sum(flt(r.variance) for r in self.items)
		self.variance_percent = (
			(self.total_variance / self.total_expected * 100) if self.total_expected else 0
		)

	def get_previously_received(self, pi_item, fieldname="actual_amount"):
		"""Sum of actual (or flat_rebate_received) amounts on other submitted
		credit notes for the same PI line."""
		if not pi_item:
			return 0
		return (
			flt(
				frappe.db.sql(
					f"""
					select sum(scni.{fieldname})
					from `tabSupplier Credit Note Item` scni
					inner join `tabSupplier Credit Note` scn on scn.name = scni.parent
					where scni.purchase_invoice_item = %(pi_item)s
					  and scn.docstatus = 1
					  and scn.name != %(self_name)s
					""",
					{"pi_item": pi_item, "self_name": self.name or ""},
				)[0][0]
			)
			or 0
		)

	def set_status(self):
		if self.docstatus == 2:
			self.status = "Cancelled"
			return
		statuses = {row.row_status for row in self.items}
		if not statuses:
			self.status = "Expected"
		elif statuses == {"Fully Received"}:
			self.status = "Fully Received"
		elif "Received Above Expected" in statuses:
			self.status = "Received Above Expected"
		elif statuses == {"Expected"}:
			self.status = "Expected"
		else:
			self.status = "Partially Received"

	def validate_variance_approval(self):
		"""A shortfall may be left open, but an overage must be signed off."""
		if self.docstatus != 1:
			return
		if self.status == "Received Above Expected" and not self.variance_approved_by:
			frappe.throw(
				_("Credit note exceeds the expected amount by {0}. A variance approver is required.").format(
					frappe.format_value(self.total_variance, {"fieldtype": "Currency"})
				)
			)

	def before_submit(self):
		if self.variance_approved_by and not self.variance_approved_on:
			self.variance_approved_on = now_datetime()

	def on_submit(self):
		self.update_invoice_lines()
		if self.create_debit_note:
			self.make_debit_note()

	def on_cancel(self):
		self.ignore_linked_doctypes = ("GL Entry", "Stock Ledger Entry")
		self.db_set("status", "Cancelled")
		self.update_invoice_lines(cancelling=True)

	def update_invoice_lines(self, cancelling=False):
		for row in self.items:
			if not row.purchase_invoice_item:
				continue
			received = self.get_previously_received(row.purchase_invoice_item)
			if not cancelling:
				received += flt(row.actual_amount)
			expected = flt(row.expected_amount)

			flat_received = self.get_previously_received(
				row.purchase_invoice_item, fieldname="flat_rebate_received"
			)
			if not cancelling:
				flat_received += flt(row.flat_rebate_received)

			frappe.db.set_value(
				"Purchase Invoice Item",
				row.purchase_invoice_item,
				{
					"custom_credit_note_received": received,
					"custom_credit_note_status": "Cancelled"
					if (cancelling and not received)
					else classify(expected, received),
					"custom_flat_rebate_received": flat_received,
				},
				update_modified=False,
			)

	def make_debit_note(self):
		"""Return Purchase Invoice for the credited Rebate Amount (the flat
		'Add Rebate' mechanism -- not Rebate Due/Expected Net Price, which
		this same Credit Note tracks and settles separately but does not
		raise a debit note for), carrying over its share of the original
		invoice's VAT -- a plain rate/qty return has no tax rows of its
		own, so without this the input VAT already claimed on the credited
		amount is never reversed."""
		if self.debit_note:
			return

		rows_to_credit = [r for r in self.items if flt(r.flat_rebate_received) > 0]
		if not rows_to_credit:
			return

		pi = frappe.get_doc("Purchase Invoice", self.purchase_invoice)
		dn = frappe.new_doc("Purchase Invoice")
		dn.supplier = self.supplier
		dn.company = self.company
		dn.is_return = 1
		dn.return_against = self.purchase_invoice
		dn.update_stock = 0
		dn.currency = pi.currency
		dn.conversion_rate = pi.conversion_rate
		dn.set_posting_time = 1
		dn.posting_date = self.credit_note_date
		dn.bill_no = self.credit_note_number
		dn.remarks = _("Supplier Credit Note {0}").format(self.name)

		for row in rows_to_credit:
			source = frappe.db.get_value(
				"Purchase Invoice Item",
				row.purchase_invoice_item,
				["item_code", "uom", "conversion_factor", "expense_account", "cost_center", "item_tax_template"],
				as_dict=True,
			)
			dn.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": -1,
					"rate": flt(row.flat_rebate_received),
					"uom": source.uom,
					"conversion_factor": source.conversion_factor or 1,
					"expense_account": source.expense_account,
					"cost_center": source.cost_center,
					"item_tax_template": source.item_tax_template,
					"purchase_invoice_item": row.purchase_invoice_item,
				},
			)

		# Percentage-based charges (the normal VAT setup) recompute
		# themselves from this return's own net total on save. A flat
		# "Actual" charge has nothing to recompute from, so it is scaled by
		# how much of the original invoice's value this note reverses.
		total_flat_received = sum(flt(r.flat_rebate_received) for r in rows_to_credit)
		ratio = (total_flat_received / flt(pi.net_total)) if pi.net_total else 0
		for t in pi.taxes:
			dn.append(
				"taxes",
				{
					"charge_type": t.charge_type,
					"account_head": t.account_head,
					"description": t.description,
					"rate": t.rate,
					"cost_center": t.cost_center,
					"included_in_print_rate": t.included_in_print_rate,
					"tax_amount": flt(t.tax_amount) * ratio if t.charge_type == "Actual" else 0,
				},
			)

		dn.flags.ignore_permissions = True
		dn.insert()
		dn.submit()
		self.db_set("debit_note", dn.name)
		frappe.msgprint(
			_("Debit Note {0} created against {1}").format(dn.name, self.purchase_invoice),
			indicator="green",
			alert=True,
		)


def classify(expected, received):
	expected = flt(expected)
	received = flt(received)
	if received <= 0:
		return "Expected"
	if abs(received - expected) <= TOLERANCE:
		return "Fully Received"
	if received < expected:
		return "Partially Received"
	return "Received Above Expected"


@frappe.whitelist()
def get_expected_lines(purchase_invoice):
	"""Pull every invoice line that still owes a credit note."""
	frappe.has_permission("Purchase Invoice", "read", doc=purchase_invoice, throw=True)

	rows = frappe.get_all(
		"Purchase Invoice Item",
		filters={"parent": purchase_invoice, "docstatus": 1},
		fields=[
			"name as purchase_invoice_item",
			"item_code",
			"qty",
			"custom_expected_discount_amount as expected_amount",
			"custom_credit_note_received as received",
			"custom_credit_note_status as status",
		],
		order_by="idx",
	)

	pending = []
	for row in rows:
		if flt(row.expected_amount) <= 0:
			continue
		if row.status == "Fully Received":
			continue
		row["actual_amount"] = max(flt(row.expected_amount) - flt(row.received), 0)
		pending.append(row)
	return pending
