"""Vehicle Profitability — transportation revenue less all vehicle costs.

Revenue and cost are both read from GL Entry against each vehicle's cost
center, so the report agrees with the general ledger by construction rather
than re-deriving figures from transaction tables.
"""

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not filters.company:
		filters.company = frappe.defaults.get_user_default("Company")

	vehicles = get_vehicles(filters)
	if not vehicles:
		return get_columns(), []

	cc_map = {v.custom_cost_center: v for v in vehicles if v.custom_cost_center}
	gl = get_gl_totals(filters, list(cc_map))
	trips = get_trip_stats(filters, [v.name for v in vehicles])

	data = []
	for vehicle in vehicles:
		cc = vehicle.custom_cost_center
		g = gl.get(cc, {})
		t = trips.get(vehicle.name, {})

		revenue = flt(g.get("revenue"))
		goods_revenue = flt(g.get("goods_revenue"))
		direct = flt(g.get("direct_cost"))
		periodic = flt(g.get("periodic_cost"))
		total_cost = direct + periodic
		profit = revenue - total_cost
		distance = flt(t.get("distance"))

		data.append(
			{
				"vehicle": vehicle.name,
				"vehicle_type": vehicle.custom_vehicle_type,
				"cost_center": cc,
				"trips": flt(t.get("trips")),
				"delivered_qty": flt(t.get("delivered_qty")),
				"distance": distance,
				"revenue": revenue,
				"goods_revenue": goods_revenue,
				"direct_cost": direct,
				"periodic_cost": periodic,
				"total_cost": total_cost,
				"profit": profit,
				"margin": (profit / revenue * 100) if revenue else 0,
				"cost_per_km": (total_cost / distance) if distance else 0,
			}
		)

	data.sort(key=lambda r: r["profit"], reverse=True)
	return get_columns(), data, None, get_chart(data)


def get_vehicles(filters):
	conditions = {}
	if filters.get("vehicle"):
		conditions["name"] = filters.vehicle
	if filters.get("vehicle_type"):
		conditions["custom_vehicle_type"] = filters.vehicle_type
	return frappe.get_all(
		"Vehicle",
		filters=conditions,
		fields=["name", "custom_vehicle_type", "custom_cost_center"],
	)


def get_transport_income_accounts():
	"""Every account a freight charge can post to.

	The default in Thameen Fleet Settings, plus the Item Default income account
	of any item actually used as a transportation charge — freight items may
	carry their own revenue account, and this report must not mistake goods
	revenue for transport revenue.
	"""
	accounts = set()

	default = frappe.db.get_single_value("Thameen Fleet Settings", "transportation_income_account")
	if default:
		accounts.add(default)

	items = frappe.get_all(
		"Delivery Note",
		filters={"custom_transportation_item": ("is", "set"), "docstatus": 1},
		pluck="custom_transportation_item",
		distinct=True,
	)
	if items:
		accounts.update(
			frappe.get_all(
				"Item Default",
				filters={"parent": ("in", items), "income_account": ("is", "set")},
				pluck="income_account",
			)
		)

	return accounts


