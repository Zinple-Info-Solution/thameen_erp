"""When the yard does not have the cement: buy it, or have the supplier deliver.

Two paths, chosen on the Delivery Trip — plus a variant of the first that
turns into the second partway through.

1. Shortfall purchase (Supply Source = Own Warehouse)
   The trip still loads from the yard, but the yard is short. The dispatcher is
   shown exactly how short, per item, and can raise a Purchase Order (or a
   Material Request for the buyer to action) for the shortfall — into the
   trip's own vehicle warehouse when one is already assigned, the loading
   warehouse otherwise (`make_purchase_order`, `_vehicle_warehouse_or_loading`).

   Received into the yard, the trip just loads as normal. Received straight
   onto the vehicle warehouse instead, `link_shortfall_receipt_to_trip` below
   notices on submit, links the receipt to the trip, and flips its route to
   Supplier to Customer — the trip becomes path 2 from here on, and Loading
   finds the stock already there instead of raising a Material Transfer from
   a yard that was never actually used.

2. Direct from Supplier (Supply Source = Direct from Supplier)
   The truck collects at the supplier's plant and drives straight to the
   customer. The cement never enters the yard:

       Loading    → Purchase Receipt  supplier → vehicle warehouse
       Delivered  → Delivery Note     vehicle warehouse → customer  (unchanged)

   Accounting stays honest: the receipt books stock at cost against the PO, the
   note relieves it at the sale price, and the month-end bill and the truck
   profitability report need no changes at all because they only ever read
   Delivery Notes.

   ERPNext's own drop-ship (delivered_by_supplier) was deliberately NOT used.
   It bypasses the Delivery Note, and the Delivery Note is what this app bills
   and reports from.

Every trip row remembers which PO line it is drawing on (`purchase_order` /
`po_detail`) and which receipt line brought the stock in (`purchase_receipt` /
`purchase_receipt_item`), so a trip can be traced plant → truck → site.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint, flt, get_link_to_form, getdate, now_datetime, nowdate

from thameen_erp.overrides.vehicle_stock import QTY_TOLERANCE, free_truck_stock

DIRECT = "Direct from Supplier"
OWN = "Own Warehouse"


# ---------------------------------------------------------------------------
# Stock check
# ---------------------------------------------------------------------------


@frappe.whitelist()
def check_trip_stock(trip, vehicle=None):
	"""Per row: planned, already on the truck (free), in the source warehouse,
	and how much is missing. Nothing is written.

	Direct-from-supplier trips are not checked against the yard — their stock
	is the Purchase Order, so the check reports the PO's state instead.
	"""
	doc = frappe.get_doc("Delivery Trip", trip)
	vehicle = vehicle or doc.get("vehicle")
	rows = [row for row in (doc.get("custom_trip_items") or []) if flt(row.qty) > 0]

	result = {
		"trip": trip,
		"vehicle": vehicle,
		"supply_source": doc.get("custom_supply_source") or OWN,
		"purchase_order": doc.get("custom_purchase_order"),
		"rows": [],
		"shortfalls": [],
		"sufficient": True,
	}

	if result["supply_source"] == DIRECT:
		result.update(_describe_po(doc))
		return result

	item_codes = list({row.item_code for row in rows})
	truck_free = free_truck_stock(vehicle, item_codes, exclude_trip=trip) if vehicle else {}
	truck_used = {}

	for row in rows:
		needed = flt(row.qty) * (flt(row.conversion_factor) or 1)
		source = row.source_warehouse or doc.get("custom_loading_warehouse")

		free_here = max(flt(truck_free.get(row.item_code)) - flt(truck_used.get(row.item_code)), 0.0)
		from_truck = min(needed, free_here)
		truck_used[row.item_code] = flt(truck_used.get(row.item_code)) + from_truck

		source_qty = _bin_qty(row.item_code, source)
		from_source = min(needed - from_truck, source_qty)
		short = max(needed - from_truck - source_qty, 0.0)

		line = {
			"idx": row.idx,
			"item_code": row.item_code,
			"item_name": row.item_name,
			"uom": row.uom,
			"stock_uom": row.stock_uom,
			"conversion_factor": flt(row.conversion_factor) or 1,
			"rate": flt(row.rate),
			"planned_qty": needed,
			"on_truck_free": from_truck,
			"source_warehouse": source,
			"source_qty": source_qty,
			"from_source": from_source,
			"shortfall": short if short > QTY_TOLERANCE else 0.0,
			"sales_order": row.sales_order,
			"so_detail": row.so_detail,
		}
		result["rows"].append(line)
		if line["shortfall"]:
			result["shortfalls"].append(line)
			result["sufficient"] = False

	# Several rows of the same item drawing on one warehouse share its stock.
	_net_off_shared_source(result)
	return result


def _net_off_shared_source(result):
	"""Two rows of OPC-43 from Main Store each see the full Main Store balance
	above. Walk them in order and let each consume what the earlier ones took.

	The vehicle decides everything now, on purpose: `shortfall` is what is
	not yet physically on the truck, and that single number drives the
	submit block, the Insufficient Stock dialog, and what gets bought.
	The yard having plenty does not reduce it; it is reported
	(`from_source`/`source_qty`) purely as information for the Loading
	step, never as an offset.
	"""
	taken = {}
	result["shortfalls"] = []
	result["sufficient"] = True
	for line in result["rows"]:
		key = (line["item_code"], line["source_warehouse"])
		left = max(flt(line["source_qty"]) - flt(taken.get(key)), 0.0)
		need_from_source = flt(line["planned_qty"]) - flt(line["on_truck_free"])
		from_source = min(max(need_from_source, 0.0), left)
		taken[key] = flt(taken.get(key)) + from_source
		line["from_source"] = from_source

		short = max(need_from_source, 0.0)
		line["shortfall"] = short if short > QTY_TOLERANCE else 0.0

		if line["shortfall"]:
			result["shortfalls"].append(line)
			result["sufficient"] = False


def _describe_po(doc):
	po = doc.get("custom_purchase_order")
	if not po:
		return {"po_status": "missing", "po_docstatus": None, "sufficient": False}
	docstatus, status, per_received = frappe.db.get_value(
		"Purchase Order", po, ["docstatus", "status", "per_received"]
	)
	return {
		"po_status": status,
		"po_docstatus": docstatus,
		"po_per_received": flt(per_received),
		"sufficient": docstatus == 1 and status not in ("Closed", "Cancelled"),
	}


# ---------------------------------------------------------------------------
# Buying the shortfall
# ---------------------------------------------------------------------------


@frappe.whitelist()
def make_purchase_order(trip, supplier=None, rows=None, mode="shortfall", schedule_date=None):
	"""Draft Purchase Order from a trip.

	mode = "shortfall"  one line per short row, qty = shortfall, into the
	                    trip's own vehicle warehouse (or the loading warehouse
	                    if no vehicle is assigned yet). Used when the yard is
	                    short.
	mode = "direct"     one line per trip row, full planned qty, flagged for
	                    collection by the truck. Used for direct supply.

	The PO is left in Draft for the buyer to price and submit — dispatch plans
	trips, purchasing commits money.
	"""
	frappe.has_permission("Purchase Order", "create", throw=True)

	doc = frappe.get_doc("Delivery Trip", trip)
	supplier = supplier or doc.get("custom_supplier") or frappe.db.get_single_value(
		"Thameen Fleet Settings", "default_cement_supplier"
	)
	if not supplier:
		frappe.throw(_("Choose a Supplier, or set a Default Cement Supplier in Thameen Fleet Settings."))

	if isinstance(rows, str):
		rows = json.loads(rows or "[]")

	if mode == "direct":
		if doc.get("custom_purchase_order") and frappe.db.get_value(
			"Purchase Order", doc.custom_purchase_order, "docstatus"
		) != 2:
			frappe.throw(
				_("{0} already has Purchase Order {1}. Cancel it first if it is wrong.").format(
					trip, get_link_to_form("Purchase Order", doc.custom_purchase_order)
				)
			)
		lines = [
			{
				"row_name": row.name,
				"item_code": row.item_code,
				"qty": flt(row.qty),
				"uom": row.uom,
				"conversion_factor": flt(row.conversion_factor) or 1,
				"warehouse": _vehicle_warehouse_or_loading(doc),
			}
			for row in (doc.get("custom_trip_items") or [])
			if flt(row.qty) > 0
		]
	else:
		check = check_trip_stock(trip)
		wanted = {r.get("idx") for r in (rows or [])} if rows else None
		# Straight onto the truck's own warehouse when it has one — the buyer
		# then makes the Purchase Receipt against that same warehouse, and
		# `link_shortfall_receipt_to_trip` picks it up on submit and links it
		# back to this trip, same as a direct-supply PO already does. Only
		# falls back to the loading warehouse when no vehicle is assigned yet.
		warehouse = _vehicle_warehouse_or_loading(doc)
		# `shortfall` is what is not yet on the truck, full stop. The yard's
		# own stock is not netted off: if the vehicle does not have it, it is
		# bought, even when the yard already holds it and only loading is
		# needed.
		lines = [
			{
				"row_name": None,
				"item_code": r["item_code"],
				"qty": flt(r["shortfall"]) / (flt(r["conversion_factor"]) or 1),
				"uom": r["uom"],
				"conversion_factor": flt(r["conversion_factor"]) or 1,
				"warehouse": warehouse,
			}
			for r in check["shortfalls"]
			if flt(r.get("shortfall")) > QTY_TOLERANCE and (not wanted or r["idx"] in wanted)
		]

	if not lines:
		frappe.throw(_("Nothing to order — every row is covered by stock on hand."))

	po = frappe.new_doc("Purchase Order")
	po.supplier = supplier
	po.company = doc.company
	po.transaction_date = nowdate()
	po.schedule_date = getdate(schedule_date or doc.get("departure_time") or nowdate())
	if po.schedule_date < getdate(nowdate()):
		po.schedule_date = getdate(nowdate())
	po.custom_delivery_trip = trip
	po.set_warehouse = lines[0]["warehouse"]

	for line in lines:
		po.append(
			"items",
			{
				"item_code": line["item_code"],
				"qty": line["qty"],
				"uom": line["uom"],
				"conversion_factor": line["conversion_factor"],
				"warehouse": line["warehouse"],
				"schedule_date": po.schedule_date,
			},
		)

	po.flags.ignore_mandatory = True
	po.insert()

	if mode == "direct":
		# Remember which PO line each trip row collects against.
		by_item = {}
		for po_row in po.items:
			by_item.setdefault(po_row.item_code, []).append(po_row)
		for line in lines:
			po_row = by_item[line["item_code"]].pop(0)
			frappe.db.set_value(
				"Delivery Trip Item",
				line["row_name"],
				{"purchase_order": po.name, "po_detail": po_row.name},
				update_modified=False,
			)
		doc.db_set("custom_purchase_order", po.name, update_modified=False)
		if doc.get("custom_supplier") != supplier:
			doc.db_set("custom_supplier", supplier, update_modified=False)
	elif not doc.get("custom_purchase_order"):
		doc.db_set("custom_purchase_order", po.name, update_modified=False)

	frappe.msgprint(
		_("Purchase Order {0} created in draft for {1}. Price it and submit it before the trip loads.").format(
			get_link_to_form("Purchase Order", po.name), supplier
		),
		indicator="green",
		title=_("Purchase Order Raised"),
	)
	return po.name


@frappe.whitelist()
def make_material_request(trip, rows=None):
	"""Draft Material Request (Purchase) for the shortfall — when the
	dispatcher is not the one who chooses the supplier."""
	frappe.has_permission("Material Request", "create", throw=True)

	doc = frappe.get_doc("Delivery Trip", trip)
	check = check_trip_stock(trip)
	if isinstance(rows, str):
		rows = json.loads(rows or "[]")
	wanted = {r.get("idx") for r in (rows or [])} if rows else None
	# `shortfall` is what is not yet on the truck. A row is requested here
	# even if the yard already holds it; only Loading, not this request,
	# checks the yard.
	lines = [
		r
		for r in check["shortfalls"]
		if flt(r.get("shortfall")) > QTY_TOLERANCE and (not wanted or r["idx"] in wanted)
	]
	if not lines:
		frappe.throw(_("Nothing short — every row is covered by stock on hand."))

	mr = frappe.new_doc("Material Request")
	mr.material_request_type = "Purchase"
	mr.company = doc.company
	mr.transaction_date = nowdate()
	mr.schedule_date = getdate(doc.get("departure_time") or nowdate())
	if mr.schedule_date < getdate(nowdate()):
		mr.schedule_date = getdate(nowdate())
	mr.custom_delivery_trip = trip
	mr.custom_vehicle = doc.get("vehicle")

	for r in lines:
		mr.append(
			"items",
			{
				"item_code": r["item_code"],
				"qty": flt(r["shortfall"]) / (flt(r["conversion_factor"]) or 1),
				"uom": r["uom"],
				"conversion_factor": flt(r["conversion_factor"]) or 1,
				"warehouse": r["source_warehouse"],
				"schedule_date": mr.schedule_date,
			},
		)

	mr.insert()
	frappe.msgprint(
		_("Material Request {0} created for the shortfall.").format(get_link_to_form("Material Request", mr.name)),
		indicator="green",
	)
	return mr.name


# ---------------------------------------------------------------------------
# Direct supply: receiving onto the truck at Loading
# ---------------------------------------------------------------------------


def receive_onto_vehicle(trip_doc):
	"""Purchase Receipt supplier → vehicle warehouse for every trip row.

	Called by ThameenDeliveryTrip.load_vehicle when Supply Source is Direct
	from Supplier. Idempotent: a second call finds the existing receipt.
	"""
	rows = [row for row in (trip_doc.get("custom_trip_items") or []) if flt(row.qty) > 0]
	if not rows:
		return None

	po_name = trip_doc.get("custom_purchase_order")
	if not po_name:
		frappe.throw(
			_("This is a direct-from-supplier trip but it has no Purchase Order. "
			  "Use Create > Purchase Order on the trip first.")
		)

	po = frappe.get_doc("Purchase Order", po_name)
	if po.docstatus != 1:
		frappe.throw(
			_("Purchase Order {0} is still a draft. The buyer must submit it before the truck can collect.").format(
				get_link_to_form("Purchase Order", po_name)
			)
		)
	if po.status in ("Closed", "Cancelled"):
		frappe.throw(_("Purchase Order {0} is {1}.").format(po_name, po.status))

	existing = frappe.db.get_value(
		"Purchase Receipt", {"custom_delivery_trip": trip_doc.name, "docstatus": 1}, "name"
	)
	if existing:
		return existing

	vehicle_wh = frappe.db.get_value("Vehicle", trip_doc.vehicle, "custom_vehicle_warehouse")
	if not vehicle_wh:
		frappe.throw(_("Vehicle {0} has no vehicle warehouse.").format(trip_doc.vehicle))

	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	pr = make_purchase_receipt(po_name)
	pr.custom_delivery_trip = trip_doc.name
	pr.custom_vehicle = trip_doc.vehicle
	pr.set_warehouse = vehicle_wh
	pr.remarks = _("Collected by {0} for trip {1} — direct to {2}").format(
		trip_doc.vehicle, trip_doc.name, trip_doc.get("custom_delivery_location") or _("site")
	)

	# Match trip rows to PO lines. Prefer the stored po_detail; fall back to
	# item code for trips whose PO was raised by hand.
	by_detail = {row.po_detail: row for row in rows if row.po_detail}
	by_item = {}
	for row in rows:
		if not row.po_detail:
			by_item.setdefault(row.item_code, []).append(row)

	kept = []
	for pr_row in pr.items:
		trip_row = by_detail.get(pr_row.purchase_order_item)
		if not trip_row and by_item.get(pr_row.item_code):
			trip_row = by_item[pr_row.item_code].pop(0)
		if not trip_row:
			continue
		pr_row.qty = flt(trip_row.qty)
		pr_row.received_qty = flt(trip_row.qty)
		pr_row.warehouse = vehicle_wh
		pr_row.cost_center = trip_doc.get("custom_cost_center") or pr_row.cost_center
		pr_row._trip_row = trip_row.name
		kept.append(pr_row)

	if not kept:
		frappe.throw(
			_("None of the trip rows match a pending line on Purchase Order {0}.").format(po_name)
		)

	pr.items = kept
	for idx, pr_row in enumerate(pr.items, start=1):
		pr_row.idx = idx

	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()

	for pr_row in pr.items:
		trip_row_name = getattr(pr_row, "_trip_row", None)
		if trip_row_name:
			frappe.db.set_value(
				"Delivery Trip Item",
				trip_row_name,
				{
					"purchase_order": po_name,
					"po_detail": pr_row.purchase_order_item,
					"purchase_receipt": pr.name,
					"purchase_receipt_item": pr_row.name,
				},
				update_modified=False,
			)

	trip_doc.db_set("custom_purchase_receipt", pr.name, update_modified=False)

	frappe.msgprint(
		_("Received {0} row(s) from {1} straight onto {2} via {3}").format(
			len(kept), po.supplier, trip_doc.vehicle, get_link_to_form("Purchase Receipt", pr.name)
		),
		indicator="green",
		alert=True,
	)
	return pr.name


def _link_ordinary_receipt_to_waiting_trip(doc):
	"""An ordinary receipt into the yard — no vehicle-warehouse row at all,
	so none of the Direct-from-Supplier machinery below applies. The goods
	are correctly sitting in the yard; Loading still moves them onto the
	truck the normal way.

	The only thing missing without this: a trip whose shortfall was bought
	through 'Material Request instead' (a buyer's own Purchase Order, not
	`make_purchase_order`) never gets `custom_purchase_order` or
	`custom_purchase_receipt` recorded on it at all, so it shows none of the
	View buttons a shortfall bought directly does. This fills those in,
	purely for traceability — never `custom_supply_source`, destination or
	route, which stay exactly as a yard-sourced trip's already are.

	A pickup trip is the one exception: it never moves forward from
	anything but a vehicle-warehouse receipt (see
	`link_shortfall_receipt_to_trip`, the sibling that handles that case),
	so tracing back to one HERE means its Purchase Receipt was made against
	the yard by mistake — refused outright, while it can still be fixed,
	rather than left to quietly strand the trip at "Trip Started" forever.
	"""
	from thameen_erp.overrides.delivery_trip import PICKUP_ROUTE
	from thameen_erp.overrides.po_trips import _traced_trip

	trip_name = _traced_trip(doc.name)
	if not trip_name:
		return

	row = frappe.db.get_value(
		"Delivery Trip", trip_name,
		["custom_purchase_order", "custom_purchase_receipt", "custom_trip_route", "vehicle"],
	)
	if not row:
		return
	trip_po, trip_pr, trip_route, trip_vehicle = row

	if trip_route == PICKUP_ROUTE:
		frappe.throw(
			_("{0} is a pickup trip — its Purchase Receipt must be received into {1}'s own vehicle "
			  "warehouse, not the yard, or the trip can never move past \"Trip Started\". Cancel "
			  "this receipt and remake it with that warehouse.").format(
				get_link_to_form("Delivery Trip", trip_name), trip_vehicle or _("the vehicle")
			),
			title=_("Wrong Warehouse for Pickup Trip"),
		)

	po_names = {row.purchase_order for row in doc.items if row.get("purchase_order")}

	updates = {}
	if not trip_po and len(po_names) == 1:
		updates["custom_purchase_order"] = po_names.pop()
	if not trip_pr:
		updates["custom_purchase_receipt"] = doc.name
	if updates:
		frappe.db.set_value("Delivery Trip", trip_name, updates, update_modified=False)


def _open_pickup_trips_for_pos(po_names):
	"""{vehicle: {trip, warehouse, driver, purchase_order}} for every pickup
	trip still waiting on its receipt (Trip Started) against one of these
	Purchase Orders — the candidates a receipt's own Pickup Vehicles table
	may name. `driver` is shown there purely for visibility — it was already
	fixed when the pickup trip was created and submitted, nothing to pick
	again on the receipt."""
	po_names = [p for p in (po_names or []) if p]
	if not po_names:
		return {}

	from thameen_erp.overrides.delivery_trip import PICKUP_ROUTE

	trips = frappe.get_all(
		"Delivery Trip",
		filters={
			"custom_trip_route": PICKUP_ROUTE,
			"custom_purchase_order": ("in", po_names),
			"docstatus": 1,
			"status": "Trip Started",
		},
		fields=["name", "vehicle", "driver", "custom_purchase_order"],
	)
	if not trips:
		return {}

	warehouses = {
		v.name: v.custom_vehicle_warehouse
		for v in frappe.get_all(
			"Vehicle", filters={"name": ("in", [t.vehicle for t in trips])}, fields=["name", "custom_vehicle_warehouse"]
		)
	}
	return {
		t.vehicle: {
			"trip": t.name,
			"warehouse": warehouses.get(t.vehicle),
			"driver": t.driver,
			"purchase_order": t.custom_purchase_order,
		}
		for t in trips
	}


def _trip_claims(trip_names):
	"""{trip_name: [{po_detail, item_code, qty}]} — each trip's own item
	rows, in stock qty, grouped by the PO line they were cut from. A single
	PO line split across several vehicles gives every one of those trips the
	SAME po_detail — qty is what actually tells them apart, so both travel
	together wherever this is used to find the matching receipt row.

	`item_code` rides along too: a receipt row typed in by hand (not pulled
	via "Get Items From Purchase Order") never gets `purchase_order_item`
	set at all — ERPNext only stamps that during mapped-doc creation — so
	matching such a row needs a fallback that does not depend on po_detail
	existing on the row in the first place.
	"""
	if not trip_names:
		return {}
	rows = frappe.get_all(
		"Delivery Trip Item",
		filters={"parent": ("in", trip_names), "po_detail": ("is", "set")},
		fields=["parent", "po_detail", "item_code", "qty", "conversion_factor"],
	)
	out = {}
	for row in rows:
		out.setdefault(row.parent, []).append(
			{
				"po_detail": row.po_detail,
				"item_code": row.item_code,
				"qty": flt(row.qty) * (flt(row.conversion_factor) or 1),
			}
		)
	return out


@frappe.whitelist()
def pickup_vehicles_for_pos(purchase_orders):
	"""Every vehicle with an open pickup trip against these Purchase Orders —
	for the Purchase Receipt's own "Add Pickup Vehicles" button, so filling in
	its Pickup Vehicles table is one click instead of hunting down which
	trucks are out for which PO. `claims` per candidate, same reason as in
	`pickup_trip_for_vehicle`: matching item rows to the right vehicle's
	warehouse by `po_detail` ALONE cannot tell two vehicles apart when they
	split the very same PO line — qty is what does."""
	if isinstance(purchase_orders, str):
		purchase_orders = json.loads(purchase_orders or "[]")
	found = _open_pickup_trips_for_pos(purchase_orders)
	if not found:
		return []

	claims_by_trip = _trip_claims([info["trip"] for info in found.values()])
	return [
		{
			"vehicle": vehicle,
			"delivery_trip": info["trip"],
			"driver": info["driver"],
			"warehouse": info["warehouse"],
			"purchase_order": info["purchase_order"],
			"claims": claims_by_trip.get(info["trip"], []),
		}
		for vehicle, info in found.items()
	]


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def pickup_vehicle_query(doctype, txt, searchfield, start, page_len, filters):
	"""Link-field query for a Purchase Receipt Pickup Vehicle row's `vehicle`
	— only trucks with an open pickup trip against one of this receipt's own
	Purchase Orders are offered, same reasoning as `empty_vehicle_query`:
	picking the wrong truck should not even be possible."""
	filters = filters or {}
	po_names = filters.get("purchase_orders") or []
	if isinstance(po_names, str):
		po_names = json.loads(po_names or "[]")

	found = _open_pickup_trips_for_pos(po_names)
	needle = (txt or "").lower()
	out = [
		(vehicle, f"→ {info['trip']}")
		for vehicle, info in found.items()
		if not needle or needle in vehicle.lower()
	]
	out.sort(key=lambda row: row[0])
	return out[start : start + page_len]


@frappe.whitelist()
def pickup_trip_for_vehicle(vehicle, purchase_orders):
	"""The open pickup trip (and its warehouse) this vehicle is actually out
	on, scoped to this receipt's own Purchase Order(s) — what a Purchase
	Receipt Pickup Vehicle row auto-fills the moment its Vehicle is picked.

	`claims` — this trip's own item rows as {po_detail, qty} — is what the
	form uses to find which of the receipt's OWN item rows to auto-set to
	this same warehouse. `po_detail` alone is not enough: a single PO line
	split across two vehicles gives BOTH of their trips that same po_detail,
	only their claimed qty actually differs, so both travel together.
	"""
	if isinstance(purchase_orders, str):
		purchase_orders = json.loads(purchase_orders or "[]")
	found = _open_pickup_trips_for_pos(purchase_orders)
	info = found.get(vehicle)
	if not info:
		return None

	info = dict(info)
	info["claims"] = _trip_claims([info["trip"]]).get(info["trip"], [])
	return info


def validate_pickup_receipt_warehouse(doc, method=None):
	"""A receipt covering a pickup trip's Purchase Order must name every
	vehicle it is settling in its own Pickup Vehicles table, and every item
	row against that PO must land in that vehicle's own warehouse — checked
	here, before submit, instead of leaving `_link_ordinary_receipt_to_waiting_trip`
	to catch a wrong warehouse only after the fact.

	One receipt can cover several trucks at once (the user does not want a
	separate Purchase Receipt per vehicle) — so this is a many-to-many check:
	every pickup PO referenced by an item row needs at least one matching
	Pickup Vehicles row, and every item row's warehouse must be one of the
	warehouses named for ITS OWN Purchase Order, not just any of them.

	Self-heals the one case that is never actually ambiguous: a PO with only
	ONE truck listed for it AND that truck can actually hold everything this
	receipt is putting against this PO. There is nothing to choose between
	there, so a mismatched row's warehouse is silently corrected on every
	save — not just when the browser's own auto-fill happened to already
	run. But "only one truck listed" is not by itself proof there is no
	real problem: if the rows add up to more than that one truck's own
	rated capacity, forcing them all onto it would create cement that
	physically cannot be there — that is a missing second truck, not an
	unambiguous single-truck receipt, so it is left for the mismatch error
	below to explain instead of being silently forced.
	"""
	po_names = list({row.purchase_order for row in doc.items if row.get("purchase_order")})
	if not po_names:
		return

	open_trips = _open_pickup_trips_for_pos(po_names)
	pickup_pos = {info["purchase_order"] for info in open_trips.values()}
	if not pickup_pos:
		return

	chosen_rows = list(_pickup_vehicle_rows(doc, open_trips))
	chosen = {row.warehouse: row.purchase_order for row in chosen_rows}
	warehouses_by_po = {}
	for row in chosen_rows:
		warehouses_by_po.setdefault(row.purchase_order, set()).add(row.warehouse)

	capacity_by_warehouse = {}
	if warehouses_by_po:
		all_warehouses = {wh for whs in warehouses_by_po.values() for wh in whs}
		for v in frappe.get_all(
			"Vehicle",
			filters={"custom_vehicle_warehouse": ("in", list(all_warehouses))},
			fields=["custom_vehicle_warehouse as warehouse", "custom_capacity as capacity"],
		):
			capacity_by_warehouse[v.warehouse] = flt(v.capacity)

	def _row_qty(row):
		return flt(row.qty) * (flt(row.conversion_factor) or 1)

	over_capacity = []
	for row in doc.items:
		if row.purchase_order not in pickup_pos:
			continue
		if chosen.get(row.warehouse) == row.purchase_order:
			continue
		candidates = warehouses_by_po.get(row.purchase_order) or set()
		if len(candidates) != 1:
			continue

		only_warehouse = next(iter(candidates))
		capacity = capacity_by_warehouse.get(only_warehouse)
		if capacity:
			total_for_po = sum(
				_row_qty(r)
				for r in doc.items
				if r.purchase_order == row.purchase_order
				and (r.name == row.name or r.warehouse == only_warehouse)
			)
			if total_for_po > capacity + QTY_TOLERANCE:
				over_capacity.append(
					_("Row {0} ({1}): this Purchase Order needs {2} on {3}, but that truck is only rated "
					  "for {4}. Add a second truck to Pickup Vehicles for this PO, or split the qty across "
					  "the receipt rows to match each truck.").format(
						row.idx, row.item_code, flt(total_for_po, 2), only_warehouse, flt(capacity, 2)
					)
				)
				continue

		row.warehouse = only_warehouse

	if over_capacity and doc._action == "submit":
		frappe.throw("<br>".join(over_capacity), title=_("Truck Over Capacity"))

	if doc._action != "submit":
		return

	missing_pos = sorted(pickup_pos - set(chosen.values()))
	if missing_pos:
		frappe.throw(
			_("This receipt covers a pickup trip's Purchase Order ({0}) but no vehicle for it is listed "
			  "in Pickup Vehicles below. Add the truck(s) it is for before submitting.").format(
				", ".join(missing_pos)
			),
			title=_("Pickup Vehicle Missing"),
		)

	errors = []
	for row in doc.items:
		if row.purchase_order not in pickup_pos:
			continue
		if chosen.get(row.warehouse) != row.purchase_order:
			allowed = sorted(wh for wh, po in chosen.items() if po == row.purchase_order)
			errors.append(
				_("Row {0} ({1}): must be received into {2}, not {3}.").format(
					row.idx, row.item_code, " or ".join(allowed) or _("a Pickup Vehicle's warehouse"), row.warehouse or _("(blank)")
				)
			)

	if errors:
		frappe.throw(
			"<br>".join(errors),
			title=_("Wrong Warehouse for Pickup Trip"),
		)


def _pickup_vehicle_rows(doc, open_trips):
	"""Validate + resolve the receipt's own Pickup Vehicles table against
	`open_trips` (vehicle -> {trip, warehouse, purchase_order}), throwing on
	anything that does not actually match a real open pickup trip."""
	for row in doc.get("custom_pickup_vehicles") or []:
		info = open_trips.get(row.vehicle)
		if not info:
			frappe.throw(
				_("Pickup Vehicles row {0}: {1} has no open pickup trip against this receipt's "
				  "Purchase Order(s).").format(row.idx, row.vehicle or _("(blank)")),
				title=_("Pickup Vehicle Missing"),
			)
		yield frappe._dict(warehouse=info["warehouse"], purchase_order=info["purchase_order"])


def link_shortfall_receipt_to_trip(doc, method=None):
	"""A third path onto Direct-from-Supplier, alongside `receive_onto_vehicle`
	and `make_purchase_order` above: dispatch buys the shortfall into the
	yard as usual (mode="shortfall"), but the buyer receives it straight onto
	the truck's own warehouse instead — an ordinary Purchase Receipt, just
	pointed at the vehicle warehouse rather than the yard.

	That single choice of warehouse is the trigger. When it fires: the trip
	that raised the Purchase Order this receipt is against gets the receipt
	linked, and its route flips to Supplier to Customer — same destination,
	but Loading now finds this receipt already sitting there (see
	`receive_onto_vehicle`, which is idempotent) instead of raising a
	Material Transfer from a yard that was never actually used.

	Ordinary restocks are never touched: this only fires when the Purchase
	Order traces back to a trip at all, which only ever happens for a
	shortfall purchase or a direct-supply one — never a plain restock PO with
	no Delivery Trip behind it.

	The Sales Order on the trip is left exactly as it is, deliberately not
	re-derived here the way `redirect_trip`'s Customer branch does: that
	function exists for a trip with NO Sales Order yet (Decide After
	Loading). This one only ever fires on a trip planned from a Sales Order
	in the first place — its header `custom_sales_order` and every row's
	`sales_order` / `so_detail` / `rate` were set back when the trip was
	created, and are correct already. Re-running that allocation here would
	claim the order's pending qty a second time for stock the trip already
	owns.

	Not a substitute for `_validate_po_pending_qty`, which this path cannot
	use — these rows carry no `po_detail` (only `make_purchase_order`'s
	`mode="direct"` stamps that). The qty check below is this path's own,
	looser version of the same guard: a warning, not a hard block, since a
	trip legitimately split across more than one receipt is not an error.
	"""
	warehouses = {row.warehouse for row in doc.items if row.warehouse}
	if not warehouses:
		return

	vehicle_warehouses = set(
		frappe.get_all(
			"Warehouse",
			filters={"name": ("in", list(warehouses)), "custom_is_vehicle_warehouse": 1},
			pluck="name",
		)
	)
	vehicle_rows = [row for row in doc.items if row.warehouse in vehicle_warehouses]
	if not vehicle_rows:
		_link_ordinary_receipt_to_waiting_trip(doc)
		return

	# The PO is traced from the vehicle-warehouse rows specifically, not the
	# whole receipt — a receipt can mix a vehicle-warehouse row with an
	# ordinary yard row for an unrelated PO, and that unrelated PO must never
	# decide which trip this one links to.
	po_names = {row.purchase_order for row in vehicle_rows if row.get("purchase_order")}
	if len(po_names) != 1:
		if po_names:
			frappe.msgprint(
				_("This receipt's vehicle-warehouse rows span more than one Purchase Order, so no "
				  "Delivery Trip could be linked automatically. Link it by hand on the trip if one applies."),
				indicator="orange",
				title=_("Not Linked"),
			)
		return

	po_name = po_names.pop()
	po_supplier = frappe.db.get_value("Purchase Order", po_name, "supplier")

	# The vehicle a row lands on picks the trip directly, not
	# `Purchase Order.custom_delivery_trip` — that field is one Link, no
	# good once a PO is split across several pickup trips (one per truck).
	# One receipt can legitimately cover several of those trucks at once —
	# dispatch does not want to raise a separate Purchase Receipt per
	# vehicle just because the goods happened to load onto more than one —
	# so this groups the receipt's own rows by warehouse and resolves (and
	# advances) each truck's trip independently. A vehicle can only ever be
	# on one open trip at a time (see `vehicles_booked_on_other_trips`), so
	# each group resolves correctly no matter how many sibling trips share
	# the PO.
	rows_by_warehouse = {}
	for row in vehicle_rows:
		rows_by_warehouse.setdefault(row.warehouse, []).append(row)

	from thameen_erp.overrides.delivery_trip import PICKUP_ROUTE

	linked_pickup_trips = []
	last_linked_trip = None

	for vehicle_warehouse, rows in rows_by_warehouse.items():
		vehicle_name = frappe.db.get_value("Vehicle", {"custom_vehicle_warehouse": vehicle_warehouse}, "name")
		if not vehicle_name:
			continue

		trip_name = frappe.db.get_value(
			"Delivery Trip",
			{"vehicle": vehicle_name, "custom_purchase_order": po_name, "docstatus": 1},
			"name",
			order_by="creation desc",
		)
		if not trip_name:
			continue

		trip = frappe.get_doc("Delivery Trip", trip_name)

		# A pickup trip (empty truck sent to collect this PO) is not a
		# shortfall being redirected — it is exactly what it always was,
		# just now loaded. None of the Direct-from-Supplier flipping below
		# applies: route, supply source and destination all stay Warehouse
		# to Supplier. Its own status machine (see PICKUP_ROUTE) takes it
		# from here — "Trip Reached and Loaded" is the one status this
		# whole app sets automatically rather than from a button, precisely
		# because this is the moment it becomes true.
		if trip.custom_trip_route == PICKUP_ROUTE:
			if trip.docstatus == 1 and trip.status == "Trip Started":
				frappe.db.set_value(
					"Delivery Trip", trip_name,
					{"custom_purchase_receipt": doc.name, "status": "Trip Reached and Loaded"},
					update_modified=False,
				)
				_auto_create_follow_on_trip(trip_name, doc.name)
			linked_pickup_trips.append(trip_name)
			last_linked_trip = trip_name
			continue

		# Already Direct-from-Supplier — this receipt is the one
		# `load_vehicle` / `receive_onto_vehicle` itself just raised and
		# submitted for that trip, not a new shortfall being redirected.
		# Nothing left to flip; let that function's own message stand alone
		# instead of printing a second one.
		if trip.custom_supply_source == DIRECT:
			continue

		if trip.custom_purchase_receipt and trip.custom_purchase_receipt != doc.name:
			frappe.msgprint(
				_("{0} is already linked to Purchase Receipt {1}. {2} was not linked over it — check "
				  "both receipts cover this trip correctly.").format(
					get_link_to_form("Delivery Trip", trip_name), trip.custom_purchase_receipt, doc.name
				),
				indicator="orange",
				title=_("Already Linked"),
			)
			continue

		# Set here, not left for the trip's own `_fill_supplier_from_po_or_receipt`
		# to catch on its next save: that only runs inside validate(), and this
		# write bypasses it entirely, same as every other field set above. Left
		# to validate(), the Supplier field would sit blank — and any dialog that
		# offers to raise a second Purchase Order for a further shortfall would
		# have nothing to default it to — until someone happens to save the trip.
		trip_updates = {
			"custom_purchase_receipt": doc.name,
			"custom_supply_source": DIRECT,
			"custom_destination_type": "Customer",
			"custom_trip_route": "Supplier to Customer",
		}
		# `_validate_supply_source` refuses to submit a Direct-from-Supplier trip
		# with no `custom_purchase_order` — "the PO IS its stock" — and this is
		# the one place that flips a trip to that supply source without ever
		# having called `make_purchase_order` (which is what normally sets it).
		# Without this, the trip has a real PO and its receipt already in hand,
		# and still gets told it needs one.
		if not trip.custom_purchase_order:
			trip_updates["custom_purchase_order"] = po_name
		if not trip.custom_supplier:
			trip_updates["custom_supplier"] = po_supplier
		if not trip.custom_supplier_warehouse:
			supplier_warehouse = frappe.db.get_value("Supplier", po_supplier, "custom_default_warehouse")
			if supplier_warehouse:
				trip_updates["custom_supplier_warehouse"] = supplier_warehouse

		frappe.db.set_value("Delivery Trip", trip_name, trip_updates, update_modified=False)
		last_linked_trip = trip_name

		# This path skips `_validate_po_pending_qty` (no `po_detail` to check) and,
		# once the trip is Direct-from-Supplier, `_validate_stock_available` too
		# (that check exempts this supply source outright) — so nothing else will
		# ever tell the dispatcher if this receipt came up short. A plain qty
		# comparison, by item, is the only guard left standing. Scoped to THIS
		# warehouse's own rows only — another truck's cement in the same
		# receipt is not this trip's stock.
		received = {}
		for row in rows:
			received[row.item_code] = flt(received.get(row.item_code)) + flt(row.qty) * (flt(row.conversion_factor) or 1)

		short = []
		for row in trip.get("custom_trip_items") or []:
			needed = flt(row.qty) * (flt(row.conversion_factor) or 1)
			have = flt(received.get(row.item_code))
			if needed > have + QTY_TOLERANCE:
				short.append(_("{0}: trip needs {1}, this receipt brought {2}").format(row.item_code, needed, have))

		message = _("Stock updated on {0}. Linked to {1} — Trip Route set to Supplier to Customer.").format(
			vehicle_warehouse, get_link_to_form("Delivery Trip", trip_name)
		)
		if short:
			message += "<br><br>" + _("This receipt does not cover the whole trip:") + "<br>" + "<br>".join(short)

		frappe.msgprint(message, indicator="orange" if short else "green", title=_("Stock Updated"))

	# The PO→PR doc-mapping copies `custom_delivery_trip` straight off the
	# Purchase Order — fine while a PO has one trip, wrong the moment it has
	# several. Overwrite it with whichever trip this receipt actually
	# resolved to last, purely so the receipt's own "View" button and status
	# checks have somewhere correct to point — same "informational only"
	# convention `create_pickup_trips` already uses on the PO itself.
	if last_linked_trip:
		frappe.db.set_value("Purchase Receipt", doc.name, "custom_delivery_trip", last_linked_trip, update_modified=False)

	if len(linked_pickup_trips) > 1:
		frappe.msgprint(
			_("This receipt loaded {0} trucks at once: {1}.").format(
				len(linked_pickup_trips),
				", ".join(get_link_to_form("Delivery Trip", t) for t in linked_pickup_trips),
			),
			indicator="green",
			alert=True,
		)


def purchase_receipt_on_cancel(doc, method=None):
	"""The receipt that put the cement on the truck is gone — so is the link."""
	trip = doc.get("custom_delivery_trip")
	if not trip:
		return
	rows = frappe.get_all("Delivery Trip Item", filters={"purchase_receipt": doc.name}, pluck="name")
	for name in rows:
		frappe.db.set_value(
			"Delivery Trip Item", name,
			{"purchase_receipt": None, "purchase_receipt_item": None},
			update_modified=False,
		)
	if frappe.db.get_value("Delivery Trip", trip, "custom_purchase_receipt") == doc.name:
		frappe.db.set_value("Delivery Trip", trip, "custom_purchase_receipt", None, update_modified=False)

	status = frappe.db.get_value("Delivery Trip", trip, "status")
	if status in ("Loading", "In Transit"):
		frappe.msgprint(
			_("Trip {0} is {1} but its Purchase Receipt was cancelled — the truck no longer holds this stock. "
			  "Cancel and amend the trip, or receive again.").format(get_link_to_form("Delivery Trip", trip), status),
			indicator="orange",
		)


def purchase_order_on_cancel(doc, method=None):
	trip = doc.get("custom_delivery_trip")
	if not trip:
		return
	for name in frappe.get_all("Delivery Trip Item", filters={"purchase_order": doc.name}, pluck="name"):
		frappe.db.set_value(
			"Delivery Trip Item", name, {"purchase_order": None, "po_detail": None}, update_modified=False
		)
	if frappe.db.get_value("Delivery Trip", trip, "custom_purchase_order") == doc.name:
		frappe.db.set_value("Delivery Trip", trip, "custom_purchase_order", None, update_modified=False)


# ---------------------------------------------------------------------------
# One item per trip
# ---------------------------------------------------------------------------


def one_item_per_trip():
	return bool(frappe.db.get_single_value("Thameen Fleet Settings", "one_item_per_trip"))


@frappe.whitelist()
def split_trip_by_item(trip):
	"""Keep the first item on this trip; every other item gets its own draft.

	A bulk tanker carries one cement type. Mixed rows pulled with Get Items
	land here rather than being refused outright.
	"""
	frappe.has_permission("Delivery Trip", "write", doc=trip, throw=True)
	doc = frappe.get_doc("Delivery Trip", trip)
	if doc.docstatus != 0:
		frappe.throw(_("Only a draft trip can be split."))

	from thameen_erp.overrides.vehicle_load import _row_values, _trip_header

	groups = {}
	for row in doc.get("custom_trip_items") or []:
		groups.setdefault(row.item_code, []).append(row)

	if len(groups) <= 1:
		frappe.throw(_("This trip carries a single item — nothing to split."))

	items = list(groups)
	template = _trip_header(doc)
	# A per-trip PO cannot be shared — each new trip raises its own.
	for key in ("custom_purchase_order",):
		template.pop(key, None)

	doc.set("custom_trip_items", [])
	for row in groups[items[0]]:
		doc.append("custom_trip_items", _row_values(row))
	doc.flags.thameen_splitting = True
	doc.save()

	created = []
	for item_code in items[1:]:
		new_trip = frappe.new_doc("Delivery Trip")
		new_trip.update(template)
		for row in groups[item_code]:
			values = _row_values(row)
			values.pop("purchase_order", None)
			values.pop("po_detail", None)
			new_trip.append("custom_trip_items", values)
		new_trip.insert()
		created.append(new_trip.name)

	frappe.msgprint(
		_("{0} keeps {1}. New trip(s): {2}").format(
			get_link_to_form("Delivery Trip", doc.name),
			items[0],
			", ".join(
				f"{get_link_to_form('Delivery Trip', name)} ({item})"
				for name, item in zip(created, items[1:])
			),
		),
		indicator="green",
		title=_("Split by Item"),
	)
	return created


@frappe.whitelist()
def linked_purchase_invoice(purchase_receipt=None, purchase_order=None):
	"""The Purchase Invoice raised against a receipt or order, if any —
	nothing on the trip points at one directly, so this is a lookup rather
	than a stored field.

	Done server-side with `ignore_permissions`, not a plain
	`frappe.db.get_list` from the browser: "Purchase Invoice Item" is a
	child table, and querying one directly over the client API needs the
	parent doctype in context to check permissions at all — without it,
	Frappe refuses with "X is not a valid parent DocType for X" before this
	ever gets to look at the data. Read access is still checked, just
	against Purchase Invoice itself, once, here.
	"""
	if not (purchase_receipt or purchase_order):
		return None
	if not frappe.has_permission("Purchase Invoice", "read"):
		return None

	filters = {"docstatus": 1}
	if purchase_receipt:
		filters["purchase_receipt"] = purchase_receipt
	else:
		filters["purchase_order"] = purchase_order

	rows = frappe.get_all(
		"Purchase Invoice Item", filters=filters, fields=["parent"], limit_page_length=1, ignore_permissions=True
	)
	return rows[0].parent if rows else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bin_qty(item_code, warehouse):
	if not (item_code and warehouse):
		return 0.0
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))


def _vehicle_warehouse_or_loading(doc):
	"""PO warehouse for a trip's shortfall or direct-supply purchase alike —
	the truck's own warehouse when one is assigned, so the Purchase Receipt
	the buyer makes from it defaults straight onto the vehicle. Falls back to
	the loading warehouse (or a generic yard) only when no vehicle is picked
	yet, in which case the buyer sets the real warehouse by hand later."""
	if doc.get("vehicle"):
		wh = frappe.db.get_value("Vehicle", doc.vehicle, "custom_vehicle_warehouse")
		if wh:
			return wh
	return doc.get("custom_loading_warehouse") or frappe.db.get_value(
		"Warehouse", {"company": doc.company, "is_group": 0, "custom_is_vehicle_warehouse": 0}, "name"
	)


# ---------------------------------------------------------------------------
# A fourth path: send an empty truck to collect a Purchase Order in person
# ---------------------------------------------------------------------------
#
#   Purchase Order submitted -> create_pickup_trip -> Draft pickup trip
#   Pickup trip submitted    -> "Trip Started" (see update_status)
#   Its Purchase Receipt submitted, into the truck's own warehouse
#                             -> "Trip Reached and Loaded" (see
#                                link_shortfall_receipt_to_trip)
#   create_trip_after_pickup -> a second, ordinary Delivery Trip, already
#                                loaded, Direct from Supplier / Decide After
#                                Loading — the existing Deliver to Customer /
#                                Deliver to Own Warehouse buttons take it from
#                                there
#   Pickup trip, once its own driver confirms arrival back -> "Trip Reached"
#                                (see _advance_pickup_trip), POD-gated same
#                                as any other trip's close-out
#
# Two separate Delivery Trip records for one physical round trip, on
# purpose: the pickup leg carries nothing and reports on nothing a cargo
# trip does (no items, no capacity, no stock check), so folding both into
# one record would mean every rule in this app second-guessing which half
# of the journey it is looking at.
# ---------------------------------------------------------------------------


@frappe.whitelist()
def create_pickup_trips(purchase_order, plan):
	"""One pickup trip per vehicle — an empty truck each, sent to collect a
	share of what a just-submitted Purchase Order bought. A PO too big for
	one truck's capacity is exactly why `plan` is a list, not a single
	vehicle: [{vehicle, qty, driver?, departure_time?}, ...], `qty` in
	stock units, cut across the PO's own item rows in order (the same
	cut-and-carry a cargo trip's Split Into Trips uses, just driven by what
	the dispatcher typed here rather than each truck's rated capacity).

	Each trip's item rows carry `po_detail` — the same per-ROW link
	`make_purchase_order`'s "direct" mode already stamps on a cargo trip —
	so `link_shortfall_receipt_to_trip` can find the RIGHT trip once
	several share the one PO: by then it resolves through the vehicle a
	receipt actually lands on, never through this PO's own
	`custom_delivery_trip` (one Link, no good for more than one trip at
	once — kept updated anyway, purely so the PO's own "View" button has
	somewhere to point).

	No stock check on submit — the PO itself is what backs these trips,
	and nothing is actually loaded until each one's own Purchase Receipt is
	submitted, which moves that one trip on to "Trip Reached and Loaded"
	on its own (see `link_shortfall_receipt_to_trip`).
	"""
	frappe.has_permission("Delivery Trip", "create", throw=True)

	if isinstance(plan, str):
		plan = json.loads(plan or "[]")
	plan = [p for p in (plan or []) if p.get("vehicle") and flt(p.get("qty")) > 0]
	if not plan:
		frappe.throw(_("Add at least one vehicle with a quantity."))

	po = frappe.get_doc("Purchase Order", purchase_order)
	if po.docstatus != 1:
		frappe.throw(_("Submit the Purchase Order first."))

	claimed = _pickup_claimed_by_po_detail(po.name)
	pool = []
	for row in po.items:
		left = max(flt(row.stock_qty) - flt(claimed.get(row.name)), 0.0)
		if left > QTY_TOLERANCE:
			pool.append(
				{
					"po_detail": row.name,
					"item_code": row.item_code,
					"item_name": row.item_name,
					"uom": row.uom,
					"stock_uom": row.stock_uom,
					"conversion_factor": flt(row.conversion_factor) or 1,
					"left": left,
				}
			)

	total_requested = sum(flt(p["qty"]) for p in plan)
	total_pending = sum(entry["left"] for entry in pool)
	if total_requested > total_pending + QTY_TOLERANCE:
		frappe.throw(
			_("The plan sends {0}, but only {1} of this PO is not already claimed by another "
			  "pickup trip.").format(flt(total_requested, 2), flt(total_pending, 2))
		)

	from thameen_erp.overrides.delivery_trip import PICKUP_ROUTE

	created = []
	for p in plan:
		vehicle = p["vehicle"]
		vehicle_warehouse = frappe.db.get_value("Vehicle", vehicle, "custom_vehicle_warehouse")
		if not vehicle_warehouse:
			frappe.throw(_("{0} has no vehicle warehouse set up — it cannot be sent anywhere.").format(vehicle))
		if frappe.db.exists("Bin", {"warehouse": vehicle_warehouse, "actual_qty": (">", 0)}):
			frappe.throw(_("{0} is not empty — a pickup trip needs an empty truck.").format(vehicle))

		need = flt(p["qty"])
		rows_for_trip = []
		for entry in pool:
			if need <= QTY_TOLERANCE:
				break
			if entry["left"] <= QTY_TOLERANCE:
				continue
			take = min(entry["left"], need)
			rows_for_trip.append({**entry, "qty_taken": take})
			entry["left"] -= take
			need -= take

		trip = frappe.new_doc("Delivery Trip")
		trip.company = po.company
		trip.custom_supply_source = OWN
		trip.custom_destination_type = "Supplier"
		trip.custom_trip_route = PICKUP_ROUTE
		trip.custom_supplier = po.supplier
		trip.custom_purchase_order = po.name
		trip.vehicle = vehicle
		if p.get("driver"):
			trip.driver = p["driver"]
		trip.departure_time = p.get("departure_time") or now_datetime()
		trip.flags.ignore_mandatory = True

		for r in rows_for_trip:
			trip.append(
				"custom_trip_items",
				{
					"item_code": r["item_code"],
					"item_name": r["item_name"],
					"qty": flt(r["qty_taken"]) / (flt(r["conversion_factor"]) or 1),
					"uom": r["uom"],
					"stock_uom": r["stock_uom"],
					"conversion_factor": r["conversion_factor"],
					"purchase_order": po.name,
					"po_detail": r["po_detail"],
				},
			)

		trip.insert()
		created.append(trip.name)

	# Informational only now — `link_shortfall_receipt_to_trip` resolves
	# each receipt through the vehicle it lands on, not through this
	# field, so it is safe to just point at whichever trip was created
	# last; the PO's own "View" button needs somewhere to go.
	frappe.db.set_value("Purchase Order", po.name, "custom_delivery_trip", created[-1], update_modified=False)

	frappe.msgprint(
		_("{0} pickup trip(s) created: {1}.").format(
			len(created), ", ".join(get_link_to_form("Delivery Trip", t) for t in created)
		),
		indicator="green",
		alert=True,
	)
	return created


def _pickup_claimed_by_po_detail(po_name):
	"""{po_detail: qty} already claimed by an open (not cancelled) pickup
	trip against this PO — so a second batch of vehicles, sent later for
	the same PO, does not re-claim quantity a first batch already owns."""
	rows = frappe.db.sql(
		"""
		select i.po_detail, sum(ifnull(i.qty, 0) * ifnull(nullif(i.conversion_factor, 0), 1)) as qty
		from `tabDelivery Trip Item` i
		inner join `tabDelivery Trip` t on t.name = i.parent
		where i.purchase_order = %(po)s and t.docstatus < 2 and i.po_detail is not null
		group by i.po_detail
		""",
		{"po": po_name},
		as_dict=True,
	)
	return {r.po_detail: flt(r.qty) for r in rows}


@frappe.whitelist()
def pending_pickup_qty(purchase_order):
	"""What of this PO no pickup trip has claimed yet, item by item, plus
	the empty vehicles and drivers on offer right now — everything the
	"Send Vehicle to Collect" dialog needs in one call, before
	`create_pickup_trips` re-checks the plan for real."""
	po = frappe.get_doc("Purchase Order", purchase_order)
	claimed = _pickup_claimed_by_po_detail(po.name)

	lines = []
	for row in po.items:
		left = max(flt(row.stock_qty) - flt(claimed.get(row.name)), 0.0)
		lines.append(
			{
				"item_code": row.item_code,
				"item_name": row.item_name,
				"stock_uom": row.stock_uom,
				"ordered": flt(row.stock_qty),
				"pending": left,
			}
		)

	from thameen_erp.overrides.sales_order import _drivers_for_planning
	from thameen_erp.overrides.vehicle_load import list_vehicles_for_planning

	vehicles = [
		{"name": v.name, "capacity": flt(v.capacity)}
		for v in list_vehicles_for_planning()
		if v.is_empty
	]
	drivers = [{"name": d.name, "full_name": d.full_name} for d in _drivers_for_planning()]

	return {
		"lines": lines,
		"total_pending": sum(l["pending"] for l in lines),
		"vehicles": vehicles,
		"drivers": drivers,
	}


@frappe.whitelist()
def pending_second_leg_trips(purchase_receipt):
	"""Every pickup trip THIS receipt settled that has reached "Trip Reached
	and Loaded" but has no follow-on (delivery-leg) trip yet — one receipt
	can cover several trucks at once (see Pickup Vehicles), so this is a
	list, not a single trip the way `Purchase Receipt.custom_delivery_trip`
	(one Link) can only ever point at one of them."""
	from thameen_erp.overrides.delivery_trip import PICKUP_ROUTE

	rows = frappe.get_all(
		"Delivery Trip",
		filters={
			"custom_purchase_receipt": purchase_receipt,
			"custom_trip_route": PICKUP_ROUTE,
			"status": "Trip Reached and Loaded",
			"custom_follow_on_trip": ("in", ("", None)),
			"docstatus": 1,
		},
		fields=["name", "vehicle", "driver"],
	)
	return rows


def _auto_create_follow_on_trip(pickup_trip, purchase_receipt):
	"""Called the instant a pickup trip reaches "Trip Reached and Loaded" —
	nothing about the follow-on trip is a choice (vehicle, driver and items
	all come straight off the truck this receipt just loaded), so there is
	nothing to ask the dispatcher to confirm in a dialog first. Any failure
	here (truck genuinely empty, already has one, ...) is swallowed and
	logged rather than allowed to fail the receipt's own submit — the
	"Create Delivery Trip" button on the receipt still covers it by hand as
	a fallback.
	"""
	try:
		create_trip_after_pickup(purchase_receipt, pickup_trip=pickup_trip)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"Thameen ERP: auto follow-on trip failed for pickup trip {pickup_trip}",
		)
		frappe.msgprint(
			_("Could not automatically create the delivery trip for {0} — use the \"Create Delivery Trip\" "
			  "button on this receipt once you have checked why.").format(
				get_link_to_form("Delivery Trip", pickup_trip)
			),
			indicator="orange",
			title=_("Follow-on Trip Not Created"),
		)


@frappe.whitelist()
def create_trip_after_pickup(purchase_receipt, pickup_trip=None):
	"""The second leg. The truck is already loaded — this receipt is what
	just put its Purchase Order's goods onto it — so nothing here is asked
	for by hand: vehicle, driver and item rows all come straight off what
	is physically on the truck right now. Left Direct from Supplier /
	Decide After Loading, same as any other trip collected at a supplier's
	plant with nowhere fixed to go yet — the existing Deliver to Customer /
	Deliver to Own Warehouse buttons apply unchanged from here.

	`pickup_trip` names WHICH pickup trip this receipt covers — required
	once a receipt can settle several at once (see Pickup Vehicles); left
	blank, this falls back to the receipt's own `custom_delivery_trip`
	(whichever trip that single Link happens to point at) for any older
	caller that still only ever deals with one.
	"""
	frappe.has_permission("Delivery Trip", "create", throw=True)

	from thameen_erp.overrides.delivery_trip import PICKUP_ROUTE

	pr = frappe.get_doc("Purchase Receipt", purchase_receipt)
	pickup_name = pickup_trip or pr.get("custom_delivery_trip")
	if not pickup_name:
		frappe.throw(_("This receipt is not linked to a pickup trip."))

	pickup = frappe.get_doc("Delivery Trip", pickup_name)
	if pickup.custom_trip_route != PICKUP_ROUTE:
		frappe.throw(_("{0} is not a pickup trip.").format(pickup_name))
	if pickup.custom_purchase_receipt != pr.name:
		frappe.throw(_("{0} was not settled by this receipt.").format(pickup_name))
	if pickup.custom_follow_on_trip:
		frappe.throw(
			_("{0} already has a follow-on trip: {1}.").format(
				pickup_name, get_link_to_form("Delivery Trip", pickup.custom_follow_on_trip)
			)
		)

	from thameen_erp.overrides.vehicle_stock import get_truck_stock_summary

	truck = get_truck_stock_summary(pickup.vehicle)
	on_truck = [row for row in (truck.get("items") or []) if flt(row.get("free")) > 0]
	if not on_truck:
		frappe.throw(_("{0} has nothing on it yet.").format(pickup.vehicle))

	trip = frappe.new_doc("Delivery Trip")
	trip.company = pickup.company
	trip.custom_supply_source = DIRECT
	trip.custom_destination_type = "Decide After Loading"
	trip.custom_supplier = pickup.custom_supplier
	trip.custom_purchase_order = pickup.custom_purchase_order
	trip.custom_purchase_receipt = pr.name
	trip.vehicle = pickup.vehicle
	trip.driver = pickup.driver
	trip.flags.ignore_mandatory = True

	for row in on_truck:
		trip.append(
			"custom_trip_items",
			{
				"item_code": row["item_code"],
				"item_name": row.get("item_name") or row["item_code"],
				"qty": flt(row["free"]),
				"uom": row.get("stock_uom"),
				"stock_uom": row.get("stock_uom"),
				"conversion_factor": 1,
				"source_warehouse": truck.get("warehouse"),
			},
		)

	trip.insert()
	frappe.db.set_value("Delivery Trip", pickup_name, "custom_follow_on_trip", trip.name, update_modified=False)

	frappe.msgprint(
		_("Delivery Trip {0} created (Draft) — {1} is already loaded and at the supplier's location with "
		  "the remaining quantity. Choose Deliver to Customer or Deliver to Own Warehouse on it once you "
		  "know where it is going, then submit.").format(
			get_link_to_form("Delivery Trip", trip.name), pickup.vehicle
		),
		indicator="green",
		title=_("Delivery Trip Created"),
	)
	return trip.name
