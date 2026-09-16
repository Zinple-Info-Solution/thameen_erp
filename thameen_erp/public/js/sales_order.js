frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (["Closed", "Cancelled"].includes(frm.doc.status)) return;

		// Raised regardless of docstatus — a promise the yard cannot keep is
		// worth flagging while the order is still a draft, not just after.
		check_stock_coverage(frm);

		if (frm.doc.docstatus !== 1) return;

		frm.add_custom_button(
			__("Delivery Trips"),
			() => plan_trips(frm),
			__("Create")
		).addClass("btn-primary");

		show_trip_summary(frm);
	},
});

// Company-wide stock against what this order still owes, checked the moment
// the order is opened — catching a shortfall here is far cheaper than
// catching it when a Delivery Trip refuses to submit later.
function check_stock_coverage(frm) {
	if (frm.is_new() || !(frm.doc.items || []).length) return;

	frappe.call({
		method: "thameen_erp.overrides.sales_order.check_stock_coverage",
		args: { sales_order: frm.doc.name },
		callback({ message }) {
			if (!message || !message.short || !message.short.length) return;

			const lines = message.short
				.map((r) =>
					__("{0}: needs {1}, only {2} available company-wide ({3} short)", [
						frappe.utils.escape_html(r.item_name),
						format_number(r.needed),
						format_number(r.available),
						format_number(r.short),
					])
				)
				.join("<br>");

			frm.dashboard.set_headline_alert(
				`<div class="row"><div class="col-sm-12">` +
					`<b>${__("Not enough stock company-wide to fully deliver this order")}</b><br>${lines}` +
					`</div></div>`,
				"red"
			);
		},
	});
}

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

// Sales Order case: one trip per site AND per item to start with — two
// different items never share a row, each gets its own line with its own
// vehicle picker. Qty, vehicle, driver and date/time are all set per row in
// the table below — nothing is left outside it any more. "Same truck for
// all, one day apart" works off whatever date is already on the first row,
// stepping one day at a time.
function open_so_planner(frm, data) {
	const dialog = new frappe.ui.Dialog({
		title: __("Plan Delivery Trips from {0}", [frm.doc.name]),
		size: "extra-large",
		fields: [{ fieldtype: "HTML", fieldname: "plan" }],
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
			key, item_code: p.item_code, qty: flt(p.qty), vehicle: null, driver: null,
			label: `${p.item_code} → ${p.delivery_location || __("(order default)")}`,
			departure_time: data.departure_time || frappe.datetime.get_today(),
			extra: { delivery_location: p.delivery_location || null },
		};
	});
	dialog.show();
	thameen.trip_planner.render(dialog, {
		// No dialog-level vehicle, date or days-between field any more —
		// "Same truck for all" falls back to whichever vehicle and date are
		// already on the first row of the table, stepping one day at a time.
		plan, limits, vehicles: data.vehicles || [], drivers: data.drivers || [], allow_under: true,
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
