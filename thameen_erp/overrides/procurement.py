"""When the yard does not have the cement: buy it, or have the supplier deliver.

Two paths, chosen on the Delivery Trip.

1. Shortfall purchase (Supply Source = Own Warehouse)
   The trip still loads from the yard, but the yard is short. The dispatcher is
   shown exactly how short, per item, and can raise a Purchase Order (or a
   Material Request for the buyer to action) for the shortfall into the loading
   warehouse. Once the Purchase Receipt lands, the trip loads as normal.

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
from frappe.utils import cint, flt, get_link_to_form, getdate, nowdate

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
	above. Walk them in order and let each consume what the earlier ones took."""
	taken = {}
	result["shortfalls"] = []
	result["sufficient"] = True
	for line in result["rows"]:
		key = (line["item_code"], line["source_warehouse"])
		left = max(flt(line["source_qty"]) - flt(taken.get(key)), 0.0)
		need_from_source = flt(line["planned_qty"]) - flt(line["on_truck_free"])
		from_source = min(need_from_source, left)
		taken[key] = flt(taken.get(key)) + from_source
		line["from_source"] = from_source
		short = max(need_from_source - from_source, 0.0)
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
	                    loading warehouse. Used when the yard is short.
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
		lines = [
			{
				"row_name": None,
				"item_code": r["item_code"],
				"qty": flt(r["shortfall"]) / (flt(r["conversion_factor"]) or 1),
				"uom": r["uom"],
				"conversion_factor": flt(r["conversion_factor"]) or 1,
				"warehouse": r["source_warehouse"],
			}
			for r in check["shortfalls"]
			if not wanted or r["idx"] in wanted
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
	lines = [r for r in check["shortfalls"] if not wanted or r["idx"] in wanted]
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


@frappe.whitelist()
def switch_to_direct_supply(trip, supplier=None):
	"""Turn an own-warehouse trip into a direct-from-supplier one and raise the PO."""
	doc = frappe.get_doc("Delivery Trip", trip)
	frappe.has_permission("Delivery Trip", "write", doc=doc, throw=True)
	if doc.docstatus != 0:
		frappe.throw(_("Only a draft trip can be switched to direct supply."))

	supplier = supplier or frappe.db.get_single_value("Thameen Fleet Settings", "default_cement_supplier")
	if not supplier:
		frappe.throw(_("Choose a Supplier."))

	doc.custom_supply_source = DIRECT
	doc.custom_supplier = supplier
	doc.save()
	return make_purchase_order(trip, supplier=supplier, mode="direct")


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bin_qty(item_code, warehouse):
	if not (item_code and warehouse):
		return 0.0
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))


def _vehicle_warehouse_or_loading(doc):
	"""PO warehouse for a direct trip. The receipt overrides it with the
	truck's warehouse at Loading, so this is only a placeholder ERPNext needs."""
	if doc.get("vehicle"):
		wh = frappe.db.get_value("Vehicle", doc.vehicle, "custom_vehicle_warehouse")
		if wh:
			return wh
	return doc.get("custom_loading_warehouse") or frappe.db.get_value(
		"Warehouse", {"company": doc.company, "is_group": 0, "custom_is_vehicle_warehouse": 0}, "name"
	)
