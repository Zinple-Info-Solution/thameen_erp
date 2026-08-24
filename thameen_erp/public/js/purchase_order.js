// Purchase Order → Delivery Trips.
//
//   Supplier → My Warehouse   inbound; unloaded into the yard at Delivered
//   Supplier → Customer       direct; Delivery Note from the truck at Delivered
//
// The planner (trip_planner.js) splits the pending order by a truck's capacity
// into dated trips. Every qty / truck / date is editable before anything is
// created, and the plan can never exceed what the order still has pending.

frappe.ui.form.on("Purchase Order", {
	refresh(frm) {
		if (frm.doc.custom_delivery_trip) {
			frm.add_custom_button(frm.doc.custom_delivery_trip,
				() => frappe.set_route("Form", "Delivery Trip", frm.doc.custom_delivery_trip), __("View"));
		}
		if (frm.doc.docstatus !== 1) return;
		if (["Closed", "Cancelled", "Completed"].includes(frm.doc.status)) return;
		if (flt(frm.doc.per_received) >= 100) return;

		frm.add_custom_button(__("Delivery Trips"), () => open_po_planner(frm), __("Create")).addClass("btn-primary");
	},
});

function open_po_planner(frm) {
	frappe.call({
		method: "thameen_erp.overrides.po_trips.preview_po_trips",
		args: { purchase_order: frm.doc.name },
		freeze: true,
		callback({ message }) {
			if (!message) return;
			const pending = (message.lines || []).filter((l) => flt(l.pending) > 0);
			if (!pending.length) {
				frappe.msgprint(__("Everything on this order is already received or planned onto trips."));
				return;
			}
			build_po_dialog(frm, message, pending);
		},
	});
}

function build_po_dialog(frm, data, pending) {
	const dialog = new frappe.ui.Dialog({
		title: __("Plan Delivery Trips from {0}", [frm.doc.name]),
		size: "extra-large",
		fields: [
			{
				fieldname: "destination", fieldtype: "Select", label: __("Where is the cement going?"),
				options: "Own Warehouse\nCustomer\nDecide After Loading", default: "Own Warehouse", reqd: 1,
				onchange: () => toggle_destination(dialog),
			},
			{
				fieldname: "target_warehouse", fieldtype: "Link", options: "Warehouse", label: __("Into Warehouse"),
				default: data.default_warehouse,
				get_query: () => ({ filters: { is_group: 0, company: frm.doc.company, custom_is_vehicle_warehouse: 0 } }),
			},
			{
				fieldname: "sales_order", fieldtype: "Link", options: "Sales Order", label: __("Customer's Sales Order"), hidden: 1,
				get_query: () => ({
					filters: { docstatus: 1, status: ["not in", ["Closed", "Completed", "Cancelled"]], per_delivered: ["<", 100] },
				}),
				onchange: () => {
					const so = dialog.get_value("sales_order");
					if (!so) return;
					frappe.db.get_value("Sales Order", so, ["customer", "custom_delivery_location"]).then(({ message }) => {
						if (message && message.custom_delivery_location) dialog.set_value("delivery_location", message.custom_delivery_location);
					});
				},
			},
			{ fieldname: "delivery_location", fieldtype: "Data", label: __("Delivery Site"), hidden: 1 },
			{ fieldtype: "Column Break" },
			{
				fieldname: "vehicle", fieldtype: "Link", options: "Vehicle", label: __("Plan by truck"),
				description: __("Splits the order by this truck's capacity, one trip per day. Edit afterwards."),
				get_query: () => ({
					query: "thameen_erp.overrides.vehicle_stock.vehicle_query",
					filters: { custom_status: ["in", ["Available", "Assigned"]] },
				}),
				onchange: () => {
					const v = dialog.get_value("vehicle");
					if (v) thameen.trip_planner.auto_split(dialog, v, dialog.get_value("start_date"), dialog.get_value("days_between") || 1);
				},
			},
			{ fieldname: "start_date", fieldtype: "Date", label: __("First trip on"), default: data.schedule_date || frappe.datetime.get_today() },
			{ fieldname: "days_between", fieldtype: "Int", label: __("Days between trips"), default: 1 },
			{ fieldname: "transportation_charge", fieldtype: "Currency", label: __("Freight per trip"),
				description: __("Customer trips only — what the customer is charged per truckload."), hidden: 1 },
			{ fieldtype: "Section Break" },
			{ fieldtype: "HTML", fieldname: "plan" },
		],
		primary_action_label: __("Create Trips"),
		primary_action(values) {
			const plan = thameen.trip_planner.collect(dialog);
			if (!plan.length) {
				frappe.msgprint(__("Nothing to create — every row is zero."));
				return;
			}
			frappe.call({
				method: "thameen_erp.overrides.po_trips.create_trips_from_po",
				args: {
					purchase_order: frm.doc.name,
					destination: values.destination,
					target_warehouse: values.target_warehouse,
					sales_order: values.sales_order,
					delivery_location: values.delivery_location,
					transportation_charge: values.transportation_charge,
					plan: JSON.stringify(plan),
				},
				freeze: true,
				freeze_message: __("Creating trips…"),
				callback({ message }) {
					dialog.hide();
					frm.reload_doc();
					if (message && message.length === 1) frappe.set_route("Form", "Delivery Trip", message[0]);
					else if (message && message.length) frappe.set_route("List", "Delivery Trip", { custom_purchase_order: frm.doc.name });
				},
			});
		},
	});

	const limits = {};
	pending.forEach((l) => (limits[l.po_detail] = { label: l.item_code, max: flt(l.pending) }));
	dialog.show();
	toggle_destination(dialog);
	thameen.trip_planner.render(dialog, {
		plan: pending.map((l) => ({
			key: l.po_detail, item_code: l.item_code, qty: flt(l.pending), vehicle: null,
			departure_time: data.schedule_date || frappe.datetime.get_today(),
			extra: { po_detail: l.po_detail },
		})),
		limits,
		vehicles: data.vehicles || [],
		allow_under: true,
		same_truck: () => dialog.get_value("vehicle"),
		start_date: () => dialog.get_value("start_date"),
		days_between: () => dialog.get_value("days_between"),
	});
}

function toggle_destination(dialog) {
	const dest = dialog.get_value("destination");
	const customer = dest === "Customer";
	const undecided = dest === "Decide After Loading";
	dialog.set_df_property("target_warehouse", "hidden", customer || undecided ? 1 : 0);
	dialog.set_df_property("target_warehouse", "reqd", customer || undecided ? 0 : 1);
	dialog.set_df_property("sales_order", "hidden", customer ? 0 : 1);
	dialog.set_df_property("sales_order", "reqd", customer ? 1 : 0);
	dialog.set_df_property("delivery_location", "hidden", customer ? 0 : 1);
	dialog.set_df_property("transportation_charge", "hidden", customer ? 0 : 1);
}
