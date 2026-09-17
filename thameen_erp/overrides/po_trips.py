"""Delivery Trips planned from a Purchase Receipt.

Cement bought from a supplier is received into an ordinary warehouse first —
a ordinary Purchase Receipt, same as any other purchase — so the buy is on
the books (stock and accounts) the moment it actually arrives, not whenever a
truck eventually happens to load it. What this module plans is the second,
separate leg: trucking that already-received cement on to the customer it was
bought for.

That makes it an "Own Warehouse" supply-source trip like any other — sourced
from wherever the receipt put the stock — not "Direct from Supplier" (that
variant receives straight onto the truck at Loading, which would double the
receipt this module's whole point is to record early). A Direct-from-Supplier
trip is still set up by hand on the Delivery Trip form (Trip Route: Supplier
to Customer / Supplier to Decide After Loading) — there is no longer an
automatic shortcut from a stock shortfall onto that route.

Pending qty is scoped to ONE receipt, not the whole Purchase Order: received
minus whatever open trips already carry `custom_purchase_receipt` = this
receipt. A Purchase Order received in two batches gets two independent pools
to plan against, matching the stock each batch actually put in the warehouse.
"""

import json

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, get_link_to_form

from thameen_erp.overrides.vehicle_stock import QTY_TOLERANCE

OPEN_TRIP_STATES = ("Draft", "Scheduled", "Loading", "In Transit")


# ---------------------------------------------------------------------------
# What is left to plan
# ---------------------------------------------------------------------------


def pending_receipt_lines(pr):
	"""Per Purchase Receipt line: received − already on open trips linked to
	THIS receipt (stock UOM). Two rows of the same item on one receipt share
	one pool, since nothing on the receipt itself tells them apart once
	they're in the warehouse."""
	on_trips = {}
	for row in frappe.db.sql(
		"""
		select i.item_code,
		       sum(ifnull(i.qty, 0) * ifnull(nullif(i.conversion_factor, 0), 1)) as qty
		from `tabDelivery Trip Item` i
		inner join `tabDelivery Trip` t on t.name = i.parent
		where t.custom_purchase_receipt = %(pr)s and t.docstatus < 2
		  and (t.docstatus = 0 or t.status in %(states)s)
		group by i.item_code
		""",
		{"pr": pr.name, "states": OPEN_TRIP_STATES},
		as_dict=True,
	):
		on_trips[row.item_code] = flt(row.qty)

	totals = {}
	for item in pr.items:
		factor = flt(item.conversion_factor) or 1
		received = flt(item.qty) * factor
		entry = totals.setdefault(
			item.item_code,
			{
				"item_code": item.item_code,
				"item_name": item.item_name,
				"uom": item.uom,
				"stock_uom": item.stock_uom,
				"conversion_factor": factor,
				"rate": flt(item.rate),
				"warehouse": item.warehouse,
				"received": 0.0,
			},
		)
		entry["received"] += received

	lines = []
	for item_code, entry in totals.items():
		planned = flt(on_trips.get(item_code))
		pending = max(entry["received"] - planned, 0.0)
		entry["on_trips"] = planned
		entry["pending"] = pending if pending > QTY_TOLERANCE else 0.0
		lines.append(entry)
	return lines


def _traced_trip(pr_name):
	"""If this receipt exists because a trip came up short — bought through
	'Create Purchase Order for Shortfall' (direct) or 'Material Request
	instead' (which a buyer later turned into a Purchase Order by hand) —
	trace back to that trip: Purchase Receipt row -> its Purchase Order ->
	`custom_delivery_trip`.

	The Material Request path relies on nothing more than Frappe's own
	default doc-mapping: `custom_delivery_trip` exists on Material Request,
	Purchase Order and Purchase Receipt alike, under the same fieldname and
	none of them `no_copy`, so ERPNext's standard "Create Purchase Order"
	(from the Material Request) and "Create Purchase Receipt" (from that
	Purchase Order) carry it forward with no extra code on our side.

	None when the receipt's Purchase Order(s) do not all trace to the SAME
	trip, or none of them trace to a trip at all — an ordinary restock
	purchase, not yet sold to anyone in particular. Two Purchase Orders both
	pointing at the same trip (a shortfall split across two POs, or a second
	one raised later for the same short trip) is not ambiguous and still
	resolves — only a genuine split across different trips, or a mix of
	trip and non-trip POs, is.

	Takes the receipt's name, not a loaded doc — every caller only ever
	needs the item rows' own `purchase_order` column, so a full `get_doc`
	(every child table, the whole controller/hook path) would just be
	wasted work here.
	"""
	po_names = list(
		frappe.get_all(
			"Purchase Receipt Item",
			filters={"parent": pr_name, "purchase_order": ("is", "set")},
			pluck="purchase_order",
		)
	)
	if not po_names:
		return None

	trips = set(
		frappe.get_all("Purchase Order", filters={"name": ("in", po_names)}, pluck="custom_delivery_trip")
	)
	trips.discard(None)
	trips.discard("")
	if len(trips) != 1:
		return None
	return trips.pop()


