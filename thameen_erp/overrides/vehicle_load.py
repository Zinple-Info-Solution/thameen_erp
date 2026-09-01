"""How much room is left on a truck, and what to do when there is not enough.

A truck has a rated Capacity, which never changes, and an Available Qty, which
does. Available Qty is what is left after the loads already committed to that
truck on other open trips:

    TRUCK-01   capacity      300
               committed     180   (TRIP-0005, still In Transit)
               available     120

Planning 500 onto that truck is not one overloaded trip. It is 120 now and 380
across further trips. The Delivery Trip form checks this the moment a vehicle is
chosen and offers to do the split, rather than letting the dispatcher discover
the problem at the weighbridge.

Two numbers, two checks
    Available Qty  moves as trips come and go — the everyday check.
    Capacity       is the ceiling — used when the truck is empty, and as the
                   size of every follow-on trip the split creates.

What counts as committed
    Submitted trips at Scheduled, Loading or In Transit. Draft trips do not
    hold space: a dispatcher builds several drafts while planning and only
    commits by submitting. Once a trip reaches Delivered the goods are off the
    truck, so the space comes back.

Available Qty is stored on the Vehicle so it can be seen in list views and
reports, but it is always recomputed from the trips rather than incremented, so
a missed hook can never leave it permanently wrong.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, get_link_to_form, getdate

from thameen_erp.overrides.trip_split import (
	QTY_TOLERANCE,
	describe_loads,
	split_by_capacity,
	stock_qty,
)

# Submitted trips in these states are holding space on the truck.
COMMITTED_STATES = ("Scheduled", "Loading", "In Transit")


# ---------------------------------------------------------------------------
# Reading the load
# ---------------------------------------------------------------------------


def get_committed_qty(vehicle, exclude_trip=None, on_date=None):
	"""Stock-UOM qty promised to this truck.

	Capacity is a per-JOURNEY limit, not a pool spread over the calendar. A
	20-tonne truck doing 10 on Monday and 10 on Friday is not carrying 20 at
	once, and refusing the Friday trip because Monday exists is wrong.

	Pass `on_date` to count only the trips that share that departure date —
	those are the ones that genuinely compete for the same space. Trips with no
	departure date set are always counted, since they could be any day.

	Leave `on_date` out for the total across every open trip.
	"""
	if not vehicle:
		return 0.0

	conditions = ["t.vehicle = %(vehicle)s", "t.docstatus = 1", "t.status in %(states)s"]
	values = {"vehicle": vehicle, "states": COMMITTED_STATES}

	if exclude_trip:
		conditions.append("t.name != %(exclude)s")
		values["exclude"] = exclude_trip

	if on_date:
		conditions.append("(t.departure_time is null or date(t.departure_time) = %(on_date)s)")
		values["on_date"] = getdate(on_date)

	result = frappe.db.sql(
		f"""
		select sum(ifnull(i.qty, 0) * ifnull(nullif(i.conversion_factor, 0), 1))
		from `tabDelivery Trip Item` i
		inner join `tabDelivery Trip` t on t.name = i.parent
		where {" and ".join(conditions)}
		""",
		values,
	)

	return flt(result[0][0]) if result and result[0] else 0.0


def get_peak_committed_qty(vehicle, exclude_trip=None):
	"""The busiest single day's promise on this truck.

	The right headline number for "how loaded is this vehicle": summing every
	open trip would report a truck booked for ten separate days as ten times
	overloaded.
	"""
	if not vehicle:
		return 0.0

	conditions = ["t.vehicle = %(vehicle)s", "t.docstatus = 1", "t.status in %(states)s"]
	values = {"vehicle": vehicle, "states": COMMITTED_STATES}

	if exclude_trip:
		conditions.append("t.name != %(exclude)s")
		values["exclude"] = exclude_trip

	rows = frappe.db.sql(
		f"""
		select date(t.departure_time) as day,
		       sum(ifnull(i.qty, 0) * ifnull(nullif(i.conversion_factor, 0), 1)) as qty
		from `tabDelivery Trip Item` i
		inner join `tabDelivery Trip` t on t.name = i.parent
		where {" and ".join(conditions)}
		group by date(t.departure_time)
		""",
		values,
		as_dict=True,
	)

	return max((flt(r.qty) for r in rows), default=0.0)


def refresh_vehicle_load(vehicle):
	"""Recompute and store Committed / Available on the Vehicle.

	Always a full recount, never an increment — a hook that fails to fire can
	then only make the figure stale for one save, not wrong forever.
	"""
	if not vehicle or not frappe.db.exists("Vehicle", vehicle):
		return

	capacity = flt(frappe.db.get_value("Vehicle", vehicle, "custom_capacity"))
	# Peak day, not the sum of every open trip — see get_peak_committed_qty.
	committed = get_peak_committed_qty(vehicle)

	# Physical stock in the vehicle warehouse, read from Bin right now.
	from thameen_erp.overrides.vehicle_stock import get_truck_stock

	on_truck = sum(get_truck_stock(vehicle).values())

	# Room left is capacity less whichever is larger, promised or physically
	# present. `max`, not the sum: a loaded trip appears in both.
	available = max(capacity - max(committed, on_truck), 0.0) if capacity else 0.0

	frappe.db.set_value(
		"Vehicle",
		vehicle,
		{
			"custom_committed_qty": committed,
			"custom_on_truck_qty": on_truck,
			"custom_available_qty": available,
		},
		update_modified=False,
	)


@frappe.whitelist()
def get_vehicle_load(vehicle, trip=None):
	"""Everything the form needs to decide whether this trip fits."""
	if not vehicle:
		return {}

	capacity = flt(frappe.get_cached_value("Vehicle", vehicle, "custom_capacity"))

	# Only the trips sharing this one's departure date compete for the space.
	on_date = frappe.db.get_value("Delivery Trip", trip, "departure_time") if trip else None
	committed = get_committed_qty(vehicle, exclude_trip=trip, on_date=on_date)

	# Space is capacity less whichever is larger: promised, or physically in
	# the truck warehouse. A truck rated 20 carrying 10 has 10 free even if no
	# trip claims that 10. `max` rather than the sum, because a trip that has
	# already loaded is counted in both.
	from thameen_erp.overrides.vehicle_stock import get_truck_stock

	on_truck_now = sum(get_truck_stock(vehicle).values())
	available = max(capacity - max(committed, on_truck_now), 0.0) if capacity else 0.0

	planned = 0.0
	if trip and frappe.db.exists("Delivery Trip", trip):
		rows = frappe.get_all(
			"Delivery Trip Item",
			filters={"parent": trip, "parenttype": "Delivery Trip"},
			fields=["qty", "conversion_factor"],
		)
		planned = sum(stock_qty(row) for row in rows)

	overflow = max(planned - available, 0.0) if capacity else 0.0

	# Physical stock on the truck — read from Bin, never stored. Shown next to
	# the capacity figures so the dispatcher sees what is ON the truck as well
	# as what is PROMISED to it.
	from thameen_erp.overrides.vehicle_stock import get_truck_stock_summary

	stock = get_truck_stock_summary(vehicle)

	return {
		"vehicle": vehicle,
		"capacity": capacity,
		"committed_qty": committed,
		"available_qty": available,
		"planned_qty": planned,
		"overflow_qty": overflow if overflow > QTY_TOLERANCE else 0.0,
		"fits": overflow <= QTY_TOLERANCE,
		"has_capacity": bool(capacity),
		"committed_trips": _committed_trips(vehicle, exclude_trip=trip),
		"on_truck_qty": stock.get("on_truck_qty", 0.0),
		"on_truck_items": stock.get("items", []),
		"vehicle_warehouse": stock.get("warehouse"),
	}


def _committed_trips(vehicle, exclude_trip=None):
	"""The open trips that are eating the space — shown so the dispatcher can
	see WHY a truck with 300 capacity only has 120 free."""
	filters = {"vehicle": vehicle, "docstatus": 1, "status": ("in", COMMITTED_STATES)}
	if exclude_trip:
		filters["name"] = ("!=", exclude_trip)

	trips = frappe.get_all(
		"Delivery Trip", filters=filters, fields=["name", "status"], limit=20
	)

	for row in trips:
		rows = frappe.get_all(
			"Delivery Trip Item",
			filters={"parent": row.name, "parenttype": "Delivery Trip"},
			fields=["qty", "conversion_factor"],
		)
		row["qty"] = sum(stock_qty(item) for item in rows)

	return trips


# ---------------------------------------------------------------------------
# Planning the split
# ---------------------------------------------------------------------------


@frappe.whitelist()
def preview_split(trip, vehicle=None, use_capacity=0):
	"""What splitting this trip would produce. Nothing is written.

	`use_capacity` ignores what is already committed and packs against the full
	rated capacity — the "the truck will be empty by then" case.
	"""
	doc = frappe.get_doc("Delivery Trip", trip)
	vehicle = vehicle or doc.get("vehicle")

	if not vehicle:
		frappe.throw(_("Choose a vehicle first — there is nothing to measure against."))

	load = get_vehicle_load(vehicle, trip=trip)
	if not load.get("has_capacity"):
		frappe.throw(
			_("Vehicle {0} has no Capacity set, so the load cannot be checked. "
			  "Set Capacity on the vehicle first.").format(frappe.bold(vehicle))
		)

	capacity = flt(load["capacity"])
	first = capacity if cint(use_capacity) else flt(load["available_qty"])

	loads = split_by_capacity(
		doc.get("custom_trip_items") or [], capacity, first_capacity=first, one_item_per_load=_one_item()
	)

	return {
		**load,
		"first_capacity": first,
		"loads": describe_loads(loads),
		"trip_count": len(loads),
		"departure_time": doc.get("departure_time"),
		"vehicles": list_vehicles_for_planning(
			exclude_trip=trip, on_date=doc.get("departure_time")
		),
	}


@frappe.whitelist()
def list_vehicles_for_planning(exclude_trip=None, on_date=None):
	"""Plates with capacity / free space / on-truck, for the planning dialogs.

	Every figure is computed here rather than read from the stored fields on
	Vehicle. The stored ones are a cache kept warm by hooks; a hook that did
	not fire leaves them stale, and a planning dialog that offers a truck
	which is actually full is worse than a slow one.

	`free` is capacity less whichever is larger: what open trips have PROMISED
	the truck, or what is PHYSICALLY in its warehouse. `max`, not the sum,
	because those two are usually the same cement counted twice — a trip that
	has loaded shows up in both.

	A truck rated 20 with 10 already on it has 10 free, whether or not a trip
	claims that 10. Pass `exclude_trip` so the trip being planned does not
	count against itself.
	"""
	from thameen_erp.overrides.vehicle_stock import get_vehicle_warehouse

	vehicles = frappe.get_all(
		"Vehicle",
		filters={"custom_status": ("in", ["Available", "Assigned"])},
		fields=[
			"name",
			"custom_capacity as capacity",
			"custom_vehicle_warehouse as warehouse",
			"custom_assigned_driver as driver",
			"custom_status as status",
		],
		limit_page_length=200,
	)
	if not vehicles:
		return []

	committed = _committed_by_vehicle(
		[v.name for v in vehicles], exclude_trip=exclude_trip, on_date=on_date
	)
	on_truck = _stock_by_warehouse([v.warehouse for v in vehicles if v.warehouse])

	for v in vehicles:
		v.committed = flt(committed.get(v.name))
		v.on_truck = flt(on_truck.get(v.warehouse))
		v.free = max(flt(v.capacity) - max(v.committed, v.on_truck), 0.0) if v.capacity else 0.0
		# Kept for callers written against the old key name.
		v.available = v.free

	vehicles.sort(key=lambda v: (-flt(v.free), v.name))
	return vehicles


def _committed_by_vehicle(vehicles, exclude_trip=None, on_date=None):
	"""{vehicle: promised stock-UOM qty} in one query instead of N.

	Without `on_date` this is the busiest single day per truck, which is the
	honest headline: a truck booked for ten separate days is not ten times
	overloaded. With `on_date`, only that day's trips count.
	"""
	if not vehicles:
		return {}

	conditions = ["t.vehicle in %(vehicles)s", "t.docstatus = 1", "t.status in %(states)s"]
	values = {"vehicles": tuple(vehicles), "states": COMMITTED_STATES}

	if exclude_trip:
		conditions.append("t.name != %(exclude)s")
		values["exclude"] = exclude_trip

	if on_date:
		conditions.append("(t.departure_time is null or date(t.departure_time) = %(on_date)s)")
		values["on_date"] = getdate(on_date)

	rows = frappe.db.sql(
		f"""
		select t.vehicle,
		       date(t.departure_time) as day,
		       sum(ifnull(i.qty, 0) * ifnull(nullif(i.conversion_factor, 0), 1)) as qty
		from `tabDelivery Trip Item` i
		inner join `tabDelivery Trip` t on t.name = i.parent
		where {" and ".join(conditions)}
		group by t.vehicle, date(t.departure_time)
		""",
		values,
		as_dict=True,
	)

	peak = {}
	for row in rows:
		peak[row.vehicle] = max(flt(peak.get(row.vehicle)), flt(row.qty))
	return peak


def _stock_by_warehouse(warehouses):
	"""{warehouse: total actual qty} straight from Bin."""
	if not warehouses:
		return {}

	rows = frappe.get_all(
		"Bin",
		filters={"warehouse": ("in", list(set(warehouses))), "actual_qty": (">", 0)},
		fields=["warehouse", "sum(actual_qty) as qty"],
		group_by="warehouse",
	)
	return {row.warehouse: flt(row.qty) for row in rows}


@frappe.whitelist()
def split_trip(trip, vehicle=None, use_capacity=0, assignments=None, plan=None):
	"""Trim this trip to what fits and put the rest on fresh draft trips.

	`assignments` is an optional JSON list of vehicle names for the follow-on
	trips, in order. Anything not named is left without a vehicle for the
	dispatcher to fill in.

	`plan` (preferred) is the edited plan from the dialog:
	    [{"items": [{"item_code", "qty"}], "vehicle", "departure_time"}, ...]
	qty in stock UOM; the first entry is what stays on THIS trip. When given,
	assignments and use_capacity are ignored — the plan is the truth.

	Order matters: the current trip is trimmed and saved BEFORE the siblings are
	created. The other way round, the new trips would fail their own pending-qty
	validation, because this trip would still be holding the full quantity.
	"""
	frappe.has_permission("Delivery Trip", "write", doc=trip, throw=True)

	doc = frappe.get_doc("Delivery Trip", trip)

	if doc.docstatus != 0:
		frappe.throw(
			_("Only a draft trip can be split. Cancel and amend {0}, or plan the "
			  "remainder as a new trip from the Sales Order.").format(trip)
		)

	vehicle = vehicle or doc.get("vehicle")
	if not vehicle:
		frappe.throw(_("Choose a vehicle first."))

	if isinstance(assignments, str):
		assignments = json.loads(assignments or "[]")
	assignments = assignments or []

	load = get_vehicle_load(vehicle, trip=trip)
	if not load.get("has_capacity"):
		frappe.throw(_("Vehicle {0} has no Capacity set.").format(frappe.bold(vehicle)))

	capacity = flt(load["capacity"])
	first = capacity if cint(use_capacity) else flt(load["available_qty"])

	if first <= QTY_TOLERANCE and not plan:
		frappe.throw(
			_("{0} has no free space at all — {1} of its {2} capacity is already committed. "
			  "Choose a different vehicle.").format(
				frappe.bold(vehicle), flt(load["committed_qty"], 2), flt(capacity, 2)
			)
		)

	if isinstance(plan, str):
		plan = json.loads(plan or "[]")

	if plan:
		# The dispatcher edited the plan: honour their quantities exactly.
		from thameen_erp.overrides.trip_split import allocate_to_plan

		loads = allocate_to_plan(doc.get("custom_trip_items") or [], plan)
		assignments = [p.get("vehicle") for p in plan[1:]]
		departures = [p.get("departure_time") for p in plan]
		first_vehicle = plan[0].get("vehicle") or vehicle
	else:
		loads = split_by_capacity(
			doc.get("custom_trip_items") or [], capacity, first_capacity=first, one_item_per_load=_one_item()
		)
		departures = []
		first_vehicle = vehicle

	if len(loads) <= 1:
		frappe.throw(_("This trip already fits on {0} — nothing to split.").format(vehicle))

	template = _trip_header(doc)

	# 1. Trim the current trip down to the first load.
	doc.vehicle = first_vehicle
	if departures and departures[0]:
		doc.departure_time = departures[0]
	doc.set("custom_trip_items", [])
	for row in loads[0]:
		doc.append("custom_trip_items", _row_values(row))
	doc.flags.thameen_splitting = True
	doc.save()

	# 2. Everything else becomes a fresh draft.
	created = []
	warnings = []
	for index, chunk in enumerate(loads[1:]):
		new_trip = frappe.new_doc("Delivery Trip")
		new_trip.update(template)

		assigned = assignments[index] if index < len(assignments) else None
		if assigned:
			new_trip.vehicle = assigned
			driver = frappe.db.get_value("Vehicle", assigned, "custom_assigned_driver")
			if driver:
				new_trip.driver = driver
			cap = flt(frappe.db.get_value("Vehicle", assigned, "custom_capacity"))
			chunk_qty = sum(stock_qty(row) for row in chunk)
			if cap and chunk_qty > cap + QTY_TOLERANCE:
				warnings.append(_("trip {0}: {1} on {2} (capacity {3})").format(index + 2, flt(chunk_qty, 2), assigned, flt(cap, 2)))

		if index + 1 < len(departures) and departures[index + 1]:
			new_trip.departure_time = departures[index + 1]

		for row in chunk:
			new_trip.append("custom_trip_items", _row_values(row))

		new_trip.flags.thameen_splitting = True
		new_trip.insert()
		created.append(new_trip.name)

	if warnings:
		frappe.msgprint(
			_("Some trips are above their truck's rated capacity — {0}. They will still submit; the overload is reported, not refused.").format(
				"; ".join(warnings)
			),
			indicator="orange",
		)

	frappe.msgprint(
		_("{0} kept {1}. {2} further trip(s) created: {3}").format(
			get_link_to_form("Delivery Trip", doc.name),
			flt(sum(stock_qty(row) for row in loads[0]), 2),
			len(created),
			", ".join(get_link_to_form("Delivery Trip", name) for name in created),
		),
		indicator="green",
		title=_("Trip Split"),
	)

	return created


def _one_item():
	return bool(frappe.db.get_single_value("Thameen Fleet Settings", "one_item_per_trip"))


def _trip_header(doc):
	"""Header values a follow-on trip inherits. Vehicle and driver deliberately
	excluded — each new trip needs its own truck."""
	fields = (
		"company",
		"departure_time",
		"custom_sales_order",
		"custom_delivery_location",
		"custom_loading_warehouse",
		"custom_trip_type",
		"custom_external_transporter",
		"custom_transportation_item",
		"custom_supply_source",
		"custom_supplier",
		"custom_purchase_order",
		"custom_destination_type",
		"custom_target_warehouse",
		# Site-added fields seen in the wild — copied when present.
		"sales_order",
		"customer",
	)
	meta = doc.meta
	return {
		field: doc.get(field)
		for field in fields
		if meta.has_field(field) and doc.get(field) is not None
	}


def _row_values(row):
	"""Copy a planned row, dropping anything that belongs to the old parent or
	to a delivery that has not happened yet."""
	drop = {
		"name", "parent", "parentfield", "parenttype", "idx", "owner",
		"creation", "modified", "modified_by", "docstatus",
		"delivered_qty", "delivery_note", "delivery_note_item",
		"purchase_receipt", "purchase_receipt_item",
	}
	as_dict_method = getattr(row, "as_dict", None)
	source = as_dict_method() if callable(as_dict_method) else dict(row)
	return {key: value for key, value in source.items() if key not in drop}


# ---------------------------------------------------------------------------
# Document events
# ---------------------------------------------------------------------------


def trip_on_change(doc, method=None):
	"""Keep the truck's Available Qty in step after any trip movement."""
	for vehicle in {doc.get("vehicle"), (doc.get_doc_before_save() or {}).get("vehicle")}:
		if vehicle:
			refresh_vehicle_load(vehicle)


