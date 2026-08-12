"""Row-level permissions for Delivery Trip.

Drivers may only see their own trips; everyone else follows standard role
permissions.
"""

import frappe


BYPASS_ROLES = {
	"System Manager",
	"Fleet Manager",
	"Operation Manager",
	"Sales Manager",
	"Accounts Manager",
	"Stock Manager",
}


def _driver_for_user(user):
	employee = frappe.db.get_value("Employee", {"user_id": user}, "name")
	if not employee:
		return None
	return frappe.db.get_value("Driver", {"employee": employee}, "name")


def _is_restricted(user):
	roles = set(frappe.get_roles(user))
	return not (roles & BYPASS_ROLES)


def delivery_trip_query_conditions(user=None):
	user = user or frappe.session.user
	if not _is_restricted(user):
		return ""

	driver = _driver_for_user(user)
	if not driver:
		return ""

	return f"""`tabDelivery Trip`.driver = {frappe.db.escape(driver)}"""


def delivery_trip_has_permission(doc, user=None, permission_type=None):
	user = user or frappe.session.user
	if not _is_restricted(user):
		return True

	driver = _driver_for_user(user)
	if not driver:
		return True

	return doc.driver == driver
