const STAGES = ["Scheduled", "Loading", "In Transit", "Delivered", "POD Pending", "Completed"];

const NEXT_STATUS = {
	Scheduled: "Loading",
	Loading: "In Transit",
	"In Transit": "Delivered",
	Delivered: "POD Pending",
	"POD Pending": "Completed",
};

const STAGE_HINT = {
	Loading: __("This raises a Material Transfer from the loading warehouse onto the vehicle warehouse."),
	Delivered: __("This raises the Delivery Note(s) from the vehicle warehouse against the Sales Order."),
	Completed: __("This releases the vehicle and closes the Sales Order if it is fully delivered."),
};

frappe.ui.form.on("Delivery Trip", {
	setup(frm) {
		frm.set_query("custom_loading_warehouse", () => ({
			filters: { is_group: 0, company: frm.doc.company, custom_is_vehicle_warehouse: 0 },
		}));

		frm.set_query("custom_sales_order", () => ({
			filters: { docstatus: 1, status: ["not in", ["Closed", "Completed", "Cancelled"]] },
		}));

		frm.set_query("source_warehouse", "custom_trip_items", () => ({
			filters: { is_group: 0, company: frm.doc.company, custom_is_vehicle_warehouse: 0 },
		}));

		frm.set_query("sales_order", "custom_trip_items", () => ({
			filters: { docstatus: 1, status: ["not in", ["Closed", "Completed", "Cancelled"]] },
		}));
		frm.set_query("vehicle", () => ({
			 filters: { custom_status: "Available" },
	 }));

		// Freight is a service charge — a stock item here is rejected server-side.
		frm.set_query("custom_transportation_item", () => ({
			filters: { is_stock_item: 0, disabled: 0 },
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			add_get_items_button(frm);
			return;
		}
		if (frm.doc.docstatus !== 1) return;

		add_status_buttons(frm);
		show_progress(frm);
		add_view_buttons(frm);
	},

	vehicle(frm) {
		if (!frm.doc.vehicle) return;
		frappe.db
			.get_value("Vehicle", frm.doc.vehicle, [
				"custom_assigned_driver",
				"custom_capacity",
				"last_odometer",
			])
			.then(({ message }) => {
				if (!message) return;
				if (message.custom_assigned_driver && !frm.doc.driver) {
					frm.set_value("driver", message.custom_assigned_driver);
				}
				if (message.last_odometer && !frm.doc.custom_starting_odometer) {
					frm.set_value("custom_starting_odometer", message.last_odometer);
				}
				const planned = total_planned(frm);
				if (message.custom_capacity && planned > message.custom_capacity) {
					frm.dashboard.add_comment(
						__("Planned {0} exceeds this vehicle's capacity of {1}.", [
							planned,
							message.custom_capacity,
						]),
						"orange",
						true
					);
				}
			});
	},

	custom_ending_odometer(frm) {
		const { custom_starting_odometer: s, custom_ending_odometer: e } = frm.doc;
		if (s && e && e >= s) frm.set_value("total_distance", e - s);
	},
});

frappe.ui.form.on("Delivery Trip Item", {
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item_code) return;
		frappe.db.get_value("Item", row.item_code, ["item_name", "stock_uom"]).then(({ message }) => {
			if (!message) return;
			frappe.model.set_value(cdt, cdn, "item_name", message.item_name);
			frappe.model.set_value(cdt, cdn, "stock_uom", message.stock_uom);
			if (!row.uom) frappe.model.set_value(cdt, cdn, "uom", message.stock_uom);
		});
	},

	qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, "amount", flt(row.qty) * flt(row.rate));
		check_stock(frm, row);
	},

	custom_trip_items_remove(frm) {
		frm.refresh_field("custom_trip_items");
	},
});

function total_planned(frm) {
	return (frm.doc.custom_trip_items || []).reduce((sum, row) => sum + flt(row.qty), 0);
}

function check_stock(frm, row) {
	if (!row.item_code || !row.qty) return;
	frappe.call({
		method: "thameen_erp.api.check_stock_availability",
		args: { item_code: row.item_code, qty: row.qty },
		callback({ message }) {
			if (message && !message.sufficient) {
				frappe.show_alert({
					message: __("{0}: available {1}, requested {2}.", [
						row.item_code,
						message.available_qty,
						message.requested_qty,
					]),
					indicator: "orange",
				});
			}
		},
	});
}

