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

import json

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, get_datetime, get_link_to_form, now_datetime

from thameen_erp.overrides.delivery_trip import _planned_qty_on_other_trips

NO_LOCATION = "__default__"
ITEM_SEP = " \u00a6 "


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
	"""One group per delivery location — and, when One Item per Trip is on in
	Thameen Fleet Settings, per item within that location.

	    Site A  OPC-43 200 ─ Trip 1
	    Site A  OPC-53 120 ─ Trip 2      (one item per trip ON)
	    Site B  OPC-43  80 ─ Trip 3

	Keys stay the location string so existing callers keep working; the item
	is folded in as "Site A ¦ OPC-53" and stripped again when the trip is
	created.
	"""
	per_item = bool(frappe.db.get_single_value("Thameen Fleet Settings", "one_item_per_trip"))
	groups = {}
	for row in pending:
		location = (row.delivery_location or "").strip() or NO_LOCATION
		key = f"{location}{ITEM_SEP}{row.item_code}" if per_item else location
		groups.setdefault(key, []).append(row)
	return groups


def _location_from_key(key):
	location = key.split(ITEM_SEP, 1)[0]
	return None if location == NO_LOCATION else location


@frappe.whitelist()
def preview_trip_plan(sales_order):
	"""What the planner would create — used by the Sales Order dialog.

	Returns one entry per (location, item) with the qty in STOCK UOM, the
	vehicles available for planning and the default departure, so the dialog
	can let the dispatcher edit qty / truck / date before creating anything.
	"""
	so, pending = get_pending_items(sales_order)
	groups = group_by_location(pending)

	from thameen_erp.overrides.vehicle_load import list_vehicles_for_planning

	plan = []
	for key, rows in groups.items():
		location = _location_from_key(key)
		by_item = {}
		for row in rows:
			by_item[row.item_code] = flt(by_item.get(row.item_code)) + flt(row.qty) * (flt(row.conversion_factor) or 1)
		for item_code, qty in by_item.items():
			plan.append({"delivery_location": location, "item_code": item_code, "qty": qty})

	return {
		"plan": plan,
		"groups": [
			{
				"delivery_location": _location_from_key(location),
				"total_qty": sum(flt(row.qty) for row in rows),
				"items": [{"item_code": row.item_code, "qty": flt(row.qty), "uom": row.uom} for row in rows],
			}
			for location, rows in groups.items()
		],
		"vehicles": list_vehicles_for_planning(
			items=[row["item_code"] for row in plan if row.get("item_code")]
		),
		"departure_time": str(_default_departure(so)),
	}


@frappe.whitelist()
def make_delivery_trips_from_plan(sales_order, plan):
	"""Create trips exactly as the dispatcher planned them in the dialog.

	plan = [{delivery_location, item_code, qty (stock UOM), vehicle?, departure_time?}]
	Each entry becomes one draft trip; its qty is cut out of the order's
	pending rows for that location and item, first-come-first-served, so the
	Sales Order line references are kept. Planning more than is pending is
	refused; planning less simply leaves the rest for later.
	"""
	frappe.has_permission("Delivery Trip", "create", throw=True)

	if isinstance(plan, str):
		plan = json.loads(plan or "[]")
	plan = [p for p in (plan or []) if flt(p.get("qty")) > 0]
	if not plan:
		frappe.throw(_("Nothing to create — every row is zero."))

	so, pending = get_pending_items(sales_order)
	if not pending:
		frappe.throw(_("Nothing left to plan on {0}.").format(sales_order))

	# Pool of pending rows per (location, item), in stock UOM.
	pool = {}
	for row in pending:
		key = ((row.delivery_location or "").strip().lower(), row.item_code)
		pool.setdefault(key, []).append([row, flt(row.qty) * (flt(row.conversion_factor) or 1)])

	wanted = {}
	for p in plan:
		key = ((p.get("delivery_location") or "").strip().lower(), p["item_code"])
		wanted[key] = flt(wanted.get(key)) + flt(p["qty"])
	for key, qty in wanted.items():
		have = sum(left for _, left in pool.get(key, []))
		if qty > have + 0.001:
			frappe.throw(
				_("{0} at {1}: the plan sends {2} but only {3} is still pending on the order.").format(
					key[1], key[0] or _("(order default)"), flt(qty, 2), flt(have, 2)
				),
				title=_("Plan exceeds the order"),
			)

	created = []
	for p in plan:
		key = ((p.get("delivery_location") or "").strip().lower(), p["item_code"])
		location = p.get("delivery_location") or None
		trip = frappe.new_doc("Delivery Trip")
		trip.company = so.company
		trip.departure_time = get_datetime(p.get("departure_time")) if p.get("departure_time") else _default_departure(so)
		trip.custom_sales_order = so.name
		trip.custom_trip_type = "Company Vehicle"
		trip.custom_supply_source = "Own Warehouse"
		trip.custom_delivery_location = location
		_fill_site_fields(trip, so)
		if p.get("vehicle"):
			trip.vehicle = p["vehicle"]
			driver = frappe.db.get_value("Vehicle", p["vehicle"], "custom_assigned_driver")
			if driver:
				trip.driver = driver

		need = flt(p["qty"])
		warehouses = set()
		for entry in pool.get(key, []):
			if need <= 0.001:
				break
			row, left = entry
			if left <= 0.001:
				continue
			take = min(left, need)
			factor = flt(row.conversion_factor) or 1
			trip.append(
				"custom_trip_items",
				{
					"sales_order": row.sales_order,
					"so_detail": row.so_detail,
					"item_code": row.item_code,
					"item_name": row.item_name,
					"qty": take / factor,
					"uom": row.uom,
					"conversion_factor": factor,
					"stock_uom": row.stock_uom,
					"rate": flt(row.rate),
					"amount": take / factor * flt(row.rate),
					"source_warehouse": row.source_warehouse,
					"delivery_location": location,
				},
			)
			if row.source_warehouse:
				warehouses.add(row.source_warehouse)
			entry[1] = left - take
			need -= take

		if len(warehouses) == 1:
			trip.custom_loading_warehouse = warehouses.pop()
		trip.flags.thameen_splitting = True
		trip.insert()
		created.append(trip.name)

	frappe.msgprint(
		_("{0} trip(s) created: {1}").format(
			len(created), ", ".join(get_link_to_form("Delivery Trip", n) for n in created)
		),
		indicator="green",
		title=_("Delivery Trips Created"),
	)
	return created


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

	for key, rows in groups.items():
		location = _location_from_key(key)
		trip = frappe.new_doc("Delivery Trip")
		trip.company = so.company
		trip.departure_time = departure
		trip.custom_sales_order = so.name
		trip.custom_trip_type = "Company Vehicle"
		trip.custom_supply_source = "Own Warehouse"
		trip.custom_delivery_location = location
		_fill_site_fields(trip, so)

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
					"delivery_location": location,
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


def _fill_site_fields(trip, so):
	"""Some sites add their own mandatory links to Delivery Trip (a plain
	`sales_order` or `customer` is common). Fill them when we know the answer
	so inserts do not die on a customization we cannot see."""
	if trip.meta.has_field("sales_order") and not trip.get("sales_order"):
		trip.sales_order = so.name
	if trip.meta.has_field("customer") and not trip.get("customer"):
		trip.customer = so.get("customer")
