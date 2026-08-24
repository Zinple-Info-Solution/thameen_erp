import frappe
from frappe import _
from frappe.model.document import Document


class ThameenFleetSettings(Document):
	def validate(self):
		if self.transportation_item:
			is_stock = frappe.db.get_value("Item", self.transportation_item, "is_stock_item")
			if is_stock:
				frappe.throw(
					_("The Transportation Charge Item must be a non-stock (service) item.")
				)
		if self.transportation_income_account:
			root = frappe.db.get_value("Account", self.transportation_income_account, "root_type")
			if root != "Income":
				frappe.throw(_("Transportation Revenue Account must be an Income account."))