def _traced_sales_order(pr_name):
	"""Same trace as `_traced_trip`, one hop further to that trip's own
	`custom_sales_order`."""
	trip = _traced_trip(pr_name)
	return frappe.db.get_value("Delivery Trip", trip, "custom_sales_order") if trip else None


@frappe.whitelist()
def waiting_trip_for_receipt(purchase_receipt):
	"""The Delivery Trip this receipt's stock was bought for, if it is still
	sitting there waiting — Draft, no vehicle yet. Once a vehicle is set (or
	the trip has moved past Draft), it is no longer "waiting" and this
	returns None; a `Purchase Receipt` button offering to jump to it would
	otherwise dangle after the very next save.
	"""
	frappe.has_permission("Purchase Receipt", "read", doc=purchase_receipt, throw=True)

	trip = _traced_trip(purchase_receipt)
	if not trip:
		return None

	# `custom_delivery_trip` on the Purchase Order is a plain Link, not
	# enforced by anything — the trip it names could have been deleted or
	# renamed since. get_value on a name that no longer exists returns None,
	# not a tuple of Nones, so this must not unpack it blindly.
	row = frappe.db.get_value("Delivery Trip", trip, ["docstatus", "vehicle"])
	if not row:
		return None
	docstatus, vehicle = row
	if docstatus == 0 and not vehicle and frappe.has_permission("Delivery Trip", "read", doc=trip):
		return trip
	return None


@frappe.whitelist()
def preview_receipt_trips(purchase_receipt):
	pr = frappe.get_doc("Purchase Receipt", purchase_receipt)
	if pr.docstatus != 1:
		frappe.throw(_("Submit the Purchase Receipt before planning trips for it."))

	from thameen_erp.overrides.sales_order import _drivers_for_planning
	from thameen_erp.overrides.vehicle_load import list_vehicles_for_planning

	lines = pending_receipt_lines(pr)

	return {
		"purchase_receipt": pr.name,
		"supplier": pr.supplier,
		"company": pr.company,
		"lines": lines,
		"sales_order": _traced_sales_order(pr.name),
		"vehicles": list_vehicles_for_planning(
			items=[row.get("item_code") for row in lines if row.get("item_code")]
		),
		"drivers": _drivers_for_planning(),
	}


# ---------------------------------------------------------------------------
# Creating the trips
# ---------------------------------------------------------------------------


