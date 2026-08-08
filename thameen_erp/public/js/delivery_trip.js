frappe.ui.form.on("Delivery Trip", {
	setup(frm) {
		frm.set_query("custom_loading_warehouse", () => ({
			filters: { is_group: 0, company: frm.doc.company, custom_is_vehicle_warehouse: 0 },
		}));
		frm.set_query("custom_sales_order", () => ({
			filters: { docstatus: 1, status: ["not in", ["Closed", "Completed", "Cancelled"]] },
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;
		add_status_buttons(frm);
		show_progress(frm);

		if (frm.doc.custom_sales_order) {
			frm.add_custom_button(__("Sales Order"), () =>
				frappe.set_route("Form", "Sales Order", frm.doc.custom_sales_order), __("View"));
		}
	},

	vehicle(frm) {
		if (!frm.doc.vehicle) return;
		frappe.db.get_value("Vehicle", frm.doc.vehicle,
			["custom_assigned_driver", "custom_capacity", "last_odometer", "custom_cost_center"])
			.then(({ message }) => {
				if (!message) return;
				if (message.custom_assigned_driver && !frm.doc.driver) {
					frm.set_value("driver", message.custom_assigned_driver);
				}
				if (message.last_odometer && !frm.doc.custom_starting_odometer) {
					frm.set_value("custom_starting_odometer", message.last_odometer);
				}
				if (message.custom_capacity && !frm.doc.custom_planned_qty) {
					frm.set_value("custom_planned_qty", message.custom_capacity);
				}
			});
	},

	custom_ending_odometer(frm) {
		const { custom_starting_odometer: s, custom_ending_odometer: e } = frm.doc;
		if (s && e && e >= s) frm.set_value("total_distance", e - s);
	},

	custom_item(frm) {
		if (!frm.doc.custom_item || !frm.doc.custom_planned_qty) return;
		frappe.call({
			method: "thameen_erp.api.check_stock_availability",
			args: { item_code: frm.doc.custom_item, qty: frm.doc.custom_planned_qty },
			callback({ message }) {
				if (message && !message.sufficient) {
					frappe.msgprint({
						title: __("Insufficient Stock"),
						indicator: "orange",
						message: __("Available {0}, requested {1}.",
							[message.available_qty, message.requested_qty]),
					});
				}
			},
		});
	},
});

const NEXT_STATUS = {
	Scheduled: "Loading",
	Loading: "In Transit",
	"In Transit": "Delivered",
	Delivered: "POD Pending",
	"POD Pending": "Completed",
};

function add_status_buttons(frm) {
	const next = NEXT_STATUS[frm.doc.status];
	if (!next) return;

	frm.page.set_primary_action(__("Mark {0}", [next]), () => {
		if (next === "Completed" && !frm.doc.custom_pod_received) {
			frappe.msgprint({
				title: __("POD Required"),
				indicator: "red",
				message: __("Attach at least one Proof of Delivery document first."),
			});
			return;
		}
		frappe.call({
			method: "thameen_erp.overrides.delivery_trip.set_trip_status",
			args: { trip: frm.doc.name, status: next },
			freeze: true,
			freeze_message: __("Updating trip…"),
			callback: () => frm.reload_doc(),
		});
	});
}

function show_progress(frm) {
	const stages = ["Scheduled", "Loading", "In Transit", "Delivered", "POD Pending", "Completed"];
	const idx = stages.indexOf(frm.doc.status);
	if (idx < 0) return;
	frm.dashboard.add_progress(__("Trip Progress"), [
		{ title: frm.doc.status, width: ((idx + 1) / stages.length) * 100 + "%",
		  progress_class: frm.doc.status === "Completed" ? "progress-bar-success" : "progress-bar-info" },
	]);
}
