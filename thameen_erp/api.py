"""Whitelisted endpoints for consolidated billing and fleet lookups."""

import frappe
from frappe import _
from frappe.utils import flt, get_first_day, get_last_day, getdate


# ---------------------------------------------------------------------------
# Consolidated monthly billing
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_billable_deliveries(customer, from_date, to_date, company=None):
	"""Every submitted, unbilled Delivery Note for the customer in the period."""
	frappe.has_permission("Sales Invoice", "create", throw=True)

	filters = {
		"customer": customer,
		"docstatus": 1,
		"posting_date": ("between", [getdate(from_date), getdate(to_date)]),
		"per_billed": ("<", 99.99),
		"is_return": 0,
	}
	if company:
		filters["company"] = company

	notes = frappe.get_all(
		"Delivery Note",
		filters=filters,
		fields=[
			"name",
			"posting_date",
			"custom_delivery_trip",
			"custom_vehicle",
			"custom_transportation_amount",
			"grand_total",
			"per_billed",
		],
		order_by="posting_date",
	)
	return notes


@frappe.whitelist()
def make_consolidated_invoice(customer, from_date=None, to_date=None, billing_month=None, company=None):
	"""Build ONE Sales Invoice covering a customer's whole month.

	Cement lines keep their normal income account; freight is added as a
	separate line per vehicle so Transportation Revenue can be reported
	against each vehicle's cost center independently of cement margin.
	"""
	frappe.has_permission("Sales Invoice", "create", throw=True)

	if billing_month and not (from_date and to_date):
		anchor = getdate(billing_month)
		from_date, to_date = get_first_day(anchor), get_last_day(anchor)

	notes = get_billable_deliveries(customer, from_date, to_date, company)
	if not notes:
		frappe.throw(
			_("No unbilled Delivery Notes for {0} between {1} and {2}.").format(
				customer, from_date, to_date
			)
		)

	settings = frappe.get_single("Thameen Fleet Settings")

	si = frappe.new_doc("Sales Invoice")
	si.customer = customer
	si.company = company or notes[0].get("company") or frappe.defaults.get_user_default("Company")
	si.custom_is_consolidated_run = 1
	si.custom_billing_from_date = from_date
	si.custom_billing_to_date = to_date
	si.set_posting_time = 1
	si.posting_date = to_date

	freight_by_vehicle = {}

	for note in notes:
		items = frappe.get_all(
			"Delivery Note Item",
			filters={"parent": note.name, "docstatus": 1},
			fields=[
				"name",
				"item_code",
				"item_name",
				"description",
				"qty",
				"rate",
				"uom",
				"conversion_factor",
				"stock_uom",
				"warehouse",
				"income_account",
				"cost_center",
				"against_sales_order",
				"so_detail",
			],
			order_by="idx",
		)
		for row in items:
			si.append(
				"items",
				{
					"item_code": row.item_code,
					"item_name": row.item_name,
					"description": row.description,
					"qty": row.qty,
					"rate": row.rate,
					"uom": row.uom,
					"conversion_factor": row.conversion_factor or 1,
					"warehouse": row.warehouse,
					"income_account": row.income_account or settings.cement_income_account,
					"cost_center": row.cost_center,
					"delivery_note": note.name,
					"dn_detail": row.name,
					"sales_order": row.against_sales_order,
					"so_detail": row.so_detail,
					"custom_delivery_trip": note.custom_delivery_trip,
					"custom_vehicle": note.custom_vehicle,
				},
			)

		freight = flt(note.custom_transportation_amount)
		if freight and note.custom_vehicle:
			freight_by_vehicle.setdefault(note.custom_vehicle, 0)
			freight_by_vehicle[note.custom_vehicle] += freight

	_append_freight_lines(si, freight_by_vehicle, settings)

	si.run_method("set_missing_values")
	si.run_method("calculate_taxes_and_totals")
	return si


def _append_freight_lines(si, freight_by_vehicle, settings):
	if not freight_by_vehicle:
		return
	if not settings.transportation_item:
		frappe.msgprint(
			_("Set a Transportation Charge Item in Thameen Fleet Settings to bill freight separately."),
			indicator="orange",
		)
		return

	for vehicle, amount in freight_by_vehicle.items():
		cost_center = frappe.get_cached_value("Vehicle", vehicle, "custom_cost_center")
		si.append(
			"items",
			{
				"item_code": settings.transportation_item,
				"description": _("Transportation charges — {0}").format(vehicle),
				"qty": 1,
				"rate": flt(amount),
				"income_account": settings.transportation_income_account,
				"cost_center": cost_center,
				"custom_vehicle": vehicle,
				"custom_is_transportation_row": 1,
			},
		)


# ---------------------------------------------------------------------------
# Fleet lookups used by client scripts and dashboards
# ---------------------------------------------------------------------------


@frappe.whitelist()
def get_available_vehicles(vehicle_type=None, min_capacity=None):
	filters = {"custom_status": ("in", ["Available", "Assigned"])}
	if vehicle_type:
		filters["custom_vehicle_type"] = vehicle_type
	if min_capacity:
		filters["custom_capacity"] = (">=", flt(min_capacity))

	return frappe.get_all(
		"Vehicle",
		filters=filters,
		fields=[
			"name",
			"custom_vehicle_type",
			"custom_capacity",
			"custom_status",
			"custom_assigned_driver",
			"custom_vehicle_warehouse",
		],
		order_by="custom_status, name",
	)


@frappe.whitelist()
def get_vehicle_stock(vehicle, item_code=None):
	warehouse = frappe.get_cached_value("Vehicle", vehicle, "custom_vehicle_warehouse")
	if not warehouse:
		return []
	filters = {"warehouse": warehouse, "actual_qty": (">", 0)}
	if item_code:
		filters["item_code"] = item_code
	return frappe.get_all(
		"Bin", filters=filters, fields=["item_code", "actual_qty", "stock_uom", "warehouse"]
	)


@frappe.whitelist()
def check_stock_availability(item_code, qty, company=None):
	"""Warehouse stock plus stock already sitting on trucks."""
	bins = frappe.get_all(
		"Bin",
		filters={"item_code": item_code, "actual_qty": (">", 0)},
		fields=["warehouse", "actual_qty", "reserved_qty"],
	)
	vehicle_warehouses = set(
		frappe.get_all(
			"Warehouse", filters={"custom_is_vehicle_warehouse": 1}, pluck="name"
		)
	)

	warehouse_qty = sum(flt(b.actual_qty) for b in bins if b.warehouse not in vehicle_warehouses)
	truck_qty = sum(flt(b.actual_qty) for b in bins if b.warehouse in vehicle_warehouses)
	reserved = sum(flt(b.reserved_qty) for b in bins)

	return {
		"item_code": item_code,
		"requested_qty": flt(qty),
		"warehouse_qty": warehouse_qty,
		"truck_qty": truck_qty,
		"reserved_qty": reserved,
		"available_qty": warehouse_qty + truck_qty - reserved,
		"sufficient": (warehouse_qty + truck_qty - reserved) >= flt(qty),
		"breakdown": bins,
	}