@frappe.whitelist()
def create_trips_from_receipt(purchase_receipt, sales_order=None, plan=None, delivery_location=None, transportation_charge=None):
	"""One draft Delivery Trip per plan row, sourced from an already-received
	Purchase Receipt.

	Always "Own Warehouse" supply source: the cement is already sitting in
	whichever warehouse the receipt put it in, so Loading is a plain Material
	Transfer — the same mechanism a Sales-Order-planned trip already uses —
	not a second Purchase Receipt.

	`sales_order` is optional. Given, the rows are matched against that
	order's own pending lines (their rate, not the receipt's), the same way
	the Sales Order planner itself works. Left blank — dispatch does not yet
	know who this is going to — the trip is still created, in Draft, priced
	off the receipt; the Sales Order can be set later directly on the trip,
	before it is submitted.

	plan = [{item_code, qty (stock UOM), vehicle?, driver?, departure_time?}]
	"""
	frappe.has_permission("Delivery Trip", "create", throw=True)

	pr = frappe.get_doc("Purchase Receipt", purchase_receipt)
	if pr.docstatus != 1:
		frappe.throw(_("Submit the Purchase Receipt first."))

	if isinstance(plan, str):
		plan = json.loads(plan or "[]")
	plan = [p for p in (plan or []) if flt(p.get("qty")) > 0]
	if not plan:
		frappe.throw(_("Nothing to plan — every row is zero."))

	if sales_order:
		so_pool = _sales_order_pool(sales_order)
		delivery_location = delivery_location or frappe.db.get_value(
			"Sales Order", sales_order, "custom_delivery_location"
		)
		customer = frappe.db.get_value("Sales Order", sales_order, "customer")
	else:
		so_pool = {}
		customer = None

	# Never place more than this receipt actually put in the warehouse.
	pending = {line["item_code"]: line for line in pending_receipt_lines(pr)}
	placed = {}
	for p in plan:
		placed[p["item_code"]] = flt(placed.get(p["item_code"])) + flt(p["qty"])
	for item_code, qty in placed.items():
		line = pending.get(item_code)
		if not line:
			frappe.throw(_("{0} is not on this Purchase Receipt.").format(item_code))
		if qty > flt(line["pending"]) + QTY_TOLERANCE:
			frappe.throw(
				_("{0}: the plan sends {1} but only {2} of this receipt is still pending "
				  "(received {3}, already on trips {4}).").format(
					item_code, flt(qty, 2), flt(line["pending"], 2),
					flt(line["received"], 2), flt(line["on_trips"], 2),
				),
				title=_("Plan exceeds the receipt"),
			)

	created = []
	for p in plan:
		line = pending[p["item_code"]]
		factor = flt(line["conversion_factor"]) or 1

		trip = frappe.new_doc("Delivery Trip")
		trip.company = pr.company
		trip.departure_time = _departure(p.get("departure_time"), pr.posting_date)
		if sales_order:
			if trip.meta.has_field("sales_order"):
				trip.sales_order = sales_order
			if trip.meta.has_field("customer"):
				trip.customer = customer
			trip.custom_sales_order = sales_order
		else:
			trip.flags.ignore_mandatory = True
		trip.custom_trip_type = "Company Vehicle"
		trip.custom_supply_source = "Own Warehouse"
		trip.custom_purchase_receipt = pr.name
		trip.custom_destination_type = "Customer"
		trip.custom_delivery_location = delivery_location
		trip.custom_loading_warehouse = line["warehouse"]
		if transportation_charge is not None and flt(transportation_charge) > 0:
			trip.custom_transportation_cost = flt(transportation_charge)

		if p.get("vehicle"):
			trip.vehicle = p["vehicle"]
		if p.get("driver"):
			trip.driver = p["driver"]
		elif p.get("vehicle"):
			driver = frappe.db.get_value("Vehicle", p["vehicle"], "custom_assigned_driver")
			if driver:
				trip.driver = driver

		row = {
			"item_code": line["item_code"],
			"item_name": line["item_name"],
			"uom": line["uom"],
			"conversion_factor": factor,
			"stock_uom": line["stock_uom"],
			"source_warehouse": line["warehouse"],
			"delivery_location": delivery_location,
		}

		if sales_order:
			so_rows = _take_from_so_pool(so_pool, line["item_code"], flt(p["qty"]), sales_order)
			for so_row, take_stock in so_rows:
				piece = dict(row)
				piece.update(
					{
						"sales_order": sales_order,
						"so_detail": so_row["name"],
						"qty": take_stock / factor,
						"rate": flt(so_row["rate"]),
					}
				)
				trip.append("custom_trip_items", piece)
		else:
			# No order to match against yet — priced off the receipt itself,
			# same as the row would show on the receipt's own Items table.
			piece = dict(row)
			piece.update({"qty": flt(p["qty"]) / factor, "rate": flt(line["rate"])})
			trip.append("custom_trip_items", piece)

		trip.flags.thameen_splitting = True
		trip.insert()
		created.append(trip.name)

	frappe.msgprint(
		_("{0} trip(s) planned from {1}{2}: {3}").format(
			len(created),
			get_link_to_form("Purchase Receipt", pr.name),
			_(", direct to {0}").format(customer) if customer else "",
			", ".join(get_link_to_form("Delivery Trip", name) for name in created),
		),
		indicator="green",
		title=_("Delivery Trips Created"),
	)
	return created


