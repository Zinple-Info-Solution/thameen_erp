"""Sales Order -> Delivery Trip planning.

One Sales Order can carry several items bound for different sites, so trips are
planned **per delivery location**, not per order and not per item:

    SO-0001
      ├─ OPC 43   200 bags  →  Site A ─┐
      ├─ OPC 53   120 bags  →  Site A ─┴─ Trip 1 (2 rows)
      └─ White    80 bags   →  Site B ──  Trip 2 (1 row)

Each trip is created in **Draft** with no vehicle, because choosing the truck is
a dispatcher decision. The dispatcher assigns vehicle + driver and submits.

Qty already sitting on an open trip is never planned twice: pending qty is
`ordered - delivered - already on open trips`, so re-running the planner after a
partial dispatch only picks up what is genuinely left.
"""

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, get_datetime, get_link_to_form, now_datetime

from thameen_erp.overrides.delivery_trip import _planned_qty_on_other_trips

NO_LOCATION = "__default__"


# ---------------------------------------------------------------------------
# Document events
# ---------------------------------------------------------------------------


def validate(doc, method=None):
	"""Fall the header delivery location down to any row that has none."""
	if not doc.get("custom_delivery_location"):
		return
	for row in doc.items:
		if not row.get("custom_delivery_location"):
			row.custom_delivery_location = doc.custom_delivery_location


def on_submit(doc, method=None):
	"""Plan the trips automatically once the order is approved."""
	if not frappe.db.get_single_value("Thameen Fleet Settings", "auto_create_delivery_trip"):
		return

	if not is_fully_approved(doc):
		return

	# A planning failure must never roll back a valid submission, and must never
	# leave half a plan behind either — hence the savepoint.
	frappe.db.savepoint("thameen_auto_trip_plan")
	try:
		trips = create_trips_from_sales_order(doc.name, ignore_permissions=True)
	except Exception:
		frappe.db.rollback(save_point="thameen_auto_trip_plan")
		frappe.log_error(frappe.get_traceback(), "Thameen ERP: auto trip creation")
		frappe.msgprint(
			_("Sales Order submitted, but Delivery Trips could not be planned automatically. "
			  "Use Create > Delivery Trips once the issue is resolved."),
			indicator="orange",
			title=_("Trip Planning Failed"),
		)
		return

	if trips:
		links = ", ".join(get_link_to_form("Delivery Trip", name) for name in trips)
		frappe.msgprint(
			_("Delivery Trip(s) {0} created in draft. Assign a vehicle and driver, then submit.").format(links),
			indicator="green",
			title=_("Trips Planned"),
		)


def is_fully_approved(so) -> bool:
	"""Submitted, and — when it came from a Customer Requirement — approved there."""
	if so.docstatus != 1:
		return False

	requirement = so.get("custom_customer_requirement")
	if not requirement:
		return True

	status = frappe.db.get_value("Customer Requirement", requirement, "status")
	return status in ("Approved", "Ordered")


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def get_pending_items(sales_order):
	"""Rows on the order that still need to be put on a trip."""
	so = frappe.get_doc("Sales Order", sales_order)

	details = [row.name for row in so.items]
	planned = _planned_qty_on_other_trips(details)

	pending = []
	for row in so.items:
		balance = flt(row.qty) - flt(row.delivered_qty) - flt(planned.get(row.name))
		if balance <= 0.001:
			continue

		pending.append(
			frappe._dict(
				{
					"sales_order": so.name,
					"so_detail": row.name,
					"item_code": row.item_code,
					"item_name": row.item_name,
					"qty": balance,
					"uom": row.uom,
					"conversion_factor": flt(row.conversion_factor) or 1,
					"stock_uom": row.stock_uom,
					"rate": flt(row.rate),
					"amount": balance * flt(row.rate),
					"source_warehouse": row.warehouse,
					"delivery_location": (
						row.get("custom_delivery_location")
						or so.get("custom_delivery_location")
						or ""
					),
				}
			)
		)

	return so, pending


def group_by_location(pending):
	"""One group per delivery location, preserving the order of first appearance."""
	groups = {}
	for row in pending:
		key = (row.delivery_location or "").strip() or NO_LOCATION
		groups.setdefault(key, []).append(row)
	return groups


@frappe.whitelist()
def preview_trip_plan(sales_order):
	"""What the planner would create — used by the Sales Order dialog."""
	so, pending = get_pending_items(sales_order)
	groups = group_by_location(pending)

	return [
		{
			"delivery_location": None if location == NO_LOCATION else location,
			"total_qty": sum(flt(row.qty) for row in rows),
			"items": [
				{"item_code": row.item_code, "qty": flt(row.qty), "uom": row.uom} for row in rows
			],
		}
		for location, rows in groups.items()
	]


@frappe.whitelist()
def get_trip_rows(sales_order, delivery_location=None):
	"""Pending rows for one location — used by 'Get Items' on the Delivery Trip form."""
	_so, pending = get_pending_items(sales_order)

	if delivery_location:
		wanted = delivery_location.strip().lower()
		pending = [row for row in pending if (row.delivery_location or "").strip().lower() == wanted]

	return pending


@frappe.whitelist()
def make_delivery_trips(sales_order):
	"""Button target on the Sales Order form."""
	frappe.has_permission("Delivery Trip", "create", throw=True)

	trips = create_trips_from_sales_order(sales_order)
	if not trips:
		frappe.throw(
			_("Nothing left to plan on {0} — every line is either delivered or already on a trip.").format(
				sales_order
			)
		)
	return trips


def create_trips_from_sales_order(sales_order, vehicle=None, ignore_permissions=False):
	"""Create one draft Delivery Trip per delivery location. Returns trip names."""
	so, pending = get_pending_items(sales_order)
	if not pending:
		return []

	groups = group_by_location(pending)
	departure = _default_departure(so)
	created = []

	for location, rows in groups.items():
		trip = frappe.new_doc("Delivery Trip")
		trip.company = so.company
		trip.departure_time = departure
		trip.custom_sales_order = so.name
		trip.custom_trip_type = "Company Vehicle"
		trip.custom_delivery_location = None if location == NO_LOCATION else location

		warehouses = {row.source_warehouse for row in rows if row.source_warehouse}
		if len(warehouses) == 1:
			trip.custom_loading_warehouse = warehouses.pop()

		if vehicle:
			trip.vehicle = vehicle
			driver = frappe.db.get_value("Vehicle", vehicle, "custom_assigned_driver")
			if driver:
				trip.driver = driver

		for row in rows:
			trip.append(
				"custom_trip_items",
				{
					"sales_order": row.sales_order,
					"so_detail": row.so_detail,
					"item_code": row.item_code,
					"item_name": row.item_name,
					"qty": flt(row.qty),
					"uom": row.uom,
					"conversion_factor": flt(row.conversion_factor) or 1,
					"stock_uom": row.stock_uom,
					"rate": flt(row.rate),
					"amount": flt(row.amount),
					"source_warehouse": row.source_warehouse,
					"delivery_location": None if location == NO_LOCATION else location,
				},
			)

		if ignore_permissions:
			trip.flags.ignore_permissions = True

		trip.insert()
		created.append(trip.name)

	return created


def _default_departure(so):
	"""Delivery date at 09:00, or now if that is already in the past."""
	candidate = so.get("delivery_date")
	if candidate:
		candidate = add_to_date(get_datetime(candidate), hours=9)
		if get_datetime(candidate) > now_datetime():
			return candidate
	return now_datetime()
