"""Delivery Trip (ERPNext standard) used as the Trip Sheet.

Lifecycle enforced here:

    Draft -> Scheduled -> Loading -> In Transit -> Delivered -> Completed

  * Loading    : Material Transfer moves stock from the loading warehouse
                 onto the vehicle warehouse (this is the "truck stock").
  * Delivered  : a Delivery Note is raised from the vehicle warehouse against
                 the Sales Order. ERPNext's own logic then updates
                 `per_delivered` and the Sales Order status.
  * Completed  : requires at least one POD row. Closes the Sales Order when
                 every trip against it is complete and qty is fully delivered.
"""

import frappe
from frappe import _
from frappe.utils import flt

from thameen_erp.overrides.vehicle import ensure_vehicle_masters

TERMINAL_STATES = ("Completed", "Cancelled")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(doc, method=None):
	_set_distance(doc)
	_validate_trip_qty(doc)
	_validate_vehicle_capacity(doc)
	_set_pod_flag(doc)

	if doc.get("custom_trip_type") == "External Transport" and not doc.get(
		"custom_external_transporter"
	):
		frappe.throw(_("Select the External Transporter for an external transport trip."))

	if doc.status == "Completed" and not doc.get("custom_pod_received"):
		frappe.throw(
			_("Attach at least one Proof of Delivery document before completing this trip.")
		)


def _set_distance(doc):
	start = flt(doc.get("custom_starting_odometer"))
	end = flt(doc.get("custom_ending_odometer"))
	if start and end:
		if end < start:
			frappe.throw(_("Ending Odometer cannot be less than Starting Odometer."))
		doc.total_distance = end - start


def _set_pod_flag(doc):
	rows = [row for row in (doc.get("custom_pod_documents") or []) if row.get("attachment")]
	doc.custom_pod_received = 1 if rows else 0


def _validate_trip_qty(doc):
	"""A trip may not plan more than the Sales Order still owes."""
	if not (doc.get("custom_sales_order") and doc.get("custom_item")):
		return

	ordered = flt(
		frappe.db.get_value(
			"Sales Order Item",
			{"parent": doc.custom_sales_order, "item_code": doc.custom_item},
			"sum(qty)",
		)
	)
	if not ordered:
		return

	already = flt(
		frappe.db.get_value(
			"Delivery Trip",
			{
				"custom_sales_order": doc.custom_sales_order,
				"custom_item": doc.custom_item,
				"docstatus": 1,
				"status": ("!=", "Cancelled"),
				"name": ("!=", doc.name),
			},
			"sum(custom_planned_qty)",
		)
	)

	if flt(doc.get("custom_planned_qty")) + already > ordered:
		frappe.throw(
			_("Planned qty {0} exceeds the balance on {1}. Ordered {2}, already on trips {3}.").format(
				flt(doc.custom_planned_qty), doc.custom_sales_order, ordered, already
			)
		)


def _validate_vehicle_capacity(doc):
	if not (doc.get("vehicle") and doc.get("custom_planned_qty")):
		return
	capacity = flt(frappe.get_cached_value("Vehicle", doc.vehicle, "custom_capacity"))
	if capacity and flt(doc.custom_planned_qty) > capacity:
		frappe.msgprint(
			_("Planned qty {0} exceeds the rated capacity {1} of vehicle {2}.").format(
				flt(doc.custom_planned_qty), capacity, doc.vehicle
			),
			indicator="orange",
			title=_("Over Capacity"),
		)


# ---------------------------------------------------------------------------
# Submit / status transitions
# ---------------------------------------------------------------------------


def on_submit(doc, method=None):
	if doc.get("vehicle"):
		_ensure_masters(doc.vehicle)
		frappe.db.set_value("Vehicle", doc.vehicle, "custom_status", "On Trip", update_modified=False)


def on_update_after_submit(doc, method=None):
	previous = doc.get_doc_before_save()
	old_status = previous.status if previous else None
	if old_status == doc.status:
		return

	if doc.status == "Loading":
		load_vehicle(doc)
	elif doc.status == "Delivered":
		create_delivery_note(doc)
	elif doc.status == "Completed":
		_close_trip(doc)


def on_cancel(doc, method=None):
	if doc.get("vehicle"):
		frappe.db.set_value(
			"Vehicle", doc.vehicle, "custom_status", "Available", update_modified=False
		)


def _ensure_masters(vehicle):
	warehouse = frappe.db.get_value("Vehicle", vehicle, "custom_vehicle_warehouse")
	if not warehouse:
		vehicle_doc = frappe.get_doc("Vehicle", vehicle)
		ensure_vehicle_masters(vehicle_doc)


# ---------------------------------------------------------------------------
# Stock movements
# ---------------------------------------------------------------------------


