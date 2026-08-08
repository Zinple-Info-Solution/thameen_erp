import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, getdate, nowdate


class VehicleDocument(Document):
	def validate(self):
		self.validate_dates()
		self.set_status()

	def validate_dates(self):
		if self.issue_date and self.expiry_date and getdate(self.expiry_date) < getdate(self.issue_date):
			frappe.throw(_("Expiry Date cannot be before Issue Date."))

	def set_status(self):
		if self.status in ("Renewed", "Cancelled"):
			return
		today = getdate(nowdate())
		expiry = getdate(self.expiry_date)
		threshold = add_days(today, self.reminder_days or 30)

		if expiry < today:
			self.status = "Expired"
		elif expiry <= getdate(threshold):
			self.status = "Expiring Soon"
		else:
			self.status = "Active"

	def on_update(self):
		if self.document_type == "Registration" and self.expiry_date:
			frappe.db.set_value(
				"Vehicle", self.vehicle, "custom_registration_expiry", self.expiry_date,
				update_modified=False,
			)
		if self.document_type == "Insurance":
			frappe.db.set_value(
				"Vehicle",
				self.vehicle,
				{"policy_no": self.document_number, "end_date": self.expiry_date,
				 "start_date": self.issue_date},
				update_modified=False,
			)


@frappe.whitelist()
def renew(source_name, new_expiry_date=None):
	"""Clone an expiring document into a fresh one and mark the old as Renewed."""
	old = frappe.get_doc("Vehicle Document", source_name)
	new = frappe.copy_doc(old)
	new.issue_date = old.expiry_date
	new.expiry_date = new_expiry_date
	new.status = "Active"
	new.renewed_by_document = None
	new.document_copy = None
	new.insert(ignore_permissions=True)

	old.db_set("renewed_by_document", new.name)
	old.db_set("status", "Renewed")
	return new.name
