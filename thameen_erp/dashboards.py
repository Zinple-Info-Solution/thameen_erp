"""Connections-panel links so related documents are one click away."""

from frappe import _


def sales_order_dashboard(data):
	data.setdefault("non_standard_fieldnames", {})["Delivery Trip"] = "custom_sales_order"
	data.setdefault("transactions", []).insert(
		0, {"label": _("Fleet"), "items": ["Delivery Trip"]}
	)
	return data


def purchase_order_dashboard(data):
	data.setdefault("non_standard_fieldnames", {})["Delivery Trip"] = "custom_purchase_order"
	data.setdefault("transactions", []).insert(
		0, {"label": _("Fleet"), "items": ["Delivery Trip"]}
	)
	return data


def delivery_trip_dashboard(data):
	nsf = data.setdefault("non_standard_fieldnames", {})
	nsf.update(
		{
			"Stock Entry": "custom_delivery_trip",
			"Purchase Order": "custom_delivery_trip",
			"Purchase Receipt": "custom_delivery_trip",
			"Material Request": "custom_delivery_trip",
			"Delivery Note": "custom_delivery_trip",
		}
	)
	data["transactions"] = [
		{"label": _("Stock"), "items": ["Stock Entry", "Delivery Note"]},
		{"label": _("Purchasing"), "items": ["Purchase Order", "Purchase Receipt", "Material Request"]},
	]
	return data


def vehicle_dashboard(data):
	nsf = data.setdefault("non_standard_fieldnames", {})
	nsf.update(
		{
			"Delivery Trip": "vehicle",
			"Stock Entry": "custom_vehicle",
			"Purchase Receipt": "custom_vehicle",
			"Vehicle Document": "vehicle",
		}
	)
	data.setdefault("transactions", []).insert(
		0, {"label": _("Fleet"), "items": ["Delivery Trip", "Stock Entry", "Purchase Receipt", "Vehicle Document"]}
	)
	return data
