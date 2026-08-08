"""Scheduled jobs: expiry alerts, service reminders, credit-note ageing."""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate


def _notify_roles(subject, message, reference_doctype=None, reference_name=None):
	settings = frappe.get_single("Thameen Fleet Settings")
	roles = [r.strip() for r in (settings.notify_roles or "").splitlines() if r.strip()]
	if not roles:
		roles = ["Fleet Manager"]

	users = set()
	for role in roles:
		users.update(
			frappe.get_all(
				"Has Role",
				filters={"role": role, "parenttype": "User"},
				pluck="parent",
			)
		)
	users.discard("Administrator")
	if not users:
		return

	for user in users:
		if not frappe.db.get_value("User", user, "enabled"):
			continue
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"subject": subject,
				"email_content": message,
				"for_user": user,
				"type": "Alert",
				"document_type": reference_doctype,
				"document_name": reference_name,
			}
		).insert(ignore_permissions=True)


def notify_expiring_vehicle_documents():
	"""Refresh statuses and alert on anything inside its reminder window."""
	today = getdate(nowdate())
	docs = frappe.get_all(
		"Vehicle Document",
		filters={"status": ("not in", ["Renewed", "Cancelled"])},
		fields=["name", "vehicle", "document_type", "expiry_date", "reminder_days", "status"],
	)

	for doc in docs:
		expiry = getdate(doc.expiry_date)
		threshold = getdate(add_days(today, doc.reminder_days or 30))

		new_status = "Expired" if expiry < today else ("Expiring Soon" if expiry <= threshold else "Active")
		if new_status != doc.status:
			frappe.db.set_value("Vehicle Document", doc.name, "status", new_status, update_modified=False)

		if new_status in ("Expiring Soon", "Expired"):
			days = (expiry - today).days
			_notify_roles(
				_("{0} for {1} {2}").format(
					doc.document_type, doc.vehicle,
					_("expired {0} days ago").format(abs(days)) if days < 0
					else _("expires in {0} days").format(days),
				),
				_("Vehicle Document {0} — expiry {1}").format(doc.name, doc.expiry_date),
				"Vehicle Document",
				doc.name,
			)
	frappe.db.commit()


def notify_service_due():
	"""Alert when a vehicle is at or past its next service date/odometer."""
	today = getdate(nowdate())
	logs = frappe.db.sql(
		"""
		select vl.license_plate, max(vl.custom_next_service_due) as due,
		       max(vl.custom_next_service_odometer) as due_odo
		from `tabVehicle Log` vl
		where vl.docstatus = 1 and vl.custom_next_service_due is not null
		group by vl.license_plate
		""",
		as_dict=True,
	)

	for row in logs:
		vehicle = frappe.db.get_value(
			"Vehicle", row.license_plate, ["last_odometer", "custom_status"], as_dict=True
		)
		if not vehicle or vehicle.custom_status == "Under Maintenance":
			continue

		due_by_date = row.due and getdate(row.due) <= add_days(today, 7)
		due_by_odo = row.due_odo and flt(vehicle.last_odometer) >= flt(row.due_odo)

		if due_by_date or due_by_odo:
			_notify_roles(
				_("Service due for vehicle {0}").format(row.license_plate),
				_("Next service {0} / odometer {1}. Current odometer {2}.").format(
					row.due, row.due_odo, vehicle.last_odometer
				),
				"Vehicle",
				row.license_plate,
			)
	frappe.db.commit()


def flag_overdue_credit_notes():
	"""Purchase Invoice lines still awaiting a supplier credit note."""
	settings = frappe.get_single("Thameen Fleet Settings")
	overdue_days = settings.credit_note_overdue_days or 45
	cutoff = add_days(getdate(nowdate()), -overdue_days)

	rows = frappe.db.sql(
		"""
		select pii.parent, pii.name, pii.item_code,
		       pii.custom_expected_discount_amount as expected,
		       pii.custom_credit_note_received as received,
		       pi.supplier, pi.posting_date
		from `tabPurchase Invoice Item` pii
		inner join `tabPurchase Invoice` pi on pi.name = pii.parent
		where pi.docstatus = 1
		  and pii.custom_expected_discount_amount > 0
		  and pii.custom_credit_note_status in ('Expected', 'Partially Received')
		  and pi.posting_date <= %(cutoff)s
		""",
		{"cutoff": cutoff},
		as_dict=True,
	)
	if not rows:
		return

	by_supplier = {}
	for row in rows:
		by_supplier.setdefault(row.supplier, []).append(row)

	for supplier, lines in by_supplier.items():
		pending = sum(flt(l.expected) - flt(l.received) for l in lines)
		_notify_roles(
			_("{0}: {1} credit note line(s) overdue").format(supplier, len(lines)),
			_("Total pending credit {0} across invoices older than {1} days.").format(
				frappe.format_value(pending, {"fieldtype": "Currency"}), overdue_days
			),
			"Supplier",
			supplier,
		)
	frappe.db.commit()


def sync_vehicle_status():
	"""Vehicles with no open trip and no open maintenance become Available."""
	busy = set(
		frappe.get_all(
			"Delivery Trip",
			filters={"docstatus": 1, "status": ("not in", ["Completed", "Cancelled"])},
			pluck="vehicle",
		)
	)
	busy.discard(None)

	stale = frappe.get_all(
		"Vehicle",
		filters={"custom_status": "On Trip"},
		pluck="name",
	)
	for vehicle in stale:
		if vehicle not in busy:
			frappe.db.set_value("Vehicle", vehicle, "custom_status", "Available", update_modified=False)
	frappe.db.commit()