function add_get_items_button(frm) {
	frm.add_custom_button(
		__("Get Items from Sales Order"),
		() => {
			const dialog = new frappe.ui.Dialog({
				title: __("Pull Pending Items"),
				fields: [
					{
						fieldname: "sales_order",
						fieldtype: "Link",
						options: "Sales Order",
						label: __("Sales Order"),
						reqd: 1,
						default: frm.doc.custom_sales_order,
						get_query: () => ({
							filters: {
								docstatus: 1,
								status: ["not in", ["Closed", "Completed", "Cancelled"]],
							},
						}),
					},
					{
						fieldname: "delivery_location",
						fieldtype: "Data",
						label: __("Delivery Location / Site"),
						default: frm.doc.custom_delivery_location,
						description: __("Leave blank to pull every pending row on the order."),
					},
				],
				primary_action_label: __("Get Items"),
				primary_action(values) {
					frappe.call({
						method: "thameen_erp.overrides.sales_order.get_trip_rows",
						args: values,
						freeze: true,
						callback({ message }) {
							dialog.hide();
							if (!message || !message.length) {
								frappe.msgprint(
									__("Nothing pending on that order for this location.")
								);
								return;
							}
							apply_rows(frm, message);
						},
					});
				},
			});
			dialog.show();
		},
		__("Get Items From")
	).addClass("btn-primary");
}

function apply_rows(frm, rows) {
	const locations = new Set(rows.map((r) => (r.delivery_location || "").trim()).filter(Boolean));

	if (locations.size > 1) {
		frappe.msgprint({
			title: __("Multiple Locations"),
			indicator: "orange",
			message: __(
				"Those rows go to {0} different sites: {1}. One trip serves one site — pull them one location at a time, or use Create > Delivery Trips on the Sales Order to plan them all at once.",
				[locations.size, Array.from(locations).join(", ")]
			),
		});
		return;
	}

	rows.forEach((row) => {
		const child = frm.add_child("custom_trip_items");
		Object.assign(child, {
			sales_order: row.sales_order,
			so_detail: row.so_detail,
			item_code: row.item_code,
			item_name: row.item_name,
			qty: row.qty,
			uom: row.uom,
			conversion_factor: row.conversion_factor,
			stock_uom: row.stock_uom,
			rate: row.rate,
			amount: row.amount,
			source_warehouse: row.source_warehouse,
			delivery_location: row.delivery_location,
		});
	});

	if (locations.size === 1 && !frm.doc.custom_delivery_location) {
		frm.set_value("custom_delivery_location", Array.from(locations)[0]);
	}

	frm.refresh_field("custom_trip_items");
	frappe.show_alert({ message: __("{0} row(s) added.", [rows.length]), indicator: "green" });
}

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

		const advance = () =>
			frappe.call({
				method: "thameen_erp.overrides.delivery_trip.set_trip_status",
				args: { trip: frm.doc.name, status: next },
				freeze: true,
				freeze_message: __("Updating trip…"),
				callback: () => frm.reload_doc(),
			});

		if (STAGE_HINT[next]) {
			frappe.confirm(STAGE_HINT[next] + "<br><br>" + __("Continue?"), advance);
		} else {
			advance();
		}
	});
}

function add_view_buttons(frm) {
	const orders = Array.from(
		new Set((frm.doc.custom_trip_items || []).map((row) => row.sales_order).filter(Boolean))
	);

	orders.forEach((order) => {
		frm.add_custom_button(
			order,
			() => frappe.set_route("Form", "Sales Order", order),
			__("Sales Orders")
		);
	});

	const notes = Array.from(
		new Set((frm.doc.custom_trip_items || []).map((row) => row.delivery_note).filter(Boolean))
	);

	notes.forEach((note) => {
		frm.add_custom_button(
			note,
			() => frappe.set_route("Form", "Delivery Note", note),
			__("Delivery Notes")
		);
	});
}

function show_progress(frm) {
	const idx = STAGES.indexOf(frm.doc.status);
	if (idx < 0) return;
	frm.dashboard.add_progress(__("Trip Progress"), [
		{
			title: frm.doc.status,
			width: ((idx + 1) / STAGES.length) * 100 + "%",
			progress_class:
				frm.doc.status === "Completed" ? "progress-bar-success" : "progress-bar-info",
		},
	]);
}
