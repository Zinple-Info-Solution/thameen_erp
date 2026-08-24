"""Server-side data for the Fleet Dashboard page.

One whitelisted method returns everything the page needs, so the client makes
a single round trip instead of a dozen.
"""

import frappe

from frappe.utils import add_months, flt, get_first_day, getdate, nowdate


@frappe.whitelist()
def get_dashboard_data(company=None, from_date=None, to_date=None):
	frappe.has_permission("Vehicle", "read", throw=True)

	company = company or frappe.defaults.get_user_default("Company")
	to_date = getdate(to_date or nowdate())
	from_date = getdate(from_date or get_first_day(add_months(to_date, -5)))

	return {
		"filters": {"company": company, "from_date": str(from_date), "to_date": str(to_date)},
		"kpi": get_kpis(company, from_date, to_date),
		"fleet_status": get_fleet_status(),
		"monthly": get_monthly_trend(company, from_date, to_date),
		"top_vehicles": get_top_vehicles(company, from_date, to_date),
		"alerts": get_alerts(),
		"pod_pending": get_pod_pending(),
	}


def get_kpis(company, from_date, to_date):
	trips = frappe.db.sql(
		"""
		select count(name) as total,
		       sum(case when status = 'Completed' then 1 else 0 end) as completed,
		       sum(total_distance) as distance,
		       sum(custom_transportation_cost) as freight,
		       sum(custom_delivered_qty) as qty
		from `tabDelivery Trip`
		where docstatus = 1 and departure_time between %(from_date)s and %(to_date)s
		""",
		{"from_date": from_date, "to_date": to_date},
		as_dict=True,
	)[0]

	cost = flt(
		frappe.db.sql(
			"""
			select sum(gle.debit) - sum(gle.credit)
			from `tabGL Entry` gle
			inner join `tabAccount` acc on acc.name = gle.account
			inner join `tabCost Center` cc on cc.name = gle.cost_center
			where gle.is_cancelled = 0 and acc.root_type = 'Expense'
			  and gle.company = %(company)s
			  and gle.posting_date between %(from_date)s and %(to_date)s
			  and cc.name in (select custom_cost_center from `tabVehicle`
			                  where custom_cost_center is not null)
			""",
			{"company": company, "from_date": from_date, "to_date": to_date},
		)[0][0]
	)

	freight = flt(trips.total and trips.freight)
	return {
		"total_trips": flt(trips.total),
		"completed_trips": flt(trips.completed),
		"distance": flt(trips.distance),
		"delivered_qty": flt(trips.qty),
		"freight_revenue": freight,
		"fleet_cost": cost,
		"contribution": freight - cost,
		"cost_per_km": (cost / flt(trips.distance)) if flt(trips.distance) else 0,
		"active_vehicles": frappe.db.count(
			"Vehicle", {"custom_status": ["in", ["Available", "Assigned", "On Trip"]]}
		),
	}


def get_fleet_status():
	rows = frappe.db.sql(
		"""
		select ifnull(custom_status, 'Unspecified') as status, count(name) as count
		from `tabVehicle` group by custom_status
		""",
		as_dict=True,
	)
	return rows


def get_monthly_trend(company, from_date, to_date):
	rows = frappe.db.sql(
		"""
		select date_format(departure_time, '%%Y-%%m') as month,
		       count(name) as trips,
		       sum(custom_transportation_cost) as freight,
		       sum(total_distance) as distance
		from `tabDelivery Trip`
		where docstatus = 1 and departure_time between %(from_date)s and %(to_date)s
		group by month order by month
		""",
		{"from_date": from_date, "to_date": to_date},
		as_dict=True,
	)
	return rows


def get_top_vehicles(company, from_date, to_date, limit=8):
	rows = frappe.db.sql(
		"""
		select vehicle, count(name) as trips,
		       sum(custom_transportation_cost) as freight,
		       sum(total_distance) as distance
		from `tabDelivery Trip`
		where docstatus = 1 and vehicle is not null
		  and departure_time between %(from_date)s and %(to_date)s
		group by vehicle order by freight desc limit %(limit)s
		""",
		{"from_date": from_date, "to_date": to_date, "limit": limit},
		as_dict=True,
	)
	return rows


def get_alerts():
	docs = frappe.get_all(
		"Vehicle Document",
		filters={"status": ["in", ["Expiring Soon", "Expired"]]},
		fields=["name", "vehicle", "document_type", "expiry_date", "status"],
		order_by="expiry_date",
		limit=15,
	)
	licences = frappe.get_all(
		"Driver",
		filters={"status": "Active", "expiry_date": ["<=", frappe.utils.add_days(nowdate(), 30)]},
		fields=["name", "full_name", "expiry_date"],
		limit=15,
	)
	return {"documents": docs, "licences": licences}


def get_pod_pending():
	return frappe.get_all(
		"Delivery Trip",
		filters={
			"docstatus": 1,
			"status": ["in", ["Delivered", "POD Pending"]],
			"custom_pod_received": 0,
		},
		fields=["name", "vehicle", "driver", "departure_time", "custom_sales_order"],
		order_by="departure_time",
		limit=15,
	)
