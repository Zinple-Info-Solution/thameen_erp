"""Vehicle (ERPNext standard doctype) extensions.

We do NOT subclass the standard controller — everything runs through
`doc_events` so an ERPNext upgrade can never silently drop our logic.
"""

import frappe
from frappe import _
from frappe.utils import cstr


def validate(doc, method=None):
	if not doc.get("custom_company"):
		doc.custom_company = frappe.defaults.get_user_default("Company") or frappe.db.get_value(
			"Company", {}, "name"
		)

	# Keep the two-way driver link consistent.
	if doc.get("custom_assigned_driver"):
		driver_employee = frappe.db.get_value("Driver", doc.custom_assigned_driver, "employee")
		if driver_employee:
			doc.employee = driver_employee
		if doc.get("custom_status") == "Available":
			doc.custom_status = "Assigned"
	elif doc.get("custom_status") == "Assigned":
		doc.custom_status = "Available"


def after_insert(doc, method=None):
	# Always. The old "Auto-create Cost Center & Warehouse" tick is gone: a
	# vehicle without its warehouse cannot be loaded, planned or stock-checked,
	# so leaving it optional only produced broken trucks.
	ensure_vehicle_masters(doc)


def on_update(doc, method=None):
	# A vehicle created before the app was installed still gets its masters
	# when re-saved.
	if not (doc.get("custom_cost_center") and doc.get("custom_vehicle_warehouse")):
		ensure_vehicle_masters(doc)

	_sync_driver_link(doc)


def ensure_vehicle_masters(doc):
	"""Create CC-{plate} cost center and {plate} - Vehicle warehouse."""
	company = doc.get("custom_company") or frappe.defaults.get_user_default("Company")
	if not company:
		frappe.msgprint(
			_("Set a default Company before auto-creating fleet masters for {0}").format(doc.name),
			indicator="orange",
			alert=True,
		)
		return

	values = {}
	cost_center = doc.get("custom_cost_center") or create_vehicle_cost_center(doc, company)
	warehouse = doc.get("custom_vehicle_warehouse") or create_vehicle_warehouse(doc, company)
	asset_item = doc.get("custom_asset_item") or create_vehicle_asset_item(doc, company)

	if cost_center and cost_center != doc.get("custom_cost_center"):
		values["custom_cost_center"] = cost_center
	if warehouse and warehouse != doc.get("custom_vehicle_warehouse"):
		values["custom_vehicle_warehouse"] = warehouse
	if asset_item and asset_item != doc.get("custom_asset_item"):
		values["custom_asset_item"] = asset_item

	if values:
		# db_set avoids recursion through on_update.
		for field, value in values.items():
			doc.db_set(field, value, update_modified=False)
			doc.set(field, value)


def create_vehicle_asset_item(doc, company):
	"""Create the fixed-asset Item an Asset record needs, named for the plate.

	ERPNext will not let you raise an Asset without an Item that has
	`is_fixed_asset`. Made the same way as the cost center and warehouse so a
	new truck arrives with everything the Asset module asks for, and the item
	is named after the vehicle rather than something generic.

	The Asset itself is NOT created here — it needs a purchase date, value and
	category that only Finance can supply. This just removes the item-creation
	step from their path.
	"""
	item_code = cstr(doc.name).strip()
	if not item_code:
		return None

	if frappe.db.exists("Item", item_code):
		return item_code

	group = _get_asset_item_group()
	if not group:
		return None

	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": item_code,
			"description": _("Vehicle {0}").format(item_code),
			"item_group": group,
			"stock_uom": "Nos",
			# A truck is a fixed asset, not something to be stocked or sold.
			"is_fixed_asset": 1,
			"is_stock_item": 0,
			"is_purchase_item": 1,
			"is_sales_item": 0,
			"asset_category": _get_asset_category(),
		}
	)
	item.flags.ignore_permissions = True
	item.insert(ignore_permissions=True)
	return item.name


def _get_asset_item_group():
	for name in ("Fixed Asset", "Fixed Assets", "All Item Groups"):
		if frappe.db.exists("Item Group", name):
			return name
	return frappe.db.get_value("Item Group", {"is_group": 0}, "name")


