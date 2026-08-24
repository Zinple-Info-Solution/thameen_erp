"""Scheduled jobs: expiry alerts, service reminders, credit-note ageing.

Notification model: one digest per job per user, listing every due row,
instead of one Notification Log per document.
"""

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_link_to_form, getdate, nowdate


# ---------------------------------------------------------------------------
# Notification plumbing
# ---------------------------------------------------------------------------


def _get_notify_users():
	"""Enabled users holding any role listed in Thameen Fleet Settings."""
	settings = frappe.get_single("Thameen Fleet Settings")
	roles = [r.strip() for r in (settings.notify_roles or "").splitlines() if r.strip()]
	if not roles:
		roles = ["Fleet Manager"]

	users = set(
		frappe.get_all(
			"Has Role",
			filters={"role": ("in", roles), "parenttype": "User"},
			pluck="parent",
		)
	)
	users.discard("Administrator")
	if not users:
		return []

	return frappe.get_all(
		"User",
		filters={"name": ("in", list(users)), "enabled": 1},
		pluck="name",
	)


def _send_digest(subject, headers, rows, doctype=None, name=None):
	"""One Notification Log per user containing an HTML table of every row.

	rows: list of lists, already formatted for display. The first cell of each
	row may be a doctype link produced by get_link_to_form.
	"""
	if not rows:
		return

	users = _get_notify_users()
	if not users:
		return

	head = "".join(f"<th style='text-align:left;padding:4px 8px'>{h}</th>" for h in headers)
	body = "".join(
		"<tr>" + "".join(f"<td style='padding:4px 8px'>{c}</td>" for c in row) + "</tr>"
		for row in rows
	)
	html = (
		f"<p>{_('{0} item(s) need attention.').format(len(rows))}</p>"
		f"<table style='border-collapse:collapse;width:100%'>"
		f"<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
	)

	for user in users:
		frappe.get_doc(
			{
				"doctype": "Notification Log",
				"subject": subject,
				"email_content": html,
				"for_user": user,
				"type": "Alert",
				"document_type": doctype,
				"document_name": name,
			}
		).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Vehicle document expiry
# ---------------------------------------------------------------------------


def notify_expiring_vehicle_documents():
	"""Refresh statuses, then send ONE digest of everything expiring or expired."""
	today = getdate(nowdate())
	default_window = (
		frappe.db.get_single_value("Thameen Fleet Settings", "document_expiry_reminder_days") or 30
	)

	docs = frappe.get_all(
		"Vehicle Document",
		filters={
			"status": ("not in", ["Renewed", "Cancelled"]),
			"expiry_date": ("is", "set"),
		},
		fields=["name", "vehicle", "document_type", "expiry_date", "reminder_days", "status"],
		order_by="expiry_date asc",
	)

	rows = []
	for doc in docs:
		expiry = getdate(doc.expiry_date)
		window = doc.reminder_days or default_window
		threshold = getdate(add_days(today, window))

		if expiry < today:
			new_status = "Expired"
		elif expiry <= threshold:
			new_status = "Expiring Soon"
		else:
			new_status = "Active"

		if new_status != doc.status:
			frappe.db.set_value(
				"Vehicle Document", doc.name, "status", new_status, update_modified=False
			)

		if new_status not in ("Expiring Soon", "Expired"):
			continue

		days = (expiry - today).days
		rows.append(
			[
				get_link_to_form("Vehicle Document", doc.name),
				doc.vehicle or "",
				doc.document_type or "",
				frappe.format_value(doc.expiry_date, {"fieldtype": "Date"}),
				_("expired {0}d ago").format(abs(days)) if days < 0 else _("in {0}d").format(days),
				new_status,
			]
		)

	_send_digest(
		_("Vehicle documents: {0} expiring or expired").format(len(rows)),
		[_("Document"), _("Vehicle"), _("Type"), _("Expiry"), _("Due"), _("Status")],
		rows,
	)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Service due
# ---------------------------------------------------------------------------


def notify_service_due():
	"""One digest of every vehicle at or past its next service date/odometer."""
	today = getdate(nowdate())
	logs = frappe.db.sql(
		"""
		select vl.license_plate,
		       max(vl.custom_next_service_due) as due,
		       max(vl.custom_next_service_odometer) as due_odo
		from `tabVehicle Log` vl
		where vl.docstatus = 1 and vl.custom_next_service_due is not null
		group by vl.license_plate
		order by due asc
		""",
		as_dict=True,
	)

	rows = []
	for row in logs:
		vehicle = frappe.db.get_value(
			"Vehicle", row.license_plate, ["last_odometer", "custom_status"], as_dict=True
		)
		if not vehicle or vehicle.custom_status == "Under Maintenance":
			continue

		due_by_date = row.due and getdate(row.due) <= add_days(today, 7)
		due_by_odo = row.due_odo and flt(vehicle.last_odometer) >= flt(row.due_odo)
		if not (due_by_date or due_by_odo):
			continue

		reasons = []
		if due_by_date:
			reasons.append(_("date"))
		if due_by_odo:
			reasons.append(_("odometer"))

		rows.append(
			[
				get_link_to_form("Vehicle", row.license_plate),
				frappe.format_value(row.due, {"fieldtype": "Date"}) if row.due else "",
				flt(row.due_odo),
				flt(vehicle.last_odometer),
				" + ".join(reasons),
			]
		)

	_send_digest(
		_("Service due: {0} vehicle(s)").format(len(rows)),
		[_("Vehicle"), _("Next Service"), _("Due Odometer"), _("Current Odometer"), _("Triggered By")],
		rows,
	)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Credit note ageing
# ---------------------------------------------------------------------------


def flag_overdue_credit_notes():
	"""One digest of every Purchase Invoice line still awaiting a credit note."""
	overdue_days = (
		frappe.db.get_single_value("Thameen Fleet Settings", "credit_note_overdue_days") or 45
	)
	cutoff = add_days(getdate(nowdate()), -overdue_days)

	lines = frappe.db.sql(
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
		order by pi.supplier, pi.posting_date
		""",
		{"cutoff": cutoff},
		as_dict=True,
	)
	if not lines:
		return

	rows = []
	for line in lines:
		pending = flt(line.expected) - flt(line.received)
		age = (getdate(nowdate()) - getdate(line.posting_date)).days
		rows.append(
			[
				line.supplier,
				get_link_to_form("Purchase Invoice", line.parent),
				line.item_code,
				frappe.format_value(line.expected, {"fieldtype": "Currency"}),
				frappe.format_value(line.received, {"fieldtype": "Currency"}),
				frappe.format_value(pending, {"fieldtype": "Currency"}),
				_("{0}d").format(age),
			]
		)

	_send_digest(
		_("Credit notes: {0} line(s) overdue past {1} days").format(len(rows), overdue_days),
		[
			_("Supplier"),
			_("Invoice"),
			_("Item"),
			_("Expected"),
			_("Received"),
			_("Pending"),
			_("Age"),
		],
		rows,
	)
	frappe.db.commit()