def get_gl_totals(filters, cost_centers):
	"""Split GL into revenue, direct trip cost and periodic cost."""
	if not cost_centers:
		return {}

	conditions = ["gle.is_cancelled = 0", "gle.cost_center in %(cost_centers)s"]
	values = {"cost_centers": cost_centers, "company": filters.company}

	if filters.get("company"):
		conditions.append("gle.company = %(company)s")
	if filters.get("from_date"):
		conditions.append("gle.posting_date >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("gle.posting_date <= %(to_date)s")
		values["to_date"] = filters.to_date

	rows = frappe.db.sql(
		f"""
		select gle.cost_center, acc.root_type, gle.account,
		       sum(gle.debit) as debit, sum(gle.credit) as credit
		from `tabGL Entry` gle
		inner join `tabAccount` acc on acc.name = gle.account
		where {" and ".join(conditions)}
		group by gle.cost_center, acc.root_type, gle.account
		""",
		values,
		as_dict=True,
	)

	direct_keywords = ("fuel", "toll", "loading", "unloading", "freight", "driver allowance")

	# Cement lines also carry the vehicle cost center (create_delivery_notes sets
	# it, and the consolidated invoice copies it through), so Income on this cost
	# center is NOT all freight. Only the transport accounts count as vehicle
	# revenue; the rest is goods revenue and is reported separately.
	transport_accounts = get_transport_income_accounts()

	out = {}
	for row in rows:
		bucket = out.setdefault(
			row.cost_center,
			{"revenue": 0, "goods_revenue": 0, "direct_cost": 0, "periodic_cost": 0},
		)
		if row.root_type == "Income":
			amount = flt(row.credit) - flt(row.debit)
			key = "revenue" if row.account in transport_accounts else "goods_revenue"
			bucket[key] += amount
		elif row.root_type == "Expense":
			amount = flt(row.debit) - flt(row.credit)
			account_name = (row.account or "").lower()
			key = "direct_cost" if any(k in account_name for k in direct_keywords) else "periodic_cost"
			bucket[key] += amount
	return out


def get_trip_stats(filters, vehicles):
	conditions = ["dt.docstatus = 1", "dt.status = 'Completed'", "dt.vehicle in %(vehicles)s"]
	values = {"vehicles": vehicles}
	if filters.get("from_date"):
		conditions.append("dt.departure_time >= %(from_date)s")
		values["from_date"] = filters.from_date
	if filters.get("to_date"):
		conditions.append("dt.departure_time <= %(to_date)s")
		values["to_date"] = filters.to_date

	rows = frappe.db.sql(
		f"""
		select dt.vehicle, count(dt.name) as trips,
		       sum(dt.total_distance) as distance,
		       sum(dt.custom_delivered_qty) as delivered_qty
		from `tabDelivery Trip` dt
		where {" and ".join(conditions)}
		group by dt.vehicle
		""",
		values,
		as_dict=True,
	)
	return {r.vehicle: r for r in rows}


def get_chart(data):
	top = data[:10]
	return {
		"data": {
			"labels": [r["vehicle"] for r in top],
			"datasets": [
				{"name": _("Revenue"), "values": [r["revenue"] for r in top]},
				{"name": _("Total Cost"), "values": [r["total_cost"] for r in top]},
			],
		},
		"type": "bar",
		"colors": ["#2490ef", "#e24c4c"],
	}


def get_columns():
	return [
		{"label": _("Vehicle"), "fieldname": "vehicle", "fieldtype": "Link", "options": "Vehicle", "width": 130},
		{"label": _("Type"), "fieldname": "vehicle_type", "fieldtype": "Data", "width": 130},
		{"label": _("Cost Center"), "fieldname": "cost_center", "fieldtype": "Link", "options": "Cost Center", "width": 150},
		{"label": _("Trips"), "fieldname": "trips", "fieldtype": "Int", "width": 70},
		{"label": _("Delivered Qty"), "fieldname": "delivered_qty", "fieldtype": "Float", "width": 110},
		{"label": _("Distance"), "fieldname": "distance", "fieldtype": "Float", "width": 100},
		{"label": _("Transport Revenue"), "fieldname": "revenue", "fieldtype": "Currency", "width": 140},
		{
			"label": _("Goods Revenue"),
			"fieldname": "goods_revenue",
			"fieldtype": "Currency",
			"width": 130,
		},
		{"label": _("Direct Cost"), "fieldname": "direct_cost", "fieldtype": "Currency", "width": 120},
		{"label": _("Periodic Cost"), "fieldname": "periodic_cost", "fieldtype": "Currency", "width": 120},
		{"label": _("Total Cost"), "fieldname": "total_cost", "fieldtype": "Currency", "width": 120},
		{"label": _("Profit"), "fieldname": "profit", "fieldtype": "Currency", "width": 120},
		{"label": _("Margin %"), "fieldname": "margin", "fieldtype": "Percent", "width": 90},
		{"label": _("Cost / Km"), "fieldname": "cost_per_km", "fieldtype": "Currency", "width": 100},
	]
