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
			if (!message || !message.length) {
				frappe.msgprint(
					__("Nothing left to plan — every line is delivered or already on a trip.")
				);
				return;
			}
			confirm_plan(frm, message);
		},
	});
}

function confirm_plan(frm, groups) {
	const rows = groups
		.map((group) => {
			const items = group.items
				.map((item) => `${frappe.utils.escape_html(item.item_code)} — ${format_number(item.qty)} ${frappe.utils.escape_html(item.uom || "")}`)
				.join("<br>");
			return `<tr>
				<td>${frappe.utils.escape_html(group.delivery_location || __("(order default)"))}</td>
				<td>${items}</td>
				<td class="text-right">${format_number(group.total_qty)}</td>
			</tr>`;
		})
		.join("");

	const html = `
		<p>${__("{0} trip(s) will be created in draft — one per delivery location.", [groups.length])}</p>
		<table class="table table-bordered">
			<thead><tr>
				<th>${__("Location")}</th><th>${__("Items")}</th><th class="text-right">${__("Total Qty")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
		<p class="text-muted">${__("Assign a vehicle and driver on each trip, then submit it.")}</p>`;

	const dialog = new frappe.ui.Dialog({
		title: __("Plan Delivery Trips"),
		size: "large",
		fields: [{ fieldtype: "HTML", options: html }],
		primary_action_label: __("Create Trips"),
		primary_action() {
			dialog.hide();
			frappe.call({
				method: "thameen_erp.overrides.sales_order.make_delivery_trips",
				args: { sales_order: frm.doc.name },
				freeze: true,
				freeze_message: __("Creating trips…"),
				callback({ message }) {
					if (!message || !message.length) return;
					frappe.msgprint({
						title: __("Trips Created"),
						indicator: "green",
						message: message
							.map((name) => frappe.utils.get_form_link("Delivery Trip", name, true))
							.join("<br>"),
					});
					frm.reload_doc();
				},
			});
		},
	});
	dialog.show();
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
