"""Trip Cost Report — per-trip revenue, cost and contribution."""

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	conditions, values = build_conditions(filters)

	trips = frappe.db.sql(
		f"""
		select dt.name as trip, dt.status, dt.departure_time, dt.vehicle, dt.driver,
		       dt.custom_sales_order as sales_order, dt.custom_item as item,
		       dt.custom_planned_qty as planned_qty, dt.custom_delivered_qty as delivered_qty,
		       dt.total_distance as distance, dt.custom_transportation_cost as freight,
		       dt.custom_cost_center as cost_center, dt.custom_trip_type as trip_type,
		       dt.custom_external_transporter as transporter
		from `tabDelivery Trip` dt
		where {conditions}
		order by dt.departure_time desc
		""",
		values,
		as_dict=True,
	)
	if not trips:
		return get_columns(), []

	expenses = get_trip_expenses([t.trip for t in trips])

	data = []
	for trip in trips:
		cost = flt(expenses.get(trip.trip))
		freight = flt(trip.freight)
		distance = flt(trip.distance)
		data.append(
			{
				**trip,
				"trip_expense": cost,
				"contribution": freight - cost,
				"cost_per_km": (cost / distance) if distance else 0,
				"cost_per_unit": (cost / flt(trip.delivered_qty)) if flt(trip.delivered_qty) else 0,
			}
		)

	return get_columns(), data


def build_conditions(filters):
	conditions = ["dt.docstatus = 1"]
	values = {}
	if filters.get("from_date"):
		conditions.append("dt.departure_time >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("dt.departure_time <= %(to_date)s")
		values["to_date"] = filters.to_date
	if filters.get("vehicle"):
		conditions.append("dt.vehicle = %(vehicle)s")
		values["vehicle"] = filters.vehicle
	if filters.get("driver"):
		conditions.append("dt.driver = %(driver)s")
		values["driver"] = filters.driver
	if filters.get("status"):
		conditions.append("dt.status = %(status)s")
		values["status"] = filters.status
	if filters.get("company"):
		conditions.append("dt.company = %(company)s")
		values["company"] = filters.company
	return " and ".join(conditions), values


def get_trip_expenses(trips):
	"""Expenses booked directly against a trip via Stock Entry or Journal Entry."""
	if not trips:
		return {}

	out = {}
	stock = frappe.db.sql(
		"""
		select se.custom_delivery_trip as trip, sum(se.total_outgoing_value) as amount
		from `tabStock Entry` se
		where se.docstatus = 1 and se.custom_delivery_trip in %(trips)s
		group by se.custom_delivery_trip
		""",
		{"trips": trips},
		as_dict=True,
	)
	for row in stock:
		out[row.trip] = flt(out.get(row.trip)) + flt(row.amount)
	return out


def get_columns():
	return [
		{"label": _("Trip"), "fieldname": "trip", "fieldtype": "Link", "options": "Delivery Trip", "width": 150},
		{"label": _("Date"), "fieldname": "departure_time", "fieldtype": "Datetime", "width": 150},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 100},
		{"label": _("Vehicle"), "fieldname": "vehicle", "fieldtype": "Link", "options": "Vehicle", "width": 120},
		{"label": _("Driver"), "fieldname": "driver", "fieldtype": "Link", "options": "Driver", "width": 120},
		{"label": _("Sales Order"), "fieldname": "sales_order", "fieldtype": "Link", "options": "Sales Order", "width": 140},
		{"label": _("Item"), "fieldname": "item", "fieldtype": "Link", "options": "Item", "width": 120},
		{"label": _("Planned"), "fieldname": "planned_qty", "fieldtype": "Float", "width": 90},
		{"label": _("Delivered"), "fieldname": "delivered_qty", "fieldtype": "Float", "width": 90},
		{"label": _("Distance"), "fieldname": "distance", "fieldtype": "Float", "width": 90},
		{"label": _("Freight Revenue"), "fieldname": "freight", "fieldtype": "Currency", "width": 130},
		{"label": _("Trip Expense"), "fieldname": "trip_expense", "fieldtype": "Currency", "width": 120},
		{"label": _("Contribution"), "fieldname": "contribution", "fieldtype": "Currency", "width": 120},
		{"label": _("Cost / Km"), "fieldname": "cost_per_km", "fieldtype": "Currency", "width": 100},
		{"label": _("Cost / Unit"), "fieldname": "cost_per_unit", "fieldtype": "Currency", "width": 100},
	]
