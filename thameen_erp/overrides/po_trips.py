"""Delivery Trips planned from a Purchase Order.

A Purchase Order is cement we have bought. A truck has to move it somewhere,
and there are only two somewheres:

    Supplier → My Warehouse   inbound. Loading receives it onto the truck,
                              Delivered transfers it truck → yard. No customer,
                              no Delivery Note, no POD.
    Supplier → Customer       direct. Loading receives it onto the truck,
                              Delivered raises the Delivery Note from the truck
                              against the Sales Order. The yard never sees it.

Both are "Supply Source = Direct from Supplier" trips; what differs is
`custom_destination_type`. The yard → customer case is not planned from here —
that is the Sales Order's job and it already works.

The planner splits the order by the chosen truck's capacity: 100 bought, a
20-capacity truck → five trips, same truck, one day apart. Every quantity,
vehicle and date in that plan is editable before anything is created, and the
plan can never place more than the order still has pending.
"""

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_datetime, get_link_to_form, getdate, nowdate

from thameen_erp.overrides.vehicle_stock import QTY_TOLERANCE

OPEN_TRIP_STATES = ("Draft", "Scheduled", "Loading", "In Transit")


# ---------------------------------------------------------------------------
# What is left to plan
# ---------------------------------------------------------------------------


def pending_po_lines(po):
	"""Per PO line: ordered − received − already on open trips (stock UOM)."""
	on_trips = {}
	for row in frappe.db.sql(
		"""
		select i.po_detail,
		       sum(ifnull(i.qty, 0) * ifnull(nullif(i.conversion_factor, 0), 1)) as qty
		from `tabDelivery Trip Item` i
		inner join `tabDelivery Trip` t on t.name = i.parent
		where i.purchase_order = %(po)s and t.docstatus < 2
		  and (t.docstatus = 0 or t.status in %(states)s)
		group by i.po_detail
		""",
		{"po": po.name, "states": OPEN_TRIP_STATES},
		as_dict=True,
	):
		on_trips[row.po_detail] = flt(row.qty)

	lines = []
	for item in po.items:
		factor = flt(item.conversion_factor) or 1
		ordered = flt(item.qty) * factor
		received = flt(item.received_qty) * factor
		planned = flt(on_trips.get(item.name))
		pending = max(ordered - received - planned, 0.0)
		lines.append(
			{
				"po_detail": item.name,
				"item_code": item.item_code,
				"item_name": item.item_name,
				"uom": item.uom,
				"stock_uom": item.stock_uom,
				"conversion_factor": factor,
				"rate": flt(item.rate),
				"warehouse": item.warehouse,
				"ordered": ordered,
				"received": received,
				"on_trips": planned,
				"pending": pending if pending > QTY_TOLERANCE else 0.0,
			}
		)
	return lines


@frappe.whitelist()
def preview_po_trips(purchase_order):
	po = frappe.get_doc("Purchase Order", purchase_order)
	if po.docstatus != 1:
		frappe.throw(_("Submit the Purchase Order before planning trips for it."))

	from thameen_erp.overrides.vehicle_load import list_vehicles_for_planning

	return {
		"purchase_order": po.name,
		"supplier": po.supplier,
		"company": po.company,
		"schedule_date": po.schedule_date,
		"lines": pending_po_lines(po),
		"vehicles": list_vehicles_for_planning(),
		"default_warehouse": po.get("set_warehouse") or (po.items[0].warehouse if po.items else None),
		"one_item_per_trip": bool(frappe.db.get_single_value("Thameen Fleet Settings", "one_item_per_trip")),
	}


