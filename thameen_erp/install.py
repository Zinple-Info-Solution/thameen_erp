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


def after_migrate():
	install_customisations()
	create_roles()
	create_workflow()


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
				"fieldname": "custom_fleet_col_break",
				"fieldtype": "Column Break",
				"insert_after": "custom_capacity_uom",
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
				"insert_after": "custom_trip_section",
			},
			{
				"fieldname": "custom_item",
				"label": "Item",
				"fieldtype": "Link",
				"options": "Item",
				"insert_after": "custom_sales_order",
			},
			{
				"fieldname": "custom_planned_qty",
				"label": "Planned Qty",
				"fieldtype": "Float",
				"insert_after": "custom_item",
			},
			{
				"fieldname": "custom_delivered_qty",
				"label": "Delivered Qty",
				"fieldtype": "Float",
				"read_only": 1,
				"insert_after": "custom_planned_qty",
			},
			{
				"fieldname": "custom_loading_warehouse",
				"label": "Loading Warehouse",
				"fieldtype": "Link",
				"options": "Warehouse",
				"insert_after": "custom_delivered_qty",
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
			{
				"fieldname": "custom_starting_odometer",
				"label": "Starting Odometer",
				"fieldtype": "Int",
				"insert_after": "custom_external_transporter",
			},
			{
				"fieldname": "custom_ending_odometer",
				"label": "Ending Odometer",
				"fieldtype": "Int",
				"insert_after": "custom_starting_odometer",
			},
			{
				"fieldname": "custom_transportation_cost",
				"label": "Transportation Charge",
				"fieldtype": "Currency",
				"insert_after": "custom_ending_odometer",
				"description": "Billable freight for this trip. Posts to Transportation Revenue against the vehicle cost center.",
			},
			{
				"fieldname": "custom_cost_center",
				"label": "Cost Center",
				"fieldtype": "Link",
				"options": "Cost Center",
				"read_only": 1,
				"fetch_from": "vehicle.custom_cost_center",
				"insert_after": "custom_transportation_cost",
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
				"insert_after": "custom_pod_section",
			},
			{
				"fieldname": "custom_pod_received",
				"label": "POD Complete",
				"fieldtype": "Check",
				"read_only": 1,
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
				"insert_after": "custom_customer_requirement",
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
	frappe.clear_cache()


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
		("Vehicle", "license_plate", "label", "Vehicle Number", "Data"),
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
