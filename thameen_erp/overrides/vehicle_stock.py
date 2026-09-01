"""What is physically sitting on a truck, and moving stock on and off it by hand.

Until now the only way cement got onto a truck was a Delivery Trip reaching
*Loading*. That is still the normal path, but two real situations need a
manual one:

  * A truck is loaded at the yard before the order is confirmed, so it can
    leave the moment the customer says go.
  * A truck comes back with cement still on it (short delivery, site closed)
    and the bags have to go back into the yard.

Both are ordinary Material Transfers between the loading warehouse and the
vehicle warehouse. They are tagged `custom_vehicle_load_type` = Manual Load /
Manual Unload so they can be told apart from trip loading in reports.

Three numbers, three meanings
-----------------------------
    Capacity       rated — what the truck can carry when empty
    Committed Qty  promised — planned qty on submitted open trips
    On Truck       physical — actual stock in the vehicle warehouse

On Truck is the one this module owns. It is never stored on the Vehicle; it is
always read from `tabBin`, because stock is the one thing ERPNext already keeps
perfectly in step.

How trip loading uses it
------------------------
When a trip reaches Loading it now asks "how much of this is already on the
truck and not spoken for by another loaded trip?" and only transfers the
shortfall. A truck loaded by hand in the morning is therefore not loaded a
second time in the afternoon.  See `free_truck_stock`.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, get_link_to_form

QTY_TOLERANCE = 0.001

# Trips in these states have ALREADY moved their stock onto the truck, so that
# stock is spoken for and must not be handed to a second trip.
LOADED_STATES = ("Loading", "In Transit")


# ---------------------------------------------------------------------------
# Reading stock on the truck
# ---------------------------------------------------------------------------


def get_vehicle_warehouse(vehicle):
	return frappe.get_cached_value("Vehicle", vehicle, "custom_vehicle_warehouse") if vehicle else None


def get_truck_stock(vehicle, item_codes=None):
	"""{item_code: actual_qty in stock UOM} for everything on the truck."""
	warehouse = get_vehicle_warehouse(vehicle)
	if not warehouse:
		return {}

	filters = {"warehouse": warehouse, "actual_qty": (">", 0)}
	if item_codes:
		filters["item_code"] = ("in", list(item_codes))

	return {
		row.item_code: flt(row.actual_qty)
		for row in frappe.get_all("Bin", filters=filters, fields=["item_code", "actual_qty"])
	}


def get_loaded_qty_on_other_trips(vehicle, item_codes=None, exclude_trip=None):
	"""Stock-UOM qty other trips on this truck have already loaded.

	These trips are at Loading / In Transit, so their cement is physically on
	the truck. It is not available to a new trip even though it shows in the
	vehicle warehouse.
	"""
	if not vehicle:
		return {}

	conditions = ["t.vehicle = %(vehicle)s", "t.docstatus = 1", "t.status in %(states)s"]
	values = {"vehicle": vehicle, "states": LOADED_STATES}

	if exclude_trip:
		conditions.append("t.name != %(exclude)s")
		values["exclude"] = exclude_trip
	if item_codes:
		conditions.append("i.item_code in %(items)s")
		values["items"] = tuple(item_codes)

	rows = frappe.db.sql(
		f"""
		select i.item_code,
		       sum(ifnull(i.qty, 0) * ifnull(nullif(i.conversion_factor, 0), 1)) as qty
		from `tabDelivery Trip Item` i
		inner join `tabDelivery Trip` t on t.name = i.parent
		where {" and ".join(conditions)}
		group by i.item_code
		""",
		values,
		as_dict=True,
	)
	return {row.item_code: flt(row.qty) for row in rows}


def free_truck_stock(vehicle, item_codes=None, exclude_trip=None):
	"""Stock on the truck that no loaded trip is already counting on.

	    on truck 300 OPC-43, TRIP-0007 (In Transit) planned 180 of it
	    → 120 is free for the next trip
	"""
	on_truck = get_truck_stock(vehicle, item_codes)
	spoken_for = get_loaded_qty_on_other_trips(vehicle, item_codes, exclude_trip)
	return {
		item: max(flt(qty) - flt(spoken_for.get(item)), 0.0)
		for item, qty in on_truck.items()
	}


@frappe.whitelist()
def get_truck_stock_summary(vehicle):
	"""Everything the Vehicle form and the trip load line need about truck stock."""
	if not vehicle:
		return {}

	vehicle_doc = frappe.db.get_value(
		"Vehicle",
		vehicle,
		["custom_capacity", "custom_capacity_uom", "custom_vehicle_warehouse",
		 "custom_committed_qty", "custom_available_qty", "custom_status"],
		as_dict=True,
	) or frappe._dict()

	on_truck = get_truck_stock(vehicle)
	spoken_for = get_loaded_qty_on_other_trips(vehicle)
	total = sum(on_truck.values())
	capacity = flt(vehicle_doc.custom_capacity)

	items = []
	for item_code, qty in sorted(on_truck.items()):
		items.append(
			{
				"item_code": item_code,
				"item_name": frappe.get_cached_value("Item", item_code, "item_name"),
				"stock_uom": frappe.get_cached_value("Item", item_code, "stock_uom"),
				"qty": qty,
				"on_loaded_trips": flt(spoken_for.get(item_code)),
				"free": max(qty - flt(spoken_for.get(item_code)), 0.0),
			}
		)

	return {
		"vehicle": vehicle,
		"warehouse": vehicle_doc.custom_vehicle_warehouse,
		"capacity": capacity,
		"capacity_uom": vehicle_doc.custom_capacity_uom,
		"committed_qty": flt(vehicle_doc.custom_committed_qty),
		"available_qty": flt(vehicle_doc.custom_available_qty),
		"status": vehicle_doc.custom_status,
		"on_truck_qty": total,
		"physical_space": max(capacity - total, 0.0) if capacity else None,
		"over_capacity": bool(capacity) and total > capacity + QTY_TOLERANCE,
		"items": items,
	}


# ---------------------------------------------------------------------------
# Vehicle picker — free space and truck stock shown next to every plate
# ---------------------------------------------------------------------------


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def vehicle_query(doctype, txt, searchfield, start, page_len, filters):
	"""Link-field query for Delivery Trip.vehicle.

	Shows "free 120 · on truck: OPC-43 200" under each plate so the dispatcher
	sees the stock without opening the Vehicle. Only Available / Assigned trucks
	are offered; a truck Under Maintenance or On Trip is not.
	"""
	filters = filters or {}
	statuses = filters.get("custom_status") or ["Available", "Assigned"]
	if isinstance(statuses, (list, tuple)) and len(statuses) == 2 and statuses[0] == "in":
		statuses = statuses[1]

	conditions = {"custom_status": ("in", statuses)}
	if txt:
		conditions["name"] = ("like", f"%{txt}%")

	vehicles = frappe.get_all(
		"Vehicle",
		filters=conditions,
		fields=["name", "custom_capacity", "custom_status",
		        "custom_vehicle_warehouse", "custom_assigned_driver"],
		order_by="name",
		start=start,
		page_length=page_len,
	)

	warehouses = [v.custom_vehicle_warehouse for v in vehicles if v.custom_vehicle_warehouse]
	stock = {}
	if warehouses:
		for row in frappe.get_all(
			"Bin",
			filters={"warehouse": ("in", warehouses), "actual_qty": (">", 0)},
			fields=["warehouse", "item_code", "actual_qty"],
		):
			stock.setdefault(row.warehouse, []).append((row.item_code, flt(row.actual_qty)))

	# Free space is computed here, not read from Vehicle.custom_available_qty.
	# That stored figure is a cache; if a hook missed, the picker would offer a
	# truck that is actually full.
	from thameen_erp.overrides.vehicle_load import _committed_by_vehicle

	committed = _committed_by_vehicle([v.name for v in vehicles])
	on_hand = {}
	for wh, lines in stock.items():
		on_hand[wh] = sum(flt(qty) for _code, qty in lines)

	out = []
	for v in vehicles:
		items = stock.get(v.custom_vehicle_warehouse) or []
		capacity = flt(v.custom_capacity)
		used = max(flt(committed.get(v.name)), flt(on_hand.get(v.custom_vehicle_warehouse)))
		free = max(capacity - used, 0.0) if capacity else 0.0
		parts = [
			_("{0}").format(v.custom_status or ""),
			_("free {0} of {1}").format(flt(free, 2), flt(capacity, 2)),
			_("on truck: {0}").format(
				", ".join(f"{code} {qty:g}" for code, qty in items)
			) if items else _("on truck: empty"),
		]
		out.append((v.name, " · ".join(p for p in parts if p)))

	out.sort(key=lambda row: row[0])
	return out


# ---------------------------------------------------------------------------
# Manual load / unload from the Vehicle form
# ---------------------------------------------------------------------------


@frappe.whitelist()
def preview_manual_load(vehicle, direction, warehouse, items):
	"""Check a proposed manual movement without writing anything.

	Returns, per row, what is in the source and what the truck would hold
	afterwards, plus two flags the dialog turns into warnings:
	    over_capacity   truck would end up above its rating (load only)
	    insufficient    source does not hold enough (load or unload)
	"""
	items = _parse_items(items)
	direction = _parse_direction(direction)
	vehicle_wh = _require_vehicle_warehouse(vehicle)

	capacity = flt(frappe.get_cached_value("Vehicle", vehicle, "custom_capacity"))
	on_truck = get_truck_stock(vehicle)
	on_truck_total = sum(on_truck.values())

	source_wh = warehouse if direction == "load" else vehicle_wh
	rows, total_moving, shortfalls = [], 0.0, []

	for row in items:
		stock_qty = flt(row["qty"]) * (flt(row.get("conversion_factor")) or 1)
		source_qty = _bin_qty(row["item_code"], source_wh)
		short = max(stock_qty - source_qty, 0.0)
		if short > QTY_TOLERANCE:
			shortfalls.append({"item_code": row["item_code"], "short": short, "available": source_qty})
		rows.append(
			{
				**row,
				"stock_qty": stock_qty,
				"source_warehouse": source_wh,
				"source_qty": source_qty,
				"shortfall": short,
				"on_truck_now": flt(on_truck.get(row["item_code"])),
			}
		)
		total_moving += stock_qty

	after = on_truck_total + total_moving if direction == "load" else on_truck_total - total_moving

	return {
		"direction": direction,
		"vehicle_warehouse": vehicle_wh,
		"capacity": capacity,
		"on_truck_now": on_truck_total,
		"room_for": max(capacity - on_truck_total, 0.0) if capacity else 0.0,
		"moving": total_moving,
		"on_truck_after": max(after, 0.0),
		"over_capacity": direction == "load" and bool(capacity) and after > capacity + QTY_TOLERANCE,
		"over_by": max(after - capacity, 0.0) if capacity else 0.0,
		"insufficient": bool(shortfalls),
		"shortfalls": shortfalls,
		"rows": rows,
	}


@frappe.whitelist()
def find_stock_warehouse(item_code, company=None, exclude=None):
	"""Which warehouse should this item be loaded from?

	Picks the one holding the most, so the dispatcher does not have to guess.
	Returns `None` for `warehouse` when the item is nowhere at all, which the
	dialog turns into a plain "no stock anywhere" message instead of letting
	the load fail later inside the Stock Entry.
	"""
	filters = {"item_code": item_code, "actual_qty": (">", 0)}
	if exclude:
		filters["warehouse"] = ("!=", exclude)

	rows = frappe.get_all(
		"Bin",
		filters=filters,
		fields=["warehouse", "actual_qty"],
		order_by="actual_qty desc",
		limit_page_length=20,
	)

	if company:
		allowed = set(
			frappe.get_all(
				"Warehouse",
				filters={"company": company, "is_group": 0},
				pluck="name",
			)
		)
		rows = [r for r in rows if r.warehouse in allowed]

	# Never suggest another truck's warehouse as a yard to load from.
	truck_warehouses = set(
		frappe.get_all("Vehicle", filters={"custom_vehicle_warehouse": ("is", "set")},
		               pluck="custom_vehicle_warehouse")
	)
	rows = [r for r in rows if r.warehouse not in truck_warehouses]

	if not rows:
		return {"warehouse": None, "qty": 0.0, "others": []}

	return {
		"warehouse": rows[0].warehouse,
		"qty": flt(rows[0].actual_qty),
		"others": [{"warehouse": r.warehouse, "qty": flt(r.actual_qty)} for r in rows[1:6]],
	}


@frappe.whitelist()
def manual_load(vehicle, direction, warehouse, items, allow_over_capacity=0, remarks=None):
	"""Move stock yard → truck (load) or truck → yard (unload).

	One submitted Material Transfer. Over-capacity is a confirmed warning when
	the setting allows it; insufficient stock is always a hard stop — ERPNext
	would reject the Stock Entry anyway, this just says why in plain words
	first.
	"""
	frappe.has_permission("Stock Entry", "create", throw=True)

	preview = preview_manual_load(vehicle, direction, warehouse, items)
	direction = preview["direction"]

	if preview["insufficient"]:
		lines = ", ".join(
			_("{0} (short {1}, only {2} in {3})").format(
				s["item_code"], flt(s["short"], 2), flt(s["available"], 2),
				preview["rows"][0]["source_warehouse"],
			)
			for s in preview["shortfalls"]
		)
		frappe.throw(
			_("Not enough stock to {0}: {1}.").format(
				_("load") if direction == "load" else _("unload"), lines
			),
			title=_("Insufficient Stock"),
		)

	if preview["over_capacity"]:
		# A truck rated 20 holding 10 takes 10 more, not 20. Loading past that
		# is refused unless the setting is deliberately switched on.
		message = _("{0} holds {1} of {2} — room for {3} more, and this would load {4}.").format(
			frappe.bold(vehicle),
			flt(preview["on_truck_now"], 2),
			flt(preview["capacity"], 2),
			flt(preview["room_for"], 2),
			flt(preview["moving"], 2),
		)
		allowed = cint(
			frappe.db.get_single_value("Thameen Fleet Settings", "allow_over_capacity_manual_load")
		)
		if not allowed:
			frappe.throw(message, title=_("Over Capacity"))
		if not cint(allow_over_capacity):
			frappe.throw(message + " " + _("Confirm the overload to continue."), title=_("Over Capacity"))

	vehicle_wh = preview["vehicle_warehouse"]
	company = frappe.db.get_value("Warehouse", vehicle_wh, "company")
	cost_center = frappe.get_cached_value("Vehicle", vehicle, "custom_cost_center")

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer"
	se.company = company
	se.custom_vehicle = vehicle
	se.custom_vehicle_load_type = "Manual Load" if direction == "load" else "Manual Unload"
	se.remarks = remarks or (
		_("Manual load onto {0} from the Vehicle form").format(vehicle)
		if direction == "load"
		else _("Manual unload from {0} back to {1}").format(vehicle, warehouse)
	)

	for row in preview["rows"]:
		se.append(
			"items",
			{
				"item_code": row["item_code"],
				"qty": flt(row["qty"]),
				"uom": row.get("uom"),
				"conversion_factor": flt(row.get("conversion_factor")) or 1,
				"s_warehouse": warehouse if direction == "load" else vehicle_wh,
				"t_warehouse": vehicle_wh if direction == "load" else warehouse,
				"cost_center": cost_center,
			},
		)

	se.insert()
	se.submit()

	frappe.msgprint(
		_("{0} {1} via {2}. {3} now holds {4}.").format(
			_("Loaded") if direction == "load" else _("Unloaded"),
			flt(preview["moving"], 2),
			get_link_to_form("Stock Entry", se.name),
			vehicle,
			flt(preview["on_truck_after"], 2),
		),
		indicator="orange" if preview["over_capacity"] else "green",
		alert=True,
	)

	return {"stock_entry": se.name, "on_truck_after": preview["on_truck_after"]}


@frappe.whitelist()
def make_purchase_order_for_shortfall(vehicle, warehouse, shortfalls, supplier=None):
	"""Draft PO for what the yard is missing, raised from the Vehicle load dialog.

	Into the warehouse the dispatcher was trying to load from, so that once it
	is received the same Load Stock action goes through unchanged.
	"""
	frappe.has_permission("Purchase Order", "create", throw=True)

	if isinstance(shortfalls, str):
		shortfalls = json.loads(shortfalls or "[]")
	shortfalls = [s for s in (shortfalls or []) if s.get("item_code") and flt(s.get("short")) > 0]
	if not shortfalls:
		frappe.throw(_("Nothing is short."))

	supplier = supplier or frappe.db.get_single_value("Thameen Fleet Settings", "default_cement_supplier")
	if not supplier:
		frappe.throw(_("Choose a Supplier, or set a Default Cement Supplier in Thameen Fleet Settings."))

	company = frappe.db.get_value("Warehouse", warehouse, "company")
	from frappe.utils import nowdate

	po = frappe.new_doc("Purchase Order")
	po.supplier = supplier
	po.company = company
	po.transaction_date = nowdate()
	po.schedule_date = nowdate()
	po.set_warehouse = warehouse
	for s in shortfalls:
		po.append(
			"items",
			{
				"item_code": s["item_code"],
				"qty": flt(s["short"]),
				"uom": frappe.get_cached_value("Item", s["item_code"], "stock_uom"),
				"conversion_factor": 1,
				"warehouse": warehouse,
				"schedule_date": po.schedule_date,
			},
		)
	po.flags.ignore_mandatory = True
	po.insert()

	frappe.msgprint(
		_("Purchase Order {0} created in draft for {1}. Load {2} again once it is received.").format(
			get_link_to_form("Purchase Order", po.name), supplier, vehicle
		),
		indicator="green",
	)
	return po.name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bin_qty(item_code, warehouse):
	if not (item_code and warehouse):
		return 0.0
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))


def _require_vehicle_warehouse(vehicle):
	if not vehicle:
		frappe.throw(_("Choose a vehicle."))
	warehouse = get_vehicle_warehouse(vehicle)
	if not warehouse:
		frappe.throw(
			_("Vehicle {0} has no vehicle warehouse. Re-save the Vehicle with "
			  "'Auto-create Cost Center & Warehouse' ticked.").format(frappe.bold(vehicle))
		)
	return warehouse


def _parse_direction(direction):
	direction = (direction or "load").strip().lower()
	if direction not in ("load", "unload"):
		frappe.throw(_("Direction must be load or unload."))
	return direction


def _parse_items(items):
	if isinstance(items, str):
		items = json.loads(items or "[]")
	items = [row for row in (items or []) if row.get("item_code") and flt(row.get("qty")) > 0]
	if not items:
		frappe.throw(_("Add at least one item with a quantity."))

	for row in items:
		if not frappe.get_cached_value("Item", row["item_code"], "is_stock_item"):
			frappe.throw(_("{0} is not a stock item and cannot be loaded on a truck.").format(row["item_code"]))
		stock_uom = frappe.get_cached_value("Item", row["item_code"], "stock_uom")
		row.setdefault("uom", stock_uom)
		if not flt(row.get("conversion_factor")):
			row["conversion_factor"] = (
				1.0
				if row["uom"] == stock_uom
				else flt(
					frappe.db.get_value(
						"UOM Conversion Detail",
						{"parent": row["item_code"], "uom": row["uom"]},
						"conversion_factor",
					)
				)
				or 1.0
			)
	return items
