"""On-time delivery performance and outstanding proof-of-delivery."""

import frappe
from frappe import _
from frappe.utils import date_diff


def execute(filters=None):
	filters = frappe._dict(filters or {})
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
	if filters.get("pod_pending_only"):
		conditions.append("ifnull(dt.custom_pod_received, 0) = 0")
		conditions.append("dt.status in ('Delivered', 'POD Pending')")

	rows = frappe.db.sql(
		f"""
		select dt.name as trip, dt.status, dt.vehicle, dt.driver,
		       dt.departure_time, dt.custom_sales_order as sales_order,
		       dt.custom_delivered_qty as delivered_qty,
		       dt.custom_pod_received as pod_received,
		       so.delivery_date as promised_date,
		       dn.name as delivery_note, dn.posting_date as delivered_on
		from `tabDelivery Trip` dt
		left join `tabSales Order` so on so.name = dt.custom_sales_order
		left join `tabDelivery Note` dn on dn.custom_delivery_trip = dt.name and dn.docstatus = 1
		where {" and ".join(conditions)}
		order by dt.departure_time desc
		""",
		values,
		as_dict=True,
	)

	data = []
	for row in rows:
		delay = None
		if row.promised_date and row.delivered_on:
			delay = date_diff(row.delivered_on, row.promised_date)

		data.append(
			{
				**row,
				"delay_days": delay,
				"on_time": 1 if (delay is not None and delay <= 0) else 0,
				"pod_status": _("Received") if row.pod_received else _("Pending"),
			}
		)

	return get_columns(), data, get_summary_message(data), get_chart(data)


def get_summary_message(data):
	if not data:
		return None
	completed = [r for r in data if r["delivered_on"]]
	on_time = sum(r["on_time"] for r in completed)
	pending_pod = sum(1 for r in data if not r["pod_received"])
	rate = (on_time / len(completed) * 100) if completed else 0
	return _("On-time delivery {0}% ({1} of {2}) · {3} trip(s) awaiting POD").format(
		round(rate, 1), on_time, len(completed), pending_pod
	)


def get_chart(data):
	on_time = sum(r["on_time"] for r in data)
	late = sum(1 for r in data if r["delay_days"] is not None and r["delay_days"] > 0)
	return {
		"data": {
			"labels": [_("On Time"), _("Late")],
			"datasets": [{"name": _("Trips"), "values": [on_time, late]}],
		},
		"type": "percentage",
		"colors": ["#28a745", "#e24c4c"],
	}


def get_columns():
	return [
		{"label": _("Trip"), "fieldname": "trip", "fieldtype": "Link", "options": "Delivery Trip", "width": 150},
		{"label": _("Status"), "fieldname": "status", "fieldtype": "Data", "width": 110},
		{"label": _("Vehicle"), "fieldname": "vehicle", "fieldtype": "Link", "options": "Vehicle", "width": 120},
		{"label": _("Driver"), "fieldname": "driver", "fieldtype": "Link", "options": "Driver", "width": 120},
		{"label": _("Sales Order"), "fieldname": "sales_order", "fieldtype": "Link", "options": "Sales Order", "width": 140},
		{"label": _("Promised"), "fieldname": "promised_date", "fieldtype": "Date", "width": 100},
		{"label": _("Delivered"), "fieldname": "delivered_on", "fieldtype": "Date", "width": 100},
		{"label": _("Delay (Days)"), "fieldname": "delay_days", "fieldtype": "Int", "width": 100},
		{"label": _("Delivery Note"), "fieldname": "delivery_note", "fieldtype": "Link", "options": "Delivery Note", "width": 150},
		{"label": _("POD"), "fieldname": "pod_status", "fieldtype": "Data", "width": 90},
	]