def load_vehicle(doc):
	"""Material Transfer: loading warehouse -> vehicle warehouse."""
	if not (doc.get("custom_loading_warehouse") and doc.get("custom_item")):
		return
	if not flt(doc.get("custom_planned_qty")):
		return

	vehicle_warehouse = frappe.db.get_value("Vehicle", doc.vehicle, "custom_vehicle_warehouse")
	if not vehicle_warehouse:
		frappe.throw(_("Vehicle {0} has no vehicle warehouse. Re-save the Vehicle.").format(doc.vehicle))

	existing = frappe.db.exists(
		"Stock Entry",
		{"custom_delivery_trip": doc.name, "stock_entry_type": "Material Transfer", "docstatus": 1},
	)
	if existing:
		return

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = "Material Transfer"
	se.company = doc.company
	se.custom_delivery_trip = doc.name
	se.custom_vehicle = doc.vehicle
	se.append(
		"items",
		{
			"item_code": doc.custom_item,
			"qty": flt(doc.custom_planned_qty),
			"s_warehouse": doc.custom_loading_warehouse,
			"t_warehouse": vehicle_warehouse,
			"cost_center": doc.get("custom_cost_center"),
		},
	)
	se.insert(ignore_permissions=True)
	se.submit()

	frappe.msgprint(
		_("Loaded {0} onto {1} via {2}").format(doc.custom_item, doc.vehicle, se.name),
		indicator="green",
		alert=True,
	)


def create_delivery_note(doc):
	"""Raise the Delivery Note from the vehicle warehouse against the Sales Order."""
	if not doc.get("custom_sales_order"):
		return

	if frappe.db.exists(
		"Delivery Note", {"custom_delivery_trip": doc.name, "docstatus": ("<", 2)}
	):
		return

	from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

	delivered_qty = flt(doc.get("custom_delivered_qty")) or flt(doc.get("custom_planned_qty"))
	vehicle_warehouse = frappe.db.get_value("Vehicle", doc.vehicle, "custom_vehicle_warehouse")

	dn = make_delivery_note(doc.custom_sales_order)
	dn.custom_delivery_trip = doc.name
	dn.custom_vehicle = doc.vehicle
	dn.custom_driver_link = doc.get("driver")
	dn.custom_transportation_amount = flt(doc.get("custom_transportation_cost"))

	kept = []
	for row in dn.items:
		if doc.get("custom_item") and row.item_code != doc.custom_item:
			continue
		row.qty = delivered_qty
		row.warehouse = vehicle_warehouse or row.warehouse
		row.cost_center = doc.get("custom_cost_center") or row.cost_center
		row.custom_delivery_trip = doc.name if row.meta.has_field("custom_delivery_trip") else None
		kept.append(row)

	if not kept:
		frappe.throw(
			_("Item {0} is not on Sales Order {1}.").format(doc.custom_item, doc.custom_sales_order)
		)

	dn.items = kept
	for idx, row in enumerate(dn.items, start=1):
		row.idx = idx

	dn.flags.ignore_permissions = True
	dn.insert()
	dn.submit()

	doc.db_set("custom_delivered_qty", delivered_qty, update_modified=False)

	frappe.msgprint(
		_("Delivery Note {0} created from trip {1}").format(dn.name, doc.name),
		indicator="green",
		alert=True,
	)


# ---------------------------------------------------------------------------
# Completion + Sales Order closure
# ---------------------------------------------------------------------------


def _close_trip(doc):
	if doc.get("vehicle"):
		frappe.db.set_value(
			"Vehicle",
			doc.vehicle,
			{
				"custom_status": "Available",
				"last_odometer": flt(doc.get("custom_ending_odometer"))
				or frappe.db.get_value("Vehicle", doc.vehicle, "last_odometer"),
			},
			update_modified=False,
		)
	if doc.get("custom_sales_order"):
		close_sales_order_if_complete(doc.custom_sales_order)


def close_sales_order_if_complete(sales_order):
	"""Close the SO only when every trip is done and delivery is 100%."""
	so = frappe.get_doc("Sales Order", sales_order)
	if so.docstatus != 1 or so.status in ("Closed", "Cancelled"):
		return

	open_trips = frappe.db.count(
		"Delivery Trip",
		{
			"custom_sales_order": sales_order,
			"docstatus": 1,
			"status": ("not in", TERMINAL_STATES),
		},
	)
	if open_trips:
		return

	if flt(so.per_delivered) < 99.99:
		return

	so.flags.ignore_permissions = True
	so.update_status("Closed")
	frappe.msgprint(
		_("Sales Order {0} fully delivered and closed.").format(sales_order),
		indicator="green",
		alert=True,
	)


@frappe.whitelist()
def set_trip_status(trip, status):
	"""Called from the Delivery Trip form buttons."""
	frappe.has_permission("Delivery Trip", "write", throw=True)
	doc = frappe.get_doc("Delivery Trip", trip)
	doc.status = status
	doc.save()
	return doc.status