@frappe.whitelist()
def auto_plan(purchase_order, vehicle, start_date=None, days_between=1, use_capacity=1):
	"""Split every pending line by one truck's capacity into dated trips.

	Trip 1 gets the truck's free space (or full capacity with use_capacity),
	the rest get full capacity each, dates stepping by `days_between`.
	Pure suggestion — the dialog lets the dispatcher rewrite all of it.
	"""
	po = frappe.get_doc("Purchase Order", purchase_order)
	cap, avail = frappe.db.get_value("Vehicle", vehicle, ["custom_capacity", "custom_available_qty"])
	cap, avail = flt(cap), flt(avail)
	if cap <= 0:
		frappe.throw(_("Set a Capacity on {0} before planning by it.").format(vehicle))

	date = getdate(start_date or po.schedule_date or nowdate())
	plan = []
	room = cap if cint(use_capacity) else avail
	for line in pending_po_lines(po):
		left = flt(line["pending"])
		while left > QTY_TOLERANCE:
			if room <= QTY_TOLERANCE:
				room = cap
			take = min(left, room)
			plan.append(
				{
					"po_detail": line["po_detail"],
					"item_code": line["item_code"],
					"qty": take,
					"vehicle": vehicle,
					"departure_time": str(date),
				}
			)
			left -= take
			room = 0.0  # each further trip starts a fresh day
			date = add_days(date, cint(days_between) or 1)
	return plan


# ---------------------------------------------------------------------------
# Creating the trips
# ---------------------------------------------------------------------------