# ---------------------------------------------------------------------------
# Sales Order matching for the customer destination
# ---------------------------------------------------------------------------


def _sales_order_pool(sales_order):
	"""Pending qty per Sales Order line, minus what open trips already hold."""
	so = frappe.get_doc("Sales Order", sales_order)
	if so.docstatus != 1:
		frappe.throw(_("Sales Order {0} is not submitted.").format(sales_order))

	on_trips = {}
	for row in frappe.db.sql(
		"""
		select i.so_detail,
		       sum(ifnull(i.qty, 0) * ifnull(nullif(i.conversion_factor, 0), 1)) as qty
		from `tabDelivery Trip Item` i
		inner join `tabDelivery Trip` t on t.name = i.parent
		where i.sales_order = %(so)s and t.docstatus < 2
		  and (t.docstatus = 0 or t.status in %(states)s)
		group by i.so_detail
		""",
		{"so": sales_order, "states": OPEN_TRIP_STATES},
		as_dict=True,
	):
		on_trips[row.so_detail] = flt(row.qty)

	pool = {}
	for item in so.items:
		factor = flt(item.conversion_factor) or 1
		left = (flt(item.qty) - flt(item.delivered_qty)) * factor - flt(on_trips.get(item.name))
		if left > QTY_TOLERANCE:
			pool.setdefault(item.item_code, []).append(
				{"name": item.name, "left": left, "rate": flt(item.rate)}
			)
	return pool


def _take_from_so_pool(pool, item_code, stock_qty, sales_order):
	entries = pool.get(item_code) or []
	available = sum(e["left"] for e in entries)
	if stock_qty > available + QTY_TOLERANCE:
		frappe.throw(
			_("Sales Order {0} only has {1} of {2} still to deliver, but the plan sends {3} to the customer.").format(
				sales_order, flt(available, 2), item_code, flt(stock_qty, 2)
			),
			title=_("More than the customer ordered"),
		)
	taken, need = [], stock_qty
	for entry in entries:
		if need <= QTY_TOLERANCE:
			break
		take = min(entry["left"], need)
		if take > QTY_TOLERANCE:
			taken.append((entry, take))
			entry["left"] -= take
			need -= take
	return taken


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def sales_orders_for_receipt(doctype, txt, searchfield, start, page_len, filters):
	"""Link-field query for the receipt planner's Sales Order field.

	Only submitted, not-fully-delivered orders carrying at least one item
	from THIS receipt — picking the wrong order is an easy mistake when the
	field offers every open Sales Order in the company instead.
	"""
	purchase_receipt = (filters or {}).get("purchase_receipt")
	items = frappe.get_all("Purchase Receipt Item", filters={"parent": purchase_receipt}, pluck="item_code")
	if not items:
		return []
	return frappe.db.sql(
		"""
		select distinct so.name, so.customer
		from `tabSales Order` so
		inner join `tabSales Order Item` soi on soi.parent = so.name
		where so.docstatus = 1 and so.status not in ('Closed', 'Completed', 'Cancelled')
		  and soi.item_code in %(items)s and soi.qty > soi.delivered_qty
		  and so.name like %(txt)s
		order by so.delivery_date
		limit %(page_len)s offset %(start)s
		""",
		{"items": tuple(set(items)), "txt": f"%{txt}%", "page_len": page_len, "start": start},
	)


def _departure(value, fallback):
	if value:
		try:
			return get_datetime(value)
		except Exception:
			pass
	return get_datetime(fallback) if fallback else None


# ---------------------------------------------------------------------------
# Deciding (or changing) the destination while the truck is on the road
# ---------------------------------------------------------------------------


REDIRECTABLE_STATES = ("Scheduled", "Loading", "In Transit")


