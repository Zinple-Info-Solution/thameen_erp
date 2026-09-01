import frappe
from frappe import _
from frappe.model.document import Document
from frappe.model.mapper import get_mapped_doc
from frappe.utils import flt, now_datetime


class CustomerRequirement(Document):
	def validate(self):
		self.validate_stock_items()
		self.calculate_totals()
		self.run_credit_check()
		self.stamp_approvals()

	def validate_stock_items(self):
		"""Only stock items can be trucked.

		The form filters the picker, but a requirement can also arrive from an
		import or the API, and a non-stock item has no Bin — every availability
		check downstream would read zero and quietly mislead.
		"""
		codes = [row.item_code for row in self.items if row.item_code]
		if not codes:
			return

		non_stock = frappe.get_all(
			"Item",
			filters={"name": ("in", codes), "is_stock_item": 0},
			pluck="name",
		)
		if non_stock:
			frappe.throw(
				_("{0} do not maintain stock and cannot be delivered by truck.").format(
					", ".join(frappe.bold(code) for code in non_stock)
				),
				title=_("Non-Stock Item"),
			)

	def calculate_totals(self):
		self.total_qty = sum(flt(row.qty) for row in self.items)
		for row in self.items:
			row.amount = flt(row.qty) * flt(row.rate)
		self.estimated_value = sum(flt(row.amount) for row in self.items)

	def run_credit_check(self):
		"""Surface the customer's exposure so Finance approves with real numbers."""
		if not self.customer:
			return

		self.credit_limit = flt(get_credit_limit(self.customer, self.company))
		self.outstanding_amount = flt(get_outstanding(self.customer, self.company))
		self.available_credit = self.credit_limit - self.outstanding_amount

		if not self.credit_limit:
			# No limit configured means no automated block.
			self.credit_check_passed = 1
			return

		self.credit_check_passed = (
			1 if (self.outstanding_amount + flt(self.estimated_value)) <= self.credit_limit else 0
		)

	def stamp_approvals(self):
		if self.workflow_state == "Pending Finance Approval" and not self.sales_approved_by:
			self.sales_approved_by = frappe.session.user
			self.sales_approved_on = now_datetime()
		if self.workflow_state == "Approved" and not self.finance_approved_by:
			self.finance_approved_by = frappe.session.user
			self.finance_approved_on = now_datetime()
		if self.workflow_state == "Rejected" and not self.rejection_reason:
			frappe.throw(_("Enter a rejection reason."))

	def on_submit(self):
		if not self.status or self.status == "Draft":
			self.db_set("status", "Pending Sales Approval")

	def on_cancel(self):
		self.db_set("status", "Cancelled")


def get_credit_limit(customer, company):
	limit = frappe.db.get_value(
		"Customer Credit Limit", {"parent": customer, "company": company}, "credit_limit"
	)
	if limit:
		return limit
	group = frappe.db.get_value("Customer", customer, "customer_group")
	if group:
		return frappe.db.get_value(
			"Customer Credit Limit", {"parent": group, "company": company}, "credit_limit"
		)
	return 0


def get_outstanding(customer, company):
	return (
		frappe.db.get_value(
			"GL Entry",
			{
				"party_type": "Customer",
				"party": customer,
				"company": company,
				"is_cancelled": 0,
			},
			"sum(debit) - sum(credit)",
		)
		or 0
	)


@frappe.whitelist()
def make_sales_order(source_name, target_doc=None):
	def set_missing_values(source, target):
		target.custom_customer_requirement = source.name
		target.custom_delivery_location = source.delivery_location
		target.delivery_date = source.requested_delivery_date
		if source.project:
			target.project = source.project
		if source.payment_terms_template:
			target.payment_terms_template = source.payment_terms_template
		target.run_method("set_missing_values")
		target.run_method("calculate_taxes_and_totals")

	def update_item(source_row, target_row, source_parent):
		target_row.qty = source_row.qty
		target_row.rate = source_row.rate
		target_row.uom = source_row.uom
		target_row.delivery_date = source_row.required_by or source_parent.requested_delivery_date
		# Per-row site drives trip planning: one trip per delivery location.
		target_row.custom_delivery_location = (
			source_row.get("delivery_location") or source_parent.delivery_location
		)

	doc = get_mapped_doc(
		"Customer Requirement",
		source_name,
		{
			"Customer Requirement": {
				"doctype": "Sales Order",
				"field_map": {"transaction_date": "transaction_date"},
				"validation": {"docstatus": ["=", 1], "status": ["=", "Approved"]},
			},
			"Customer Requirement Item": {
				"doctype": "Sales Order Item",
				"field_map": {"item_code": "item_code", "description": "description"},
				"postprocess": update_item,
			},
		},
		target_doc,
		set_missing_values,
	)
	return doc


@frappe.whitelist()
def mark_ordered(requirement, sales_order):
	frappe.db.set_value(
		"Customer Requirement", requirement,
		{"sales_order": sales_order, "status": "Ordered"},
	)