@frappe.whitelist()
def create_trips_from_po(
	purchase_order,
	destination,
	plan,
	target_warehouse=None,
	sales_order=None,
	delivery_location=None,
	transportation_charge=None,
):
	"""One draft Delivery Trip per plan row.

	destination = "Own Warehouse"  → target_warehouse required
	destination = "Customer"       → sales_order required; rows are matched to
	                                 the order's pending lines by item.
	plan = [{po_detail, item_code, qty (stock UOM), vehicle?, departure_time?}]
	"""
	frappe.has_permission("Delivery Trip", "create", throw=True)

	po = frappe.get_doc("Purchase Order", purchase_order)
	if po.docstatus != 1:
		frappe.throw(_("Submit the Purchase Order first."))

	if isinstance(plan, str):
		plan = json.loads(plan or "[]")
	plan = [p for p in (plan or []) if flt(p.get("qty")) > 0]
	if not plan:
		frappe.throw(_("Nothing to plan — every row is zero."))

	if destination not in ("Own Warehouse", "Customer", "Decide After Loading"):
		frappe.throw(_("Destination must be Own Warehouse, Customer or Decide After Loading."))
	if destination == "Decide After Loading":
		# Collect first, decide on the road. No warehouse, no Sales Order yet.
		so_pool = {}
	elif destination == "Own Warehouse":
		if not target_warehouse:
			frappe.throw(_("Choose the warehouse the cement is going to."))
		if frappe.db.get_value("Warehouse", target_warehouse, "custom_is_vehicle_warehouse"):
			frappe.throw(_("{0} is a vehicle warehouse. Choose a yard warehouse.").format(target_warehouse))
		so_pool = {}
	else:
		if not sales_order:
			frappe.throw(_("Choose the Sales Order this cement is sold against."))
		so_pool = _sales_order_pool(sales_order)
		delivery_location = delivery_location or frappe.db.get_value(
			"Sales Order", sales_order, "custom_delivery_location"
		)

	# Never place more than the order still has pending.
	pending = {line["po_detail"]: line for line in pending_po_lines(po)}
	placed = {}
	for p in plan:
		placed[p["po_detail"]] = flt(placed.get(p["po_detail"])) + flt(p["qty"])
	for po_detail, qty in placed.items():
		line = pending.get(po_detail)
		if not line:
			frappe.throw(_("Row {0} is not on this Purchase Order.").format(po_detail))
		if qty > flt(line["pending"]) + QTY_TOLERANCE:
			frappe.throw(
				_("{0}: the plan places {1} but only {2} is still pending on the order "
				  "(ordered {3}, received {4}, already on trips {5}).").format(
					line["item_code"], flt(qty, 2), flt(line["pending"], 2),
					flt(line["ordered"], 2), flt(line["received"], 2), flt(line["on_trips"], 2),
				),
				title=_("Plan exceeds the order"),
			)

	created = []
	for index, p in enumerate(plan):
		line = pending[p["po_detail"]]
		factor = flt(line["conversion_factor"]) or 1
		qty_uom = flt(p["qty"]) / factor

		trip = frappe.new_doc("Delivery Trip")
		trip.company = po.company
		trip.departure_time = _departure(p.get("departure_time"), po.schedule_date)
		# Sites sometimes add their own mandatory fields to Delivery Trip (a
		# plain `sales_order` link is common). Fill the ones we can; an inbound
		# trip has no sales order at all, so form-level mandatory is skipped
		# for it — our own validate() still runs in full.
		if destination == "Customer" and sales_order:
			for fieldname in ("sales_order",):
				if trip.meta.has_field(fieldname):
					trip.set(fieldname, sales_order)
			if trip.meta.has_field("customer"):
				trip.customer = frappe.db.get_value("Sales Order", sales_order, "customer")
		else:
			trip.flags.ignore_mandatory = True
		trip.custom_trip_type = "Company Vehicle"
		trip.custom_supply_source = "Direct from Supplier"
		trip.custom_supplier = po.supplier
		trip.custom_purchase_order = po.name
		trip.custom_destination_type = destination
		trip.custom_target_warehouse = target_warehouse if destination == "Own Warehouse" else None
		trip.custom_delivery_location = (
			delivery_location if destination == "Customer"
			else target_warehouse if destination == "Own Warehouse"
			else None
		)
		if destination == "Customer":
			trip.custom_sales_order = sales_order
		if transportation_charge is not None and flt(transportation_charge) > 0:
			trip.custom_transportation_charge = flt(transportation_charge)

		if p.get("vehicle"):
			trip.vehicle = p["vehicle"]
			driver = frappe.db.get_value("Vehicle", p["vehicle"], "custom_assigned_driver")
			if driver:
				trip.driver = driver

		row = {
			"item_code": line["item_code"],
			"item_name": line["item_name"],
			"qty": qty_uom,
			"uom": line["uom"],
			"conversion_factor": factor,
			"stock_uom": line["stock_uom"],
			"purchase_order": po.name,
			"po_detail": p["po_detail"],
			"delivery_location": trip.custom_delivery_location,
		}

		if destination == "Customer":
			so_rows = _take_from_so_pool(so_pool, line["item_code"], flt(p["qty"]), sales_order)
			for so_row, take_stock in so_rows:
				piece = dict(row)
				piece.update(
					{
						"sales_order": sales_order,
						"so_detail": so_row["name"],
						"qty": take_stock / factor,
						"rate": flt(so_row["rate"]),
						"source_warehouse": None,
					}
				)
				trip.append("custom_trip_items", piece)
		else:
			row["rate"] = flt(line["rate"])
			trip.append("custom_trip_items", row)

		trip.flags.thameen_splitting = True
		try:
			trip.insert()
		except frappe.MandatoryError as e:
			if destination == "Own Warehouse" and "sales_order" in str(e):
				frappe.throw(
					_("Your site has made Sales Order mandatory on Delivery Trip rows (Customize Form), "
					  "but a supplier → warehouse trip has no Sales Order. Run bench migrate to apply the "
					  "app's property setters, or clear the mandatory tick in Customize Form → Delivery Trip Item."),
					title=_("Site Customisation Conflict"),
				)
			raise
		created.append(trip.name)

	frappe.msgprint(
		_("{0} trip(s) planned from {1} ({2}): {3}").format(
			len(created),
			get_link_to_form("Purchase Order", po.name),
			_("to {0}").format(target_warehouse) if destination == "Own Warehouse" else _("direct to customer"),
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
def sales_orders_for_po(purchase_order):
	"""Submitted, not-fully-delivered Sales Orders carrying any item on this PO."""
	items = frappe.get_all("Purchase Order Item", filters={"parent": purchase_order}, pluck="item_code")
	if not items:
		return []
	return frappe.db.sql(
		"""
		select distinct so.name, so.customer, so.custom_delivery_location as delivery_location,
		       so.delivery_date, so.per_delivered
		from `tabSales Order` so
		inner join `tabSales Order Item` soi on soi.parent = so.name
		where so.docstatus = 1 and so.status not in ('Closed', 'Completed', 'Cancelled')
		  and soi.item_code in %(items)s and soi.qty > soi.delivered_qty
		order by so.delivery_date
		limit 50
		""",
		{"items": tuple(set(items))},
		as_dict=True,
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
			"custom_trip_source": doc.get("custom_trip_source") or ("Purchase Order" if doc.get("custom_purchase_order") else "Manual"),
			"custom_sales_order": sales_order,
			"custom_target_warehouse": None,
			"custom_delivery_location": location,
		}
		if transportation_charge is not None and flt(transportation_charge) > 0:
			updates["custom_transportation_charge"] = flt(transportation_charge)
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
