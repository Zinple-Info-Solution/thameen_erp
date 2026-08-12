"""Freight billed vs freight cost, grouped by month, vehicle or customer."""

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	group_by = filters.get("group_by") or "Vehicle"

	conditions = ["dt.docstatus = 1", "dt.status = 'Completed'"]
	values = {}
	if filters.get("from_date"):
		conditions.append("dt.departure_time >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("dt.departure_time <= %(to_date)s")
		values["to_date"] = filters.to_date
	if filters.get("company"):
		conditions.append("dt.company = %(company)s")
		values["company"] = filters.company

	group_expr = {
		"Vehicle": "dt.vehicle",
		"Month": "date_format(dt.departure_time, '%%Y-%%m')",
		"Customer": "so.customer",
		"Transport Type": "dt.custom_trip_type",
	}[group_by]

	rows = frappe.db.sql(
		f"""
		select {group_expr} as grouping_key,
		       count(dt.name) as trips,
		       sum(dt.total_distance) as distance,
		       sum(dt.custom_delivered_qty) as qty,
		       sum(dt.custom_transportation_cost) as freight
		from `tabDelivery Trip` dt
		left join `tabSales Order` so on so.name = dt.custom_sales_order
		where {" and ".join(conditions)}
		group by grouping_key
		order by freight desc
		""",
		values,
		as_dict=True,
	)

	data = []
	for row in rows:
		distance = flt(row.distance)
		qty = flt(row.qty)
		freight = flt(row.freight)
		data.append(
			{
				"grouping_key": row.grouping_key,
				"trips": row.trips,
				"distance": distance,
				"qty": qty,
				"freight": freight,
				"freight_per_km": (freight / distance) if distance else 0,
				"freight_per_unit": (freight / qty) if qty else 0,
			}
		)

	columns = get_columns(group_by)
	chart = {
		"data": {
			"labels": [r["grouping_key"] for r in data[:12]],
			"datasets": [{"name": _("Freight"), "values": [r["freight"] for r in data[:12]]}],
		},
		"type": "bar",
	}
	return columns, data, None, chart


def get_columns(group_by):
	first = {
		"Vehicle": {"label": _("Vehicle"), "fieldtype": "Link", "options": "Vehicle"},
		"Month": {"label": _("Month"), "fieldtype": "Data"},
		"Customer": {"label": _("Customer"), "fieldtype": "Link", "options": "Customer"},
		"Transport Type": {"label": _("Transport Type"), "fieldtype": "Data"},
	}[group_by]
	first.update({"fieldname": "grouping_key", "width": 180})

	return [
		first,
		{"label": _("Trips"), "fieldname": "trips", "fieldtype": "Int", "width": 80},
		{"label": _("Distance"), "fieldname": "distance", "fieldtype": "Float", "width": 110},
		{"label": _("Qty Delivered"), "fieldname": "qty", "fieldtype": "Float", "width": 120},
		{"label": _("Freight"), "fieldname": "freight", "fieldtype": "Currency", "width": 130},
		{"label": _("Freight / Km"), "fieldname": "freight_per_km", "fieldtype": "Currency", "width": 120},
		{"label": _("Freight / Unit"), "fieldname": "freight_per_unit", "fieldtype": "Currency", "width": 130},
	]