def _get_asset_category():
	"""Optional. Asset Category is only required when the Asset is raised."""
	for name in ("Motor Vehicle", "Vehicles", "Motor Vehicles"):
		if frappe.db.exists("Asset Category", name):
			return name
	return frappe.db.get_value("Asset Category", {}, "name")


def create_vehicle_cost_center(doc, company):
	abbr = frappe.get_cached_value("Company", company, "abbr")
	plate = _sanitise(doc.name)
	cc_name = f"CC-{plate}"
	full_name = f"{cc_name} - {abbr}"

	if frappe.db.exists("Cost Center", full_name):
		return full_name

	parent = _get_parent_cost_center(company, abbr)
	if not parent:
		return None

	cc = frappe.get_doc(
		{
			"doctype": "Cost Center",
			"cost_center_name": cc_name,
			"parent_cost_center": parent,
			"company": company,
			"is_group": 0,
		}
	)
	cc.insert(ignore_permissions=True)
	return cc.name


def _get_parent_cost_center(company, abbr):
	"""Group all vehicle cost centers under a single 'Fleet' node."""
	fleet_group = f"Fleet - {abbr}"
	if frappe.db.exists("Cost Center", fleet_group):
		return fleet_group

	root = frappe.db.get_value(
		"Cost Center",
		{"company": company, "is_group": 1, "parent_cost_center": ("is", "not set")},
		"name",
	) or frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name")

	if not root:
		return None

	group = frappe.get_doc(
		{
			"doctype": "Cost Center",
			"cost_center_name": "Fleet",
			"parent_cost_center": root,
			"company": company,
			"is_group": 1,
		}
	)
	group.insert(ignore_permissions=True)
	return group.name


def create_vehicle_warehouse(doc, company):
	abbr = frappe.get_cached_value("Company", company, "abbr")
	plate = _sanitise(doc.name)
	wh_name = f"{plate} - Vehicle"
	full_name = f"{wh_name} - {abbr}"

	if frappe.db.exists("Warehouse", full_name):
		frappe.db.set_value(
			"Warehouse",
			full_name,
			{"custom_is_vehicle_warehouse": 1, "custom_linked_vehicle": doc.name},
			update_modified=False,
		)
		return full_name

	parent = _get_parent_warehouse(company, abbr)

	wh = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": wh_name,
			"parent_warehouse": parent,
			"company": company,
			"is_group": 0,
			"custom_is_vehicle_warehouse": 1,
			"custom_linked_vehicle": doc.name,
		}
	)
	wh.insert(ignore_permissions=True)
	return wh.name


def _get_parent_warehouse(company, abbr):
	fleet_group = f"Vehicles - {abbr}"
	if frappe.db.exists("Warehouse", fleet_group):
		return fleet_group

	root = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 1, "parent_warehouse": ("is", "not set")}, "name"
	) or frappe.db.get_value("Warehouse", {"company": company, "is_group": 1}, "name")

	if not root:
		return None

	group = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": "Vehicles",
			"parent_warehouse": root,
			"company": company,
			"is_group": 1,
		}
	)
	group.insert(ignore_permissions=True)
	return group.name


def _sync_driver_link(doc):
	"""Mirror Vehicle.custom_assigned_driver onto Driver.custom_assigned_vehicle."""
	driver = doc.get("custom_assigned_driver")

	previously = frappe.db.get_all(
		"Driver",
		filters={"custom_assigned_vehicle": doc.name},
		pluck="name",
	)
	for other in previously:
		if other != driver:
			frappe.db.set_value(
				"Driver", other, "custom_assigned_vehicle", None, update_modified=False
			)

	if driver and frappe.db.get_value("Driver", driver, "custom_assigned_vehicle") != doc.name:
		frappe.db.set_value(
			"Driver", driver, "custom_assigned_vehicle", doc.name, update_modified=False
		)


def _sanitise(value):
	"""Cost Center / Warehouse names cannot contain the separator ' - '."""
	return cstr(value).replace("-", "").replace("/", "").strip().upper()