@frappe.whitelist()
def redirect_trip(trip, destination, sales_order=None, target_warehouse=None, delivery_location=None,
                  transportation_charge=None):
	"""Point a submitted trip at a customer or at the yard, before Delivered.

	The everyday case: a direct-supply trip was created as Decide After
	Loading, the truck has collected at the plant, and dispatch now knows
	where it tips. Also usable to change a decision — a warehouse trip can
	become a customer trip and back, any time before Delivered, because until
	then nothing customer-facing has been written.

	Customer      needs a submitted Sales Order with enough pending qty of the
	              trip's items; rows are linked to its lines (that is where
	              the Delivery Note will come from).
	Own Warehouse needs a yard warehouse; any Sales Order links on the rows
	              are cleared so the order's pending qty is released.
	"""
	doc = frappe.get_doc("Delivery Trip", trip)
	frappe.has_permission("Delivery Trip", "write", doc=doc, throw=True)

	if doc.docstatus != 1:
		frappe.throw(_("Submit the trip first — a draft's destination is edited on the form."))
	if doc.status not in REDIRECTABLE_STATES:
		frappe.throw(
			_("The destination can only be changed before Delivered. This trip is {0}.").format(doc.status)
		)

	rows = [row for row in (doc.get("custom_trip_items") or []) if flt(row.qty) > 0]

	if destination == "Customer":
		if not sales_order:
			frappe.throw(_("Choose the customer's Sales Order."))
		pool = _sales_order_pool(sales_order)
		location = delivery_location or frappe.db.get_value("Sales Order", sales_order, "custom_delivery_location")

		# Allocate every row before writing anything — all or nothing.
		allocations = []
		for row in rows:
			stock_needed = flt(row.qty) * (flt(row.conversion_factor) or 1)
			allocations.append((row, _take_from_so_pool(pool, row.item_code, stock_needed, sales_order)))

		for row, pieces in allocations:
			if len(pieces) > 1:
				frappe.throw(
					_("Row {0} ({1}) spans {2} Sales Order lines. Split the trip row to match, or pick "
					  "an order whose lines cover it in one piece.").format(row.idx, row.item_code, len(pieces))
				)
			so_row, _take = pieces[0]
			frappe.db.set_value(
				"Delivery Trip Item",
				row.name,
				{
					"sales_order": sales_order,
					"so_detail": so_row["name"],
					"rate": flt(so_row["rate"]),
					"amount": flt(row.qty) * flt(so_row["rate"]),
					"delivery_location": location,
				},
				update_modified=False,
			)

		updates = {
			"custom_destination_type": "Customer",
			"custom_trip_route": "Supplier to Customer",
			"custom_trip_source": doc.get("custom_trip_source") or ("Purchase Order" if doc.get("custom_purchase_order") else "Manual"),
			"custom_sales_order": sales_order,
			"custom_target_warehouse": None,
			"custom_delivery_location": location,
		}
		if transportation_charge is not None and flt(transportation_charge) > 0:
			updates["custom_transportation_cost"] = flt(transportation_charge)
		doc.db_set(updates, update_modified=False)

		customer = frappe.db.get_value("Sales Order", sales_order, "customer")
		frappe.msgprint(
			_("{0} is now going to {1} ({2}). Delivered will raise the Delivery Note against {3}.").format(
				trip, customer, location or _("site"), get_link_to_form("Sales Order", sales_order)
			),
			indicator="green",
			title=_("Destination Set"),
		)

	elif destination == "Own Warehouse":
		if not target_warehouse:
			frappe.throw(_("Choose the warehouse the cement is going to."))
		if frappe.db.get_value("Warehouse", target_warehouse, "custom_is_vehicle_warehouse"):
			frappe.throw(_("{0} is a vehicle warehouse. Choose a yard warehouse.").format(target_warehouse))

		for row in rows:
			if row.sales_order:
				frappe.db.set_value(
					"Delivery Trip Item",
					row.name,
					{"sales_order": None, "so_detail": None, "delivery_location": target_warehouse},
					update_modified=False,
				)
			else:
				frappe.db.set_value(
					"Delivery Trip Item", row.name, "delivery_location", target_warehouse, update_modified=False
				)

		doc.db_set(
			{
				"custom_destination_type": "Own Warehouse",
				"custom_trip_route": "Supplier to Warehouse",
				"custom_target_warehouse": target_warehouse,
				"custom_sales_order": None,
				"custom_delivery_location": target_warehouse,
			},
			update_modified=False,
		)
		frappe.msgprint(
			_("{0} is now going to {1}. Delivered will unload the truck there — no Delivery Note, no POD.").format(
				trip, target_warehouse
			),
			indicator="green",
			title=_("Destination Set"),
		)
	else:
		frappe.throw(_("Destination must be Customer or Own Warehouse."))

	return destination
