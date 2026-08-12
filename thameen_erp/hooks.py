app_name = "thameen_erp"
app_title = "Thameen ERP"
app_publisher = "NextOraTrade"
app_description = "Cement distribution & fleet management layer on ERPNext + Frappe HR"
app_email = "dev@nextoratrade.com"
app_license = "mit"
required_apps = ["frappe/erpnext", "frappe/hrms"]

# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------
after_install = "thameen_erp.install.after_install"
after_migrate = "thameen_erp.install.after_migrate"

# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
doctype_js = {
	"Vehicle": "public/js/vehicle.js",
	"Delivery Trip": "public/js/delivery_trip.js",
	"Sales Order": "public/js/sales_order.js",
	"Sales Invoice": "public/js/sales_invoice.js",
	"Purchase Invoice": "public/js/purchase_invoice.js",
	"Journal Entry": "public/js/fleet_expense.js",
	"Expense Claim": "public/js/fleet_expense.js",
	"Driver": "public/js/driver.js",
}

doctype_list_js = {
	"Delivery Trip": "public/js/delivery_trip_list.js",
}

# ---------------------------------------------------------------------------
# Controller overrides
# ---------------------------------------------------------------------------
# Delivery Trip is a class override, not a doc_event. ERPNext's own
# DeliveryTrip.update_status() derives status from `delivery_stops[].visited`
# and runs *before* any doc_event hook, so a hook could never stop it from
# overwriting the cement lifecycle states. Owning the class is the only fix.
override_doctype_class = {
	"Delivery Trip": "thameen_erp.overrides.delivery_trip.ThameenDeliveryTrip",
}

# ---------------------------------------------------------------------------
# Document events
# ---------------------------------------------------------------------------
doc_events = {
	"Vehicle": {
		"validate": "thameen_erp.overrides.vehicle.validate",
		"after_insert": "thameen_erp.overrides.vehicle.after_insert",
		"on_update": "thameen_erp.overrides.vehicle.on_update",
	},
	"Driver": {
		"on_update": "thameen_erp.overrides.driver.on_update",
	},
	"Vehicle Log": {
		"validate": "thameen_erp.overrides.vehicle_log.validate",
		"on_submit": "thameen_erp.overrides.vehicle_log.on_submit",
		"on_cancel": "thameen_erp.overrides.vehicle_log.on_cancel",
	},
	"Sales Order": {
		"validate": "thameen_erp.overrides.sales_order.validate",
		"on_submit": "thameen_erp.overrides.sales_order.on_submit",
	},
	"Delivery Note": {
		"validate": "thameen_erp.overrides.delivery_note.validate",
		"on_submit": "thameen_erp.overrides.delivery_note.on_submit",
		"on_cancel": "thameen_erp.overrides.delivery_note.on_cancel",
	},
	"Purchase Order": {
		"validate": "thameen_erp.overrides.purchase.validate_expected_discount",
	},
	"Purchase Invoice": {
		"validate": "thameen_erp.overrides.purchase.validate_purchase_invoice",
		"on_submit": "thameen_erp.overrides.purchase.flag_expected_credit_notes",
	},
	"Journal Entry": {
		"validate": "thameen_erp.overrides.fleet_expense.set_cost_center_from_vehicle",
	},
	"Expense Claim": {
		"validate": "thameen_erp.overrides.fleet_expense.set_cost_center_from_vehicle",
	},
	"Sales Invoice": {
		"validate": "thameen_erp.overrides.sales_invoice.validate",
	},
}

# ---------------------------------------------------------------------------
# Scheduled tasks
# ---------------------------------------------------------------------------
scheduler_events = {
	"daily": [
		"thameen_erp.tasks.notify_expiring_vehicle_documents",
		"thameen_erp.tasks.notify_service_due",
		"thameen_erp.tasks.flag_overdue_credit_notes",
	],
	"hourly": [
		"thameen_erp.tasks.sync_vehicle_status",
	],
}

# ---------------------------------------------------------------------------
# Fixtures — only ship what cannot be generated in code
# ---------------------------------------------------------------------------
fixtures = [
	# Roles
	{
		"dt": "Role",
		"filters": [
			[
				"name",
				"in",
				[
					"Fleet Manager",
					"Operation Manager",
					"Sales Approver",
					"Finance Approver",
					"Credit Note Officer",
				],
			]
		],
	},

	# Workspaces
	{
		"dt": "Workspace",
		"filters": [
			[
				"name",
				"in",
				[
					"Purchase and Credit Notes",
					"Sales and Delivery",
					"Fleet Management",
				],
			]
		],
	},
]


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
permission_query_conditions = {
	"Delivery Trip": "thameen_erp.permissions.delivery_trip_query_conditions",
}

has_permission = {
	"Delivery Trip": "thameen_erp.permissions.delivery_trip_has_permission",
}
