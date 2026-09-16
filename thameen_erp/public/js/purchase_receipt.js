// Purchase Receipt → Delivery Trips.
//
// The receipt already put this cement in a real warehouse and posted the
// stock/accounting entries for it — the trip planned here just trucks it on
// to the customer it was bought for. Always "Own Warehouse" supply source:
// Loading is a plain Material Transfer from that warehouse, not a second
// Purchase Receipt.

frappe.ui.form.on("Purchase Receipt", {
	refresh(frm) {
		if (frm.doc.custom_delivery_trip) {
			frm.add_custom_button(frm.doc.custom_delivery_trip,
				() => frappe.set_route("Form", "Delivery Trip", frm.doc.custom_delivery_trip), __("View"));
		}
		if (frm.doc.docstatus !== 1) return;

		frm.add_custom_button(__("Delivery Trips"), () => open_receipt_planner(frm), __("Create")).addClass("btn-primary");
	},
});

function open_receipt_planner(frm) {
	frappe.call({
		method: "thameen_erp.overrides.po_trips.preview_receipt_trips",
		args: { purchase_receipt: frm.doc.name },
		freeze: true,
		freeze_message: __("Working out what is still pending…"),
		callback({ message }) {
			if (!message) return;
			const pending = (message.lines || []).filter((l) => flt(l.pending) > 0);
			if (!pending.length) {
				frappe.msgprint(__("Everything on this receipt is already planned onto a trip."));
				return;
			}
			build_receipt_dialog(frm, message, pending);
		},
	});
}

function build_receipt_dialog(frm, data, pending) {
	const dialog = new frappe.ui.Dialog({
		title: __("Plan Delivery Trips from {0}", [frm.doc.name]),
		size: "extra-large",
		fields: [
			{
				fieldname: "sales_order", fieldtype: "Link", options: "Sales Order",
				label: __("Customer's Sales Order"), reqd: 1,
				get_query: () => ({
					query: "thameen_erp.overrides.po_trips.sales_orders_for_receipt",
					filters: { purchase_receipt: frm.doc.name },
				}),
				onchange: () => {
					const so = dialog.get_value("sales_order");
					if (!so) return;
					frappe.db.get_value("Sales Order", so, "custom_delivery_location").then(({ message }) => {
						if (message && message.custom_delivery_location) {
							dialog.set_value("delivery_location", message.custom_delivery_location);
						}
					});
				},
			},
			{ fieldtype: "Column Break" },
			{
				fieldname: "transportation_charge", fieldtype: "Currency", label: __("Freight per trip"),
				description: __("What the customer is charged per truckload."),
			},
			{ fieldname: "delivery_location", fieldtype: "Data", label: __("Delivery Site"), hidden: 1 },
			{ fieldtype: "Section Break" },
			{ fieldtype: "HTML", fieldname: "summary" },
			{ fieldtype: "HTML", fieldname: "plan" },
		],
		primary_action_label: __("Create Trips"),
		primary_action(values) {
			if (!values.sales_order) {
				frappe.msgprint(__("Choose the Sales Order this cement is sold against."));
				return;
			}
			const plan = thameen.trip_planner.collect(dialog);
			if (!plan.length) {
				frappe.msgprint(__("Nothing to create — every row is zero."));
				return;
			}
			frappe.call({
				method: "thameen_erp.overrides.po_trips.create_trips_from_receipt",
				args: {
					purchase_receipt: frm.doc.name,
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
					else if (message && message.length) frappe.set_route("List", "Delivery Trip", { custom_purchase_receipt: frm.doc.name });
				},
			});
		},
	});

	// What this receipt actually put in the warehouse, item by item — shown
	// once, up front, instead of a "days between trips" field that had
	// nothing to do with a receipt that arrived on a single date.
	dialog.fields_dict.summary.$wrapper.html(
		`<div class="small text-muted mb-3"><b>${__("Item & planned qty")}</b><br>` +
			pending
				.map(
					(l) =>
						`${frappe.utils.escape_html(l.item_code)} — ${format_number(flt(l.pending))} ${frappe.utils.escape_html(l.stock_uom || "")}`
				)
				.join("<br>") +
			`</div>`
	);

	const limits = {};
	const plan = pending.map((l) => {
		limits[l.item_code] = { label: l.item_code, max: flt(l.pending) };
		return {
			key: l.item_code, item_code: l.item_code, qty: flt(l.pending), vehicle: null, driver: null,
			label: l.item_code,
			departure_time: frappe.datetime.get_today(),
			extra: {},
		};
	});

	dialog.show();
	thameen.trip_planner.render(dialog, {
		plan, limits, vehicles: data.vehicles || [], drivers: data.drivers || [], allow_under: true,
	});
}
