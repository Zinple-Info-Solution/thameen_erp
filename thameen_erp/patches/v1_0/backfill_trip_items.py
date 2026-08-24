"""Move existing single-item Delivery Trips onto the Delivery Trip Item table.

Nothing is deleted: the legacy `custom_item` / `custom_planned_qty` columns stay
where they are and become read-only summaries, so an incomplete run can simply be
re-run. The patch is idempotent — trips that already have rows are skipped.
"""

import frappe
from frappe.utils import flt


def execute():
	frappe.reload_doc("thameen_erp", "doctype", "delivery_trip_item")

	_backfill_sales_order_item_location()
	_backfill_trip_rows()


def _backfill_sales_order_item_location():
	"""Rows inherit the order's site so existing orders can be planned."""
	if not (
		frappe.db.has_column("Sales Order Item", "custom_delivery_location")
		and frappe.db.has_column("Sales Order", "custom_delivery_location")
	):
		return

	frappe.db.sql(
		"""
		UPDATE `tabSales Order Item` soi
		INNER JOIN `tabSales Order` so ON so.name = soi.parent
		SET soi.custom_delivery_location = so.custom_delivery_location
		WHERE IFNULL(soi.custom_delivery_location, '') = ''
		  AND IFNULL(so.custom_delivery_location, '') <> ''
		"""
	)


def _backfill_trip_rows():
	if not frappe.db.has_column("Delivery Trip", "custom_item"):
		return

	trips = frappe.db.sql(
		"""
		SELECT name, docstatus, custom_sales_order, custom_item, custom_planned_qty,
		       custom_delivered_qty, custom_loading_warehouse
		FROM `tabDelivery Trip`
		WHERE IFNULL(custom_item, '') <> ''
		""",
		as_dict=True,
	)
	if not trips:
		return

	already = set(
		frappe.get_all(
			"Delivery Trip Item",
			filters={"parenttype": "Delivery Trip"},
			pluck="parent",
			distinct=True,
		)
	)

	migrated = 0

	for trip in trips:
		if trip.name in already:
			continue
		if not flt(trip.custom_planned_qty):
			continue

		so_detail = None
		location = None

		if trip.custom_sales_order:
			so_item = frappe.db.get_value(
				"Sales Order Item",
				{"parent": trip.custom_sales_order, "item_code": trip.custom_item},
				["name", "rate", "uom", "stock_uom", "custom_delivery_location"],
				as_dict=True,
			)
			if so_item:
				so_detail = so_item.name
				location = so_item.custom_delivery_location

		item = frappe.db.get_value(
			"Item", trip.custom_item, ["item_name", "stock_uom"], as_dict=True
		) or frappe._dict()

		row = frappe.get_doc(
			{
				"doctype": "Delivery Trip Item",
				"parent": trip.name,
				"parenttype": "Delivery Trip",
				"parentfield": "custom_trip_items",
				"idx": 1,
				"sales_order": trip.custom_sales_order,
				"so_detail": so_detail,
				"item_code": trip.custom_item,
				"item_name": item.get("item_name"),
				"qty": flt(trip.custom_planned_qty),
				"delivered_qty": flt(trip.custom_delivered_qty),
				"uom": item.get("stock_uom"),
				"stock_uom": item.get("stock_uom"),
				"conversion_factor": 1,
				"source_warehouse": trip.custom_loading_warehouse,
				"delivery_location": location,
			}
		)
		row.flags.ignore_permissions = True
		row.flags.ignore_validate = True
		row.insert()

		if location and frappe.db.has_column("Delivery Trip", "custom_delivery_location"):
			frappe.db.set_value(
				"Delivery Trip", trip.name, "custom_delivery_location", location, update_modified=False
			)

		migrated += 1

	if migrated:
		frappe.db.commit()
		print(f"Thameen ERP: migrated {migrated} Delivery Trip(s) onto the trip items table")
