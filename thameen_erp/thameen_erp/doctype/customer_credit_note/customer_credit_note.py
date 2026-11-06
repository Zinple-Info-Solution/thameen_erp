import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

CREDIT_ACCOUNTS = {
	"Sales Return": "Sales Returns",
	"Sales Discount": "Sales Discounts",
}

RETURN_REASONS = {
	"Sales Return": "Goods returned",
	"Sales Discount": "Price adjustment agreed with customer",
}


class CustomerCreditNote(Document):
	def validate(self):
		self.validate_invoice()
		self.fetch_item_details()
		self.validate_against_remaining()
		self.calculate_total()
		self.set_status()

	def validate_invoice(self):
		if not self.sales_invoice:
			return
		si_customer, docstatus = frappe.db.get_value(
			"Sales Invoice", self.sales_invoice, ["customer", "docstatus"]
		)
		if si_customer != self.customer:
			frappe.throw(
				_("Sales Invoice {0} belongs to customer {1}, not {2}.").format(
					self.sales_invoice, si_customer, self.customer
				)
			)
		if docstatus != 1:
			frappe.throw(_("Sales Invoice {0} must be submitted.").format(self.sales_invoice))

	def fetch_item_details(self):
		for row in self.items:
			if not row.sales_invoice_item:
				continue
			si_row = frappe.db.get_value(
				"Sales Invoice Item",
				row.sales_invoice_item,
				["item_code", "qty", "rate", "parent"],
				as_dict=True,
			)
			if not si_row:
				frappe.throw(
					_("Row {0}: sales invoice line {1} not found.").format(row.idx, row.sales_invoice_item)
				)
			if si_row.parent != self.sales_invoice:
				frappe.throw(_("Row {0}: line belongs to invoice {1}.").format(row.idx, si_row.parent))
			row.item_code = si_row.item_code
			row.qty = si_row.qty
			row.rate = si_row.rate
			row.max_creditable = max(
				flt(si_row.qty) * flt(si_row.rate) - self.get_previously_credited(row.sales_invoice_item), 0
			)

	def validate_against_remaining(self):
		"""A line can never be credited past what it was actually invoiced
		for, counting every other submitted Customer Credit Note against the
		same line -- the sales-side mirror of the stock-return cap a return
		Sales Invoice would otherwise enforce only at the very last line."""
		for row in self.items:
			if not row.sales_invoice_item:
				continue
			if flt(row.amount) - flt(row.max_creditable) > 0.01:
				frappe.throw(
					_("Row {0}: cannot credit {1}, only {2} remains on this line.").format(
						row.idx,
						frappe.format_value(row.amount, {"fieldtype": "Currency"}),
						frappe.format_value(row.max_creditable, {"fieldtype": "Currency"}),
					)
				)

	def get_previously_credited(self, si_item):
		if not si_item:
			return 0
		return (
			flt(
				frappe.db.sql(
					"""
					select sum(ccni.amount)
					from `tabCustomer Credit Note Item` ccni
					inner join `tabCustomer Credit Note` ccn on ccn.name = ccni.parent
					where ccni.sales_invoice_item = %(si_item)s
					  and ccn.docstatus = 1
					  and ccn.name != %(self_name)s
					""",
					{"si_item": si_item, "self_name": self.name or ""},
				)[0][0]
			)
			or 0
		)

	def calculate_total(self):
		self.total_amount = sum(flt(r.amount) for r in self.items)

	def set_status(self):
		if self.docstatus == 2:
			self.status = "Cancelled"
		elif self.docstatus == 1:
			self.status = "Issued"
		else:
			self.status = "Draft"

	def on_submit(self):
		if self.create_return_invoice:
			self.make_return_invoice()

	def on_cancel(self):
		self.ignore_linked_doctypes = ("GL Entry", "Stock Ledger Entry")
		self.db_set("status", "Cancelled")

	def make_return_invoice(self):
		"""Return Sales Invoice for the credited amount -- carrying over its
		share of the original invoice's Output VAT, since a plain rate/qty
		return has no tax rows of its own, and posting the credit itself
		against Sales Returns or Sales Discounts (never back into ordinary
		Sales income) depending on `credit_note_type`. This is what makes
		the credit visible in the General Ledger: the Customer Credit Note
		itself never posts anything on its own, only this return Sales
		Invoice does -- adjusting Accounts Receivable, Output VAT, and the
		Sales Return/Discount account in one document."""
		if self.return_invoice:
			return

		rows_to_credit = [r for r in self.items if flt(r.amount) > 0]
		if not rows_to_credit:
			return

		si = frappe.get_doc("Sales Invoice", self.sales_invoice)
		account = get_credit_account(self.company, self.credit_note_type)

		ret = frappe.new_doc("Sales Invoice")
		ret.customer = self.customer
		ret.company = self.company
		ret.is_return = 1
		ret.return_against = self.sales_invoice
		ret.update_stock = 0
		ret.currency = si.currency
		# ksa_compliance (ZATCA e-invoicing) requires a reason on every
		# return/debit invoice -- BR-KSA-17 -- which has no equivalent
		# requirement on the purchase side.
		ret.custom_return_reason = RETURN_REASONS.get(self.credit_note_type, self.credit_note_type)
		ret.conversion_rate = si.conversion_rate
		ret.remarks = _("Customer Credit Note {0}").format(self.name)

		for row in rows_to_credit:
			source = frappe.db.get_value(
				"Sales Invoice Item",
				row.sales_invoice_item,
				["item_code", "uom", "conversion_factor", "cost_center", "item_tax_template"],
				as_dict=True,
			)
			ret.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": -1,
					"rate": flt(row.amount),
					"uom": source.uom,
					"conversion_factor": source.conversion_factor or 1,
					"income_account": account,
					"cost_center": source.cost_center,
					"item_tax_template": source.item_tax_template,
					"sales_invoice_item": row.sales_invoice_item,
				},
			)

		# Percentage-based charges (the normal Output VAT setup) recompute
		# themselves from this return's own net total on save. A flat
		# "Actual" charge has nothing to recompute from, so it is scaled by
		# how much of the original invoice's value this note reverses.
		total_credited = sum(flt(r.amount) for r in rows_to_credit)
		ratio = (total_credited / flt(si.net_total)) if si.net_total else 0
		for t in si.taxes:
			ret.append(
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

		ret.flags.ignore_permissions = True
		ret.insert()
		ret.submit()
		self.db_set("return_invoice", ret.name)
		frappe.msgprint(
			_("Return Sales Invoice {0} created against {1}").format(ret.name, self.sales_invoice),
			indicator="green",
			alert=True,
		)


def get_credit_account(company, credit_note_type):
	"""The dedicated Sales Returns / Sales Discounts income account for this
	company, created on first use under the same parent as the ordinary
	Sales account -- so a return's income impact is always visible apart
	from real sales, split further by whether goods actually came back."""
	account_name = CREDIT_ACCOUNTS.get(credit_note_type)
	if not account_name:
		frappe.throw(_("Unknown Credit Note Type: {0}").format(credit_note_type))

	abbr = frappe.get_cached_value("Company", company, "abbr")
	full_name = f"{account_name} - {abbr}"
	if frappe.db.exists("Account", full_name):
		return full_name

	parent = frappe.db.get_value(
		"Account", {"company": company, "account_name": "Direct Income", "is_group": 1}
	) or frappe.db.get_value("Account", {"company": company, "account_name": "Income", "is_group": 1})
	if not parent:
		frappe.throw(_("No Income group account found on company {0} to create {1} under.").format(company, account_name))

	account = frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": account_name,
			"company": company,
			"parent_account": parent,
			"is_group": 0,
			"root_type": "Income",
			"account_type": "Income Account",
		}
	)
	account.flags.ignore_permissions = True
	account.insert()
	return account.name


@frappe.whitelist()
def get_invoice_lines(sales_invoice):
	"""Pull every invoice line that still has room left to credit."""
	frappe.has_permission("Sales Invoice", "read", doc=sales_invoice, throw=True)

	rows = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": sales_invoice, "docstatus": 1},
		fields=["name as sales_invoice_item", "item_code", "qty", "rate"],
		order_by="idx",
	)

	pending = []
	for row in rows:
		already = flt(
			frappe.db.sql(
				"""
				select sum(ccni.amount)
				from `tabCustomer Credit Note Item` ccni
				inner join `tabCustomer Credit Note` ccn on ccn.name = ccni.parent
				where ccni.sales_invoice_item = %(si_item)s and ccn.docstatus = 1
				""",
				{"si_item": row.sales_invoice_item},
			)[0][0]
		)
		max_creditable = max(flt(row.qty) * flt(row.rate) - already, 0)
		if max_creditable <= 0:
			continue
		row["max_creditable"] = max_creditable
		row["amount"] = max_creditable
		pending.append(row)
	return pending