def trip_after_submit_or_cancel(doc, method=None):
	if doc.get("vehicle"):
		refresh_vehicle_load(doc.vehicle)


def vehicle_on_update(doc, method=None):
	"""Capacity changed, or the truck was just created — recount."""
	refresh_vehicle_load(doc.name)


@frappe.whitelist()
def recalculate_vehicle_load(vehicle):
	"""Recount one truck on demand, from the Vehicle form.

	The stored Committed / On Truck / Available figures are a cache. This is
	the button that repairs them when a hook did not fire.
	"""
	frappe.has_permission("Vehicle", "write", doc=vehicle, throw=True)
	refresh_vehicle_load(vehicle)
	return frappe.db.get_value(
		"Vehicle",
		vehicle,
		["custom_capacity", "custom_committed_qty", "custom_on_truck_qty", "custom_available_qty"],
		as_dict=True,
	)


@frappe.whitelist()
def rebuild_all_vehicle_loads():
	"""One-off repair, and the after_migrate entry point."""
	names = frappe.get_all("Vehicle", pluck="name")
	for name in names:
		refresh_vehicle_load(name)
	frappe.db.commit()
	return len(names)


def on_stock_ledger_entry(doc, method=None):
	"""Every stock movement in a vehicle warehouse re-reads On Truck Qty, so
	the Vehicle always shows exactly what the warehouse holds."""
	if not doc.get("warehouse"):
		return
	vehicle = frappe.db.get_value(
		"Warehouse", doc.warehouse, "custom_linked_vehicle"
	) if frappe.get_meta("Warehouse").has_field("custom_linked_vehicle") else None
	if not vehicle:
		vehicle = frappe.db.get_value("Vehicle", {"custom_vehicle_warehouse": doc.warehouse}, "name")
	if vehicle:
		refresh_vehicle_load(vehicle)
