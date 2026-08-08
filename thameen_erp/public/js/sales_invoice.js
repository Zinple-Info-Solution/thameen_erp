frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (!frm.is_new() || frm.doc.items.length) return;
		frm.add_custom_button(__("Consolidated Monthly Bill"), () => open_consolidation_dialog(frm),
			__("Get Items From"));
	},
});

function open_consolidation_dialog(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Consolidated Monthly Invoice"),
		fields: [
			{ fieldname: "customer", fieldtype: "Link", options: "Customer",
			  label: __("Customer"), reqd: 1, default: frm.doc.customer },
			{ fieldname: "company", fieldtype: "Link", options: "Company",
			  label: __("Company"), reqd: 1, default: frm.doc.company },
			{ fieldtype: "Section Break" },
			{ fieldname: "from_date", fieldtype: "Date", label: __("From"), reqd: 1,
			  default: frappe.datetime.month_start() },
			{ fieldtype: "Column Break" },
			{ fieldname: "to_date", fieldtype: "Date", label: __("To"), reqd: 1,
			  default: frappe.datetime.month_end() },
			{ fieldtype: "Section Break" },
			{ fieldname: "preview", fieldtype: "HTML" },
		],
		primary_action_label: __("Build Invoice"),
		primary_action(values) {
			frappe.call({
				method: "thameen_erp.api.make_consolidated_invoice",
				args: values,
				freeze: true,
				freeze_message: __("Consolidating deliveries…"),
				callback({ message }) {
					if (!message) return;
					d.hide();
					frappe.model.sync(message);
					frappe.set_route("Form", "Sales Invoice", message.name);
				},
			});
		},
	});

	const refresh_preview = () => {
		const v = d.get_values(true);
		if (!(v && v.customer && v.from_date && v.to_date)) return;
		frappe.call({
			method: "thameen_erp.api.get_billable_deliveries",
			args: v,
			callback({ message }) {
				const rows = message || [];
				const body = rows.length
					? rows.map((r) => `<tr><td>${r.name}</td><td>${frappe.datetime.str_to_user(r.posting_date)}</td>
						<td>${r.custom_vehicle || "—"}</td>
						<td class="text-right">${format_currency(r.grand_total)}</td></tr>`).join("")
					: `<tr><td colspan="4" class="text-muted">${__("No unbilled deliveries in this period.")}</td></tr>`;
				d.fields_dict.preview.$wrapper.html(`
					<p class="text-muted">${__("{0} delivery note(s) found", [rows.length])}</p>
					<table class="table table-bordered table-sm">
						<thead><tr><th>${__("Delivery Note")}</th><th>${__("Date")}</th>
						<th>${__("Vehicle")}</th><th class="text-right">${__("Amount")}</th></tr></thead>
						<tbody>${body}</tbody>
					</table>`);
			},
		});
	};

	["customer", "from_date", "to_date"].forEach((f) => {
		d.fields_dict[f].df.onchange = refresh_preview;
	});
	d.show();
	refresh_preview();
}
