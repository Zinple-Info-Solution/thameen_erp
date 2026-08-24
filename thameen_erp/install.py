"""
Thameen ERP – installation / migration routines.

Everything here is idempotent. It is called on `after_install` and again on
every `after_migrate`, so it is safe to re-run. We deliberately use
`create_custom_fields(..., update=True)` instead of deleting + recreating,
because deletion drops the underlying column and loses data.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

EXPENSE_TYPES = "\n".join(
	[
		"",
		"Fuel",
		"Maintenance",
		"Spare Parts",
		"Tyres",
		"Oil Change",
		"Battery",
		"Repair",
		"Insurance",
		"Driver Allowance",
		"Parking",
		"Toll",
		"Fines",
		"Registration / Permit",
		"External Transport Hire",
		"Miscellaneous",
	]
)

VEHICLE_STATUS = "\n".join(
	["Available", "Assigned", "On Trip", "Under Maintenance", "Out of Service"]
)

CREDIT_NOTE_STATUS = "\n".join(
	[
		"Not Applicable",
		"Expected",
		"Partially Received",
		"Fully Received",
		"Received Above Expected",
		"Cancelled",
	]
)


def after_install():
	install_customisations()
	create_roles()
	create_workflow()
	rebuild_vehicle_loads()


def after_migrate():
	install_customisations()
	create_roles()
	create_workflow()
	rebuild_vehicle_loads()


def rebuild_vehicle_loads():
	"""Recount Committed / Available on every truck.

	Runs on install and on every migrate so the figures are right from the
	first page load, including on sites whose trips predate these fields.
	"""
	from thameen_erp.overrides.vehicle_load import refresh_vehicle_load

	for name in frappe.get_all("Vehicle", pluck="name"):
		try:
			refresh_vehicle_load(name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"Vehicle load rebuild: {name}")


# ---------------------------------------------------------------------------
# Custom fields
# ---------------------------------------------------------------------------


def get_custom_fields() -> dict:
	"""Single source of truth for every custom field this app adds."""

	vehicle_expense_block = [
		{
			"fieldname": "custom_fleet_section",
			"label": "Fleet Cost Allocation",
			"fieldtype": "Section Break",
			"insert_after": "company",
			"collapsible": 1,
		},
		{
			"fieldname": "custom_vehicle",
			"label": "Vehicle",
			"fieldtype": "Link",
			"options": "Vehicle",
			"insert_after": "custom_fleet_section",
		},
		{
			"fieldname": "custom_expense_type",
			"label": "Fleet Expense Type",
			"fieldtype": "Select",
			"options": EXPENSE_TYPES,
			"insert_after": "custom_vehicle",
		},
		{
			"fieldname": "custom_fleet_col_break",
			"fieldtype": "Column Break",
			"insert_after": "custom_expense_type",
		},
		{
			"fieldname": "custom_transport_type",
			"label": "Transport Type",
			"fieldtype": "Select",
			"options": "\nCompany Vehicle\nExternal Transport",
			"insert_after": "custom_fleet_col_break",
		},
		{
			"fieldname": "custom_external_transport_vendor",
			"label": "External Transport Vendor",
			"fieldtype": "Link",
			"options": "Supplier",
			"insert_after": "custom_transport_type",
			"depends_on": "eval:doc.custom_transport_type=='External Transport'",
		},
	]

	return {
		# ------------------------------------------------------------------
		# Vehicle  (ERPNext standard – Setup module).  Already has:
		# license_plate, make, model, last_odometer, chassis_no, vehicle_value,
		# employee, insurance_company, policy_no, start_date, end_date,
		# fuel_type, uom, color, wheels, doors, carbon_check_date.
		# ------------------------------------------------------------------
		"Vehicle": [
			{
				"fieldname": "custom_fleet_section",
				"label": "Fleet Details",
				"fieldtype": "Section Break",
				"insert_after": "model",
			},
			{
				"fieldname": "custom_company",
				"label": "Company",
				"fieldtype": "Link",
				"options": "Company",
				"reqd": 1,
				"default": ":Company",
				"insert_after": "custom_fleet_section",
			},
			{
				"fieldname": "custom_vehicle_type",
				"label": "Vehicle Type",
				"fieldtype": "Select",
				"options": "\nBulk Cement Tanker\nFlatbed Trailer\nTipper\nTrailer Head\nPickup\nForklift\nOther",
				"insert_after": "custom_company",
			},
			{
				"fieldname": "custom_capacity",
				"label": "Capacity",
				"fieldtype": "Float",
				"insert_after": "custom_vehicle_type",
			},
			{
				"fieldname": "custom_capacity_uom",
				"label": "Capacity UOM",
				"fieldtype": "Link",
				"options": "UOM",
				"insert_after": "custom_capacity",
			},
			{
				"fieldname": "custom_committed_qty",
				"label": "Committed Qty",
				"fieldtype": "Float",
				"read_only": 1,
				"no_copy": 1,
				"insert_after": "custom_capacity_uom",
				"description": "On submitted trips that are Scheduled, Loading or In Transit.",
			},
			{
				"fieldname": "custom_on_truck_qty",
				"label": "On Truck Qty",
				"fieldtype": "Float",
				"read_only": 1,
				"no_copy": 1,
				"in_list_view": 1,
				"in_standard_filter": 1,
				"insert_after": "custom_committed_qty",
				"description": "Physical stock in the vehicle warehouse right now. Updated on every stock movement.",
			},
			{
				"fieldname": "custom_available_qty",
				"label": "Available Qty",
				"fieldtype": "Float",
				"read_only": 1,
				"no_copy": 1,
				"in_list_view": 1,
				"insert_after": "custom_on_truck_qty",
				"description": "Capacity less the larger of Committed Qty and On Truck Qty. Recounted, never incremented.",
			},
			{
				"fieldname": "custom_fleet_col_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_available_qty",
			},
			{
				"fieldname": "custom_status",
				"label": "Status",
				"fieldtype": "Select",
				"options": VEHICLE_STATUS,
				"default": "Available",
				"in_list_view": 1,
				"in_standard_filter": 1,
				"insert_after": "custom_fleet_col_break",
			},
			{
				"fieldname": "custom_assigned_driver",
				"label": "Assigned Driver",
				"fieldtype": "Link",
				"options": "Driver",
				"insert_after": "custom_status",
			},
			{
				"fieldname": "custom_registration_expiry",
				"label": "Registration Expiry",
				"fieldtype": "Date",
				"insert_after": "custom_assigned_driver",
			},
			{
				"fieldname": "custom_accounting_section",
				"label": "Accounting & Stock",
				"fieldtype": "Section Break",
				"insert_after": "custom_registration_expiry",
			},
			{
				"fieldname": "custom_cost_center",
				"label": "Cost Center",
				"fieldtype": "Link",
				"options": "Cost Center",
				"read_only": 1,
				"description": "Auto-created as CC-{license plate}. All trip and periodic costs post here.",
				"insert_after": "custom_accounting_section",
			},
			{
				"fieldname": "custom_vehicle_warehouse",
				"label": "Vehicle Warehouse",
				"fieldtype": "Link",
				"options": "Warehouse",
				"read_only": 1,
				"description": "Stock loaded on the truck but not yet delivered sits here.",
				"insert_after": "custom_cost_center",
			},
			{
				"fieldname": "custom_acct_col_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_vehicle_warehouse",
			},
			{
				"fieldname": "custom_asset",
				"label": "Linked Asset",
				"fieldtype": "Link",
				"options": "Asset",
				"description": "Depreciation is handled entirely by the standard Asset module.",
				"insert_after": "custom_acct_col_break",
			},
			{
				"fieldname": "custom_auto_create_masters",
				"label": "Auto-create Cost Center & Warehouse",
				"fieldtype": "Check",
				"default": "1",
				"insert_after": "custom_asset",
			},
		],
		# ------------------------------------------------------------------
		# Driver (ERPNext standard). Already carries licence no / expiry /
		# categories / employee link — no need to duplicate onto Employee.
		# ------------------------------------------------------------------
		"Driver": [
			{
				"fieldname": "custom_assigned_vehicle",
				"label": "Assigned Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"insert_after": "employee",
			},
			{
				"fieldname": "custom_sync_payroll_cost_center",
				"label": "Post Salary to Vehicle Cost Center",
				"fieldtype": "Check",
				"default": "1",
				"insert_after": "custom_assigned_vehicle",
				"description": "Rewrites the driver's active Salary Structure Assignment payroll cost centers to the assigned vehicle.",
			},
		],
		# ------------------------------------------------------------------
		# Vehicle Log (HRMS standard, submittable). Extended into a full
		# maintenance log instead of building a parallel doctype.
		# ------------------------------------------------------------------
		"Vehicle Log": [
			{
				"fieldname": "custom_log_type",
				"label": "Log Type",
				"fieldtype": "Select",
				"options": "Refuelling\nPreventive Maintenance\nBreakdown Repair\nScheduled Service\nTyre Replacement\nOther",
				"default": "Refuelling",
				"in_list_view": 1,
				"in_standard_filter": 1,
				"insert_after": "employee",
			},
			{
				"fieldname": "custom_maintenance_section",
				"label": "Maintenance",
				"fieldtype": "Section Break",
				"insert_after": "service_detail",
				"depends_on": "eval:doc.custom_log_type && doc.custom_log_type!='Refuelling'",
			},
			{
				"fieldname": "custom_workshop",
				"label": "Workshop / Vendor",
				"fieldtype": "Data",
				"insert_after": "custom_maintenance_section",
			},
			{
				"fieldname": "custom_labour_cost",
				"label": "Labour Cost",
				"fieldtype": "Currency",
				"insert_after": "custom_workshop",
			},
			{
				"fieldname": "custom_stock_entry",
				"label": "Spare Parts Stock Entry",
				"fieldtype": "Link",
				"options": "Stock Entry",
				"read_only": 1,
				"insert_after": "custom_labour_cost",
			},
			{
				"fieldname": "custom_maint_col_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_stock_entry",
			},
			{
				"fieldname": "custom_total_cost",
				"label": "Total Cost",
				"fieldtype": "Currency",
				"read_only": 1,
				"insert_after": "custom_maint_col_break",
			},
			{
				"fieldname": "custom_next_service_due",
				"label": "Next Service Due",
				"fieldtype": "Date",
				"insert_after": "custom_total_cost",
			},
			{
				"fieldname": "custom_next_service_odometer",
				"label": "Next Service Odometer",
				"fieldtype": "Int",
				"insert_after": "custom_next_service_due",
			},
			{
				"fieldname": "custom_cost_center",
				"label": "Cost Center",
				"fieldtype": "Link",
				"options": "Cost Center",
				"read_only": 1,
				"fetch_from": "license_plate.custom_cost_center",
				"insert_after": "custom_next_service_odometer",
			},
		],
		# ------------------------------------------------------------------
		# Delivery Trip (ERPNext standard, submittable) used as the Trip Sheet.
		# ------------------------------------------------------------------
		"Delivery Trip": [
			{
				"fieldname": "custom_trip_section",
				"label": "Trip Details",
				"fieldtype": "Section Break",
				"insert_after": "vehicle",
			},
			{
				"fieldname": "custom_sales_order",
				"label": "Sales Order",
				"fieldtype": "Link",
				"options": "Sales Order",
				"allow_on_submit": 1,
				"allow_on_submit": 1,
				"read_only": 1,
				"description": "Set automatically from the trip rows when they all belong to one order.",
				"insert_after": "custom_trip_section",
			},
			{
				"fieldname": "custom_delivery_location",
				"label": "Delivery Location / Site",
				"fieldtype": "Data",
				"allow_on_submit": 1,
				"in_standard_filter": 1,
				"description": "One trip serves one site. Items for another site belong on their own trip.",
				"insert_after": "custom_sales_order",
			},
			{
				"fieldname": "custom_loading_warehouse",
				"label": "Loading Warehouse",
				"fieldtype": "Link",
				"options": "Warehouse",
				"description": "Default source for every row. A row may override it.",
				"insert_after": "custom_delivery_location",
			},
			# Legacy single-item fields. Kept (never dropped — dropping loses the
			# column) but now read-only summaries derived from the trip rows.
			{
				"fieldname": "custom_item",
				"label": "Item",
				"fieldtype": "Link",
				"options": "Item",
				"read_only": 1,
				"allow_on_submit": 1,
				"description": "Summary only — set when the trip carries a single item.",
				"insert_after": "custom_loading_warehouse",
			},
			{
				"fieldname": "custom_planned_qty",
				"label": "Total Planned Qty",
				"fieldtype": "Float",
				"read_only": 1,
				"allow_on_submit": 1,
				"insert_after": "custom_item",
			},
			{
				"fieldname": "custom_delivered_qty",
				"label": "Total Delivered Qty",
				"fieldtype": "Float",
				"read_only": 1,
				"allow_on_submit": 1,
				"insert_after": "custom_planned_qty",
			},
			{
				"fieldname": "custom_trip_col_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_loading_warehouse",
			},
			{
				"fieldname": "custom_trip_type",
				"label": "Transport Type",
				"fieldtype": "Select",
				"options": "Company Vehicle\nExternal Transport",
				"default": "Company Vehicle",
				"insert_after": "custom_trip_col_break",
			},
			{
				"fieldname": "custom_external_transporter",
				"label": "External Transporter",
				"fieldtype": "Link",
				"options": "Supplier",
				"depends_on": "eval:doc.custom_trip_type=='External Transport'",
				"insert_after": "custom_trip_type",
			},
			# Where the cement comes from. "Direct from Supplier" books a Purchase
			# Receipt straight onto the vehicle warehouse at Loading instead of a
			# Material Transfer from the yard — stock never touches Main Store.
			{
				"fieldname": "custom_supply_source",
				"label": "Supply Source",
				"fieldtype": "Select",
				"options": "Own Warehouse\nDirect from Supplier",
				"default": "Own Warehouse",
				"in_standard_filter": 1,
				"insert_after": "custom_external_transporter",
			},
			{
				"fieldname": "custom_supplier",
				"label": "Supplier",
				"fieldtype": "Link",
				"options": "Supplier",
				"depends_on": "eval:doc.custom_supply_source=='Direct from Supplier'",
				"mandatory_depends_on": "eval:doc.custom_supply_source=='Direct from Supplier'",
				"insert_after": "custom_target_warehouse",
			},
			{
				"fieldname": "custom_purchase_order",
				"label": "Purchase Order",
				"fieldtype": "Link",
				"options": "Purchase Order",
				"allow_on_submit": 1,
				"depends_on": "eval:doc.custom_supply_source=='Direct from Supplier' || doc.custom_purchase_order",
				"description": "Supplier order this trip collects against. Must be submitted before the trip can be loaded.",
				"insert_after": "custom_supplier",
			},
			# Where the trip ends. Customer (the normal case) or one of our own
			# warehouses (inbound from the supplier: no DN, no POD).
			{
				"fieldname": "custom_destination_type",
				"label": "Destination",
				"fieldtype": "Select",
				"options": "Customer\nOwn Warehouse\nDecide After Loading",
				"default": "Customer",
				"in_standard_filter": 1,
				"in_list_view": 1,
				"allow_on_submit": 1,
				"description": "Decide After Loading (direct-supply trips only): collect from the supplier first, choose customer or own warehouse while the truck is on the road.",
				"insert_after": "custom_supply_source",
			},
			# Where this trip came from — filterable in the list view.
			{
				"fieldname": "custom_trip_source",
				"label": "Source",
				"fieldtype": "Select",
				"options": "\nSales Order\nPurchase Order\nManual",
				"read_only": 1,
				"no_copy": 1,
				"in_standard_filter": 1,
				"in_list_view": 1,
				"insert_after": "custom_destination_type",
			},
			{
				"fieldname": "custom_target_warehouse",
				"label": "Target Warehouse",
				"fieldtype": "Link",
				"options": "Warehouse",
				"allow_on_submit": 1,
				"depends_on": "eval:doc.custom_destination_type=='Own Warehouse'",
				"mandatory_depends_on": "eval:doc.custom_destination_type=='Own Warehouse'",
				"description": "Yard warehouse the cement is unloaded into at Delivered.",
				"insert_after": "custom_destination_type",
			},
			{
				"fieldname": "custom_purchase_receipt",
				"label": "Purchase Receipt",
				"fieldtype": "Link",
				"options": "Purchase Receipt",
				"read_only": 1,
				"allow_on_submit": 1,
				"no_copy": 1,
				"depends_on": "custom_purchase_receipt",
				"insert_after": "custom_purchase_order",
			},
			{
				"fieldname": "custom_starting_odometer",
				"label": "Starting Odometer",
				"fieldtype": "Int",
				"insert_after": "custom_purchase_receipt",
			},
			{
				"fieldname": "custom_ending_odometer",
				"label": "Ending Odometer",
				"fieldtype": "Int",
				"allow_on_submit": 1,
				"insert_after": "custom_starting_odometer",
			},
			{
				"fieldname": "custom_transportation_cost",
				"label": "Transportation Charge",
				"fieldtype": "Currency",
				"allow_on_submit": 1,
				"insert_after": "custom_ending_odometer",
				"description": "Billable freight for this trip. Posts to Transportation Revenue against the vehicle cost center.",
			},
			{
				"fieldname": "custom_transportation_item",
				"label": "Transportation Charge Item",
				"fieldtype": "Link",
				"options": "Item",
				"allow_on_submit": 1,
				"insert_after": "custom_transportation_cost",
				"description": "Overrides the default in Thameen Fleet Settings. Leave blank to use the default.",
			},
			{
				"fieldname": "custom_cost_center",
				"label": "Cost Center",
				"fieldtype": "Link",
				"options": "Cost Center",
				"read_only": 1,
				"fetch_from": "vehicle.custom_cost_center",
				"insert_after": "custom_transportation_item",
			},
			{
				"fieldname": "custom_items_section",
				"label": "Trip Items",
				"fieldtype": "Section Break",
				"insert_after": "custom_cost_center",
			},
			{
				"fieldname": "custom_trip_items",
				"label": "Trip Items",
				"fieldtype": "Table",
				"options": "Delivery Trip Item",
				# Editable after submit so the driver's actual delivered qty can be
				# recorded. Row add/remove and re-planning are blocked in
				# ThameenDeliveryTrip.before_update_after_submit.
				"allow_on_submit": 1,
				"insert_after": "custom_items_section",
			},
			{
				"fieldname": "custom_pod_section",
				"label": "Proof of Delivery",
				"fieldtype": "Section Break",
				"insert_after": "delivery_stops",
			},
			{
				"fieldname": "custom_pod_documents",
				"label": "POD Documents",
				"fieldtype": "Table",
				"options": "Trip POD Document",
				"allow_on_submit": 1,
				"insert_after": "custom_pod_section",
			},
			{
				"fieldname": "custom_pod_received",
				"label": "POD Complete",
				"fieldtype": "Check",
				"read_only": 1,
				"allow_on_submit": 1,
				"insert_after": "custom_pod_documents",
			},
		],
		# ------------------------------------------------------------------
		"Delivery Note": [
			{
				"fieldname": "custom_delivery_trip",
				"label": "Delivery Trip",
				"fieldtype": "Link",
				"options": "Delivery Trip",
				"insert_after": "customer",
			},
			{
				"fieldname": "custom_vehicle",
				"label": "Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"insert_after": "custom_delivery_trip",
			},
			{
				"fieldname": "custom_driver_link",
				"label": "Driver",
				"fieldtype": "Link",
				"options": "Driver",
				"insert_after": "custom_vehicle",
			},
			{
				"fieldname": "custom_transportation_amount",
				"label": "Transportation Amount",
				"fieldtype": "Currency",
				"insert_after": "custom_driver_link",
				"description": "This note's share of the trip freight, apportioned by delivered value.",
			},
			{
				"fieldname": "custom_transportation_item",
				"label": "Transportation Charge Item",
				"fieldtype": "Link",
				"options": "Item",
				"read_only": 1,
				"insert_after": "custom_transportation_amount",
			},
		],
		"Sales Order": [
			{
				"fieldname": "custom_customer_requirement",
				"label": "Customer Requirement",
				"fieldtype": "Link",
				"options": "Customer Requirement",
				"read_only": 1,
				"insert_after": "customer_name",
			},
			{
				"fieldname": "custom_delivery_location",
				"label": "Delivery Location / Site",
				"fieldtype": "Data",
				"description": "Default site for the order. Rows may point at a different site.",
				"insert_after": "custom_customer_requirement",
			},
		],
		"Sales Order Item": [
			{
				"fieldname": "custom_delivery_location",
				"label": "Delivery Location / Site",
				"fieldtype": "Data",
				"in_list_view": 1,
				"columns": 2,
				"description": "Trips are planned one per location. Blank falls back to the order's location.",
				"insert_after": "warehouse",
			},
		],
		"Sales Invoice": [
			{
				"fieldname": "custom_billing_section",
				"label": "Consolidated Billing",
				"fieldtype": "Section Break",
				"insert_after": "due_date",
				"collapsible": 1,
			},
			{
				"fieldname": "custom_is_consolidated_run",
				"label": "Consolidated Monthly Invoice",
				"fieldtype": "Check",
				"insert_after": "custom_billing_section",
			},
			{
				"fieldname": "custom_billing_from_date",
				"label": "Billing Period From",
				"fieldtype": "Date",
				"insert_after": "custom_is_consolidated_run",
			},
			{
				"fieldname": "custom_billing_col_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_billing_from_date",
			},
			{
				"fieldname": "custom_billing_to_date",
				"label": "Billing Period To",
				"fieldtype": "Date",
				"insert_after": "custom_billing_col_break",
			},
		],
		"Sales Invoice Item": [
			{
				"fieldname": "custom_delivery_trip",
				"label": "Delivery Trip",
				"fieldtype": "Link",
				"options": "Delivery Trip",
				"insert_after": "delivery_note",
			},
			{
				"fieldname": "custom_vehicle",
				"label": "Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"insert_after": "custom_delivery_trip",
			},
			{
				"fieldname": "custom_is_transportation_row",
				"label": "Transportation Revenue Row",
				"fieldtype": "Check",
				"insert_after": "custom_vehicle",
			},
		],
		# ------------------------------------------------------------------
		# Purchase side – expected discount / credit note reconciliation
		# ------------------------------------------------------------------
		"Purchase Order Item": [
			{
				"fieldname": "custom_expected_discount_amount",
				"label": "Expected Discount (Credit Note)",
				"fieldtype": "Currency",
				"insert_after": "rate",
			},
			{
				"fieldname": "custom_agreed_net_price",
				"label": "Agreed Net Price",
				"fieldtype": "Currency",
				"read_only": 1,
				"insert_after": "custom_expected_discount_amount",
			},
		],
		"Purchase Invoice Item": [
			{
				"fieldname": "custom_expected_discount_amount",
				"label": "Expected Discount (Credit Note)",
				"fieldtype": "Currency",
				"insert_after": "rate",
			},
			{
				"fieldname": "custom_agreed_net_price",
				"label": "Agreed Net Price",
				"fieldtype": "Currency",
				"read_only": 1,
				"insert_after": "custom_expected_discount_amount",
			},
			{
				"fieldname": "custom_credit_note_status",
				"label": "Credit Note Status",
				"fieldtype": "Select",
				"options": CREDIT_NOTE_STATUS,
				"default": "Not Applicable",
				"read_only": 1,
				"insert_after": "custom_agreed_net_price",
			},
			{
				"fieldname": "custom_credit_note_received",
				"label": "Credit Note Received",
				"fieldtype": "Currency",
				"read_only": 1,
				"insert_after": "custom_credit_note_status",
			},
			{
				"fieldname": "custom_vehicle",
				"label": "Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"insert_after": "cost_center",
			},
		],
		"Purchase Invoice": vehicle_expense_block,
		"Journal Entry": vehicle_expense_block,
		"Journal Entry Account": [
			{
				"fieldname": "custom_vehicle",
				"label": "Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"insert_after": "cost_center",
			},
		],
		"Expense Claim": vehicle_expense_block,
		"Expense Claim Detail": [
			{
				"fieldname": "custom_vehicle",
				"label": "Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"insert_after": "cost_center",
			},
		],
		# ------------------------------------------------------------------
		"Asset": [
			{
				"fieldname": "custom_vehicle",
				"label": "Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"insert_after": "asset_category",
			},
		],
		"Warehouse": [
			{
				"fieldname": "custom_is_vehicle_warehouse",
				"label": "Is Vehicle Warehouse",
				"fieldtype": "Check",
				"insert_after": "is_group",
			},
			{
				"fieldname": "custom_linked_vehicle",
				"label": "Linked Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"depends_on": "custom_is_vehicle_warehouse",
				"insert_after": "custom_is_vehicle_warehouse",
			},
		],
		"Stock Entry": [
			{
				"fieldname": "custom_delivery_trip",
				"label": "Delivery Trip",
				"fieldtype": "Link",
				"options": "Delivery Trip",
				"insert_after": "purpose",
			},
			{
				"fieldname": "custom_vehicle",
				"label": "Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"insert_after": "custom_delivery_trip",
			},
			{
				"fieldname": "custom_vehicle_load_type",
				"label": "Vehicle Load Type",
				"fieldtype": "Select",
				"options": "\nTrip Loading\nManual Load\nManual Unload\nInbound Unload",
				"read_only": 1,
				"in_standard_filter": 1,
				"insert_after": "custom_vehicle",
			},
		],
		"Purchase Order": [
			{
				"fieldname": "custom_delivery_trip",
				"label": "Delivery Trip",
				"fieldtype": "Link",
				"options": "Delivery Trip",
				"read_only": 1,
				"insert_after": "supplier_name",
				"description": "Raised from a Delivery Trip — either to cover a stock shortfall or for a direct-from-supplier delivery.",
			},
		],
		"Purchase Receipt": [
			{
				"fieldname": "custom_delivery_trip",
				"label": "Delivery Trip",
				"fieldtype": "Link",
				"options": "Delivery Trip",
				"read_only": 1,
				"insert_after": "supplier_name",
			},
			{
				"fieldname": "custom_vehicle",
				"label": "Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"read_only": 1,
				"insert_after": "custom_delivery_trip",
			},
		],
		"Material Request": [
			{
				"fieldname": "custom_delivery_trip",
				"label": "Delivery Trip",
				"fieldtype": "Link",
				"options": "Delivery Trip",
				"read_only": 1,
				"insert_after": "material_request_type",
			},
			{
				"fieldname": "custom_vehicle",
				"label": "Vehicle",
				"fieldtype": "Link",
				"options": "Vehicle",
				"read_only": 1,
				"insert_after": "custom_delivery_trip",
			},
		],
	}


def install_customisations():
	fields = get_custom_fields()

	# Only install for doctypes that actually exist on this site, so a site
	# without hrms installed does not blow up the whole migrate.
	installed = {}
	for doctype, defs in fields.items():
		if not defs:
			continue
		if not frappe.db.exists("DocType", doctype):
			frappe.log_error(
				f"Thameen ERP: skipping custom fields for missing DocType {doctype}",
				"Thameen ERP Install",
			)
			continue
		installed[doctype] = defs

	create_custom_fields(installed, update=True)

	_apply_property_setters()
	_unrequire_stray_custom_fields()
	frappe.clear_cache()


def _unrequire_stray_custom_fields():
	"""Sites customised by hand sometimes carry a Custom Field like
	`sales_order` on Delivery Trip marked mandatory. Inbound trips (Supplier →
	My Warehouse) have no Sales Order by design, so any such flag breaks them
	with a MandatoryError. Clear it wherever it is found — the real rule
	(customer trips need an SO per row at submit) lives in
	ThameenDeliveryTrip, not in field flags."""
	stray = frappe.get_all(
		"Custom Field",
		filters={
			"dt": ("in", ["Delivery Trip", "Delivery Trip Item"]),
			"fieldname": ("in", ["sales_order", "so_detail", "customer", "custom_sales_order"]),
			"reqd": 1,
		},
		pluck="name",
	)
	for name in stray:
		frappe.db.set_value("Custom Field", name, "reqd", 0, update_modified=False)
	if stray:
		frappe.clear_cache(doctype="Delivery Trip")


def _apply_property_setters():
	"""Small UX tweaks on standard doctypes."""
	setters = [
		# Delivery Trip status gains the states the cement flow needs.
		(
			"Delivery Trip",
			"status",
			"options",
			"Draft\nScheduled\nLoading\nIn Transit\nDelivered\nPOD Pending\nCompleted\nCancelled",
			"Text",
		),
		# `total_distance` is recomputed from the odometer readings, which are
		# entered after the trip is submitted. Without this, validate() raises
		# UpdateAfterSubmitError on every post-submit save.
		("Delivery Trip", "total_distance", "allow_on_submit", "1", "Check"),
		# This app dispatches against Sales Order lines, not Google-Maps stops.
		# Standard ERPNext makes `delivery_stops` mandatory, which blocked every
		# trip that had no customer Address record.
		("Delivery Trip", "delivery_stops", "reqd", "0", "Check"),
		# A trip is planned before dispatch picks a truck. Submission still
		# requires a vehicle — enforced in ThameenDeliveryTrip.validate.
		("Delivery Trip", "vehicle", "reqd", "0", "Check"),
		# A trip row only has a Sales Order when the trip goes to a customer.
		# Inbound trips (Supplier -> My Warehouse) have none, and some sites
		# had these marked mandatory via Customize Form or an old doctype
		# version, which broke every inbound trip with a MandatoryError.
		# The real rule (customer trips need an SO per row at submit) is
		# enforced in ThameenDeliveryTrip._validate_trip_items instead.
		("Delivery Trip Item", "sales_order", "reqd", "0", "Check"),
		("Delivery Trip Item", "so_detail", "reqd", "0", "Check"),
		("Delivery Trip", "custom_sales_order", "reqd", "0", "Check"),
		("Delivery Trip", "sales_order", "reqd", "0", "Check"),
		("Delivery Trip", "customer", "reqd", "0", "Check"),
		("Vehicle", "license_plate", "label", "Vehicle Number", "Data"),
		# The list used to show the driver's name as the row title. Dispatch
		# wants the trip ID, with status / vehicle / driver as columns and
		# filters beside it.
		("Delivery Trip", None, "title_field", "", "Data"),
		("Delivery Trip", None, "show_title_field_in_link", "0", "Check"),
		("Delivery Trip", "vehicle", "in_list_view", "1", "Check"),
		("Delivery Trip", "vehicle", "in_standard_filter", "1", "Check"),
		("Delivery Trip", "driver", "in_list_view", "1", "Check"),
		("Delivery Trip", "driver", "in_standard_filter", "1", "Check"),
		("Delivery Trip", "driver_name", "in_list_view", "0", "Check"),
		("Delivery Trip", "status", "in_standard_filter", "1", "Check"),
		("Delivery Trip", "departure_time", "in_list_view", "1", "Check"),
		("Delivery Trip", None, "search_fields", "vehicle,driver,status", "Data"),
	]
	for doctype, fieldname, prop, value, prop_type in setters:
		try:
			make_property_setter(
				doctype, fieldname, prop, value, prop_type, for_doctype=not fieldname
			)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "Thameen ERP Property Setter")


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

ROLES = [
	"Fleet Manager",
	"Operation Manager",
	"Sales Approver",
	"Finance Approver",
	"Credit Note Officer",
]


def create_roles():
	for role in ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc(
				{"doctype": "Role", "role_name": role, "desk_access": 1}
			).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Workflow for Customer Requirement
# ---------------------------------------------------------------------------


def create_workflow():
	if not frappe.db.exists("DocType", "Customer Requirement"):
		return
	if frappe.db.exists("Workflow", "Customer Requirement Approval"):
		return

	for state, style in [
		("Draft", "Danger"),
		("Pending Sales Approval", "Warning"),
		("Pending Finance Approval", "Warning"),
		("Approved", "Success"),
		("Rejected", "Danger"),
	]:
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc(
				{"doctype": "Workflow State", "workflow_state_name": state, "style": style}
			).insert(ignore_permissions=True)

	for action in ["Submit for Approval", "Approve", "Reject", "Return to Draft"]:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc(
				{"doctype": "Workflow Action Master", "workflow_action_name": action}
			).insert(ignore_permissions=True)

	wf = frappe.get_doc(
		{
			"doctype": "Workflow",
			"workflow_name": "Customer Requirement Approval",
			"document_type": "Customer Requirement",
			"workflow_state_field": "workflow_state",
			"is_active": 1,
			"send_email_alert": 1,
			"states": [
				{
					"state": "Draft",
					"doc_status": "0",
					"allow_edit": "Sales User",
					"update_field": "status",
					"update_value": "Draft",
				},
				{
					"state": "Pending Sales Approval",
					"doc_status": "1",
					"allow_edit": "Sales Approver",
					"update_field": "status",
					"update_value": "Pending Sales Approval",
				},
				{
					"state": "Pending Finance Approval",
					"doc_status": "1",
					"allow_edit": "Finance Approver",
					"update_field": "status",
					"update_value": "Pending Finance Approval",
				},
				{
					"state": "Approved",
					"doc_status": "1",
					"allow_edit": "Finance Approver",
					"update_field": "status",
					"update_value": "Approved",
				},
				{
					"state": "Rejected",
					"doc_status": "1",
					"allow_edit": "Sales Approver",
					"update_field": "status",
					"update_value": "Rejected",
				},
			],
			"transitions": [
				{
					"state": "Draft",
					"action": "Submit for Approval",
					"next_state": "Pending Sales Approval",
					"allowed": "Sales User",
					"allow_self_approval": 1,
				},
				{
					"state": "Pending Sales Approval",
					"action": "Approve",
					"next_state": "Pending Finance Approval",
					"allowed": "Sales Approver",
					"allow_self_approval": 0,
				},
				{
					"state": "Pending Sales Approval",
					"action": "Reject",
					"next_state": "Rejected",
					"allowed": "Sales Approver",
					"allow_self_approval": 1,
				},
				{
					"state": "Pending Finance Approval",
					"action": "Approve",
					"next_state": "Approved",
					"allowed": "Finance Approver",
					"allow_self_approval": 0,
				},
				{
					"state": "Pending Finance Approval",
					"action": "Reject",
					"next_state": "Rejected",
					"allowed": "Finance Approver",
					"allow_self_approval": 1,
				},
			],
		}
	)
	wf.insert(ignore_permissions=True)
