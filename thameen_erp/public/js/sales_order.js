frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;
		if (["Closed", "Cancelled"].includes(frm.doc.status)) return;

		frm.add_custom_button(
			__("Delivery Trips"),
			() => plan_trips(frm),
			__("Create")
		).addClass("btn-primary");

		show_trip_summary(frm);
	},
});

function plan_trips(frm) {
	frappe.call({
		method: "thameen_erp.overrides.sales_order.preview_trip_plan",
		args: { sales_order: frm.doc.name },
		freeze: true,
		freeze_message: __("Working out what is still pending…"),
		callback({ message }) {
			if (!message || !message.plan || !message.plan.length) {
				frappe.msgprint(__("Nothing left to plan — every line is delivered or already on a trip."));
				return;
			}
			open_so_planner(frm, message);
		},
	});
}

// Sales Order case: one trip per site and item to start with; pick a truck to
// split by its capacity into dated trips, then edit anything before creating.
function open_so_planner(frm, data) {
	const dialog = new frappe.ui.Dialog({
		title: __("Plan Delivery Trips from {0}", [frm.doc.name]),
		size: "extra-large",
		fields: [
			{
				fieldname: "vehicle", fieldtype: "Link", options: "Vehicle", label: __("Plan by truck"),
				description: __("Splits every line by this truck's capacity, one trip per day. Edit afterwards."),
				get_query: () => ({
					query: "thameen_erp.overrides.vehicle_stock.vehicle_query",
					filters: { custom_status: ["in", ["Available", "Assigned"]] },
				}),
				onchange: () => {
					const v = dialog.get_value("vehicle");
					if (v) thameen.trip_planner.auto_split(dialog, v, dialog.get_value("start_date"), dialog.get_value("days_between") || 1);
				},
			},
			{ fieldtype: "Column Break" },
			{ fieldname: "start_date", fieldtype: "Date", label: __("First trip on"), default: (data.departure_time || "").slice(0, 10) || frappe.datetime.get_today() },
			{ fieldname: "days_between", fieldtype: "Int", label: __("Days between trips"), default: 1 },
			{ fieldtype: "Section Break" },
			{ fieldtype: "HTML", fieldname: "plan" },
		],
		primary_action_label: __("Create Trips"),
		primary_action() {
			const plan = thameen.trip_planner.collect(dialog);
			if (!plan.length) {
				frappe.msgprint(__("Nothing to create — every row is zero."));
				return;
			}
			frappe.call({
				method: "thameen_erp.overrides.sales_order.make_delivery_trips_from_plan",
				args: { sales_order: frm.doc.name, plan: JSON.stringify(plan) },
				freeze: true,
				freeze_message: __("Creating trips…"),
				callback({ message }) {
					dialog.hide();
					frm.reload_doc();
					if (message && message.length === 1) frappe.set_route("Form", "Delivery Trip", message[0]);
					else if (message && message.length) frappe.set_route("List", "Delivery Trip", { custom_sales_order: frm.doc.name });
				},
			});
		},
	});

	const limits = {};
	const plan = data.plan.map((p) => {
		const key = `${p.delivery_location || ""}::${p.item_code}`;
		limits[key] = { label: `${p.item_code} @ ${p.delivery_location || __("(order default)")}`, max: flt(p.qty) };
		return {
			key, item_code: p.item_code, qty: flt(p.qty), vehicle: null,
			label: `${p.item_code} → ${p.delivery_location || __("(order default)")}`,
			departure_time: (data.departure_time || "").slice(0, 10) || frappe.datetime.get_today(),
			extra: { delivery_location: p.delivery_location || null },
		};
	});
	dialog.show();
	thameen.trip_planner.render(dialog, {
		plan, limits, vehicles: data.vehicles || [], allow_under: true,
		same_truck: () => dialog.get_value("vehicle"),
		start_date: () => dialog.get_value("start_date"),
		days_between: () => dialog.get_value("days_between"),
	});
}

function show_trip_summary(frm) {
	frappe.call({
		method: "frappe.client.get_list",
		args: {
			doctype: "Delivery Trip Item",
			parent: "Delivery Trip",
			filters: { sales_order: frm.doc.name },
			fields: ["parent", "item_code", "qty", "delivered_qty", "delivery_location"],
			limit_page_length: 200,
		},
		callback({ message }) {
			if (!message || !message.length) return;
			const trips = new Set(message.map((row) => row.parent));
			frm.dashboard.add_comment(
				__("{0} row(s) planned across {1} trip(s).", [message.length, trips.size]),
				"blue",
				true
			);
		},
	});
}
