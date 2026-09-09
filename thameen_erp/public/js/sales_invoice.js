frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (!frm.is_new() || frm.doc.items.length) return;
		frm.add_custom_button(__("Consolidated Monthly Bill"), () => open_consolidation_dialog(frm),
			__("Get Items From"));
	},

	custom_add_rebate(frm) {
		recalculate_all_rebates(frm);
	},
});

frappe.ui.form.on("Sales Invoice Item", {
	qty(frm, cdt, cdn) {
		calculate_row_rebate(frm, cdt, cdn);
	},
	price_list_rate(frm, cdt, cdn) {
		calculate_row_rebate(frm, cdt, cdn);
	},
	item_code(frm, cdt, cdn) {
		setTimeout(() => calculate_row_rebate(frm, cdt, cdn), 300);
	},
	custom_rebate_percentage(frm, cdt, cdn) {
		calculate_row_rebate(frm, cdt, cdn);
	},
});

function calculate_row_rebate(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	const base_rate = flt(row.price_list_rate) || flt(row.rate);
	const percentage = frm.doc.custom_add_rebate ? flt(row.custom_rebate_percentage) : 0;
	const rebate_per_unit = (base_rate * percentage) / 100;
	const new_rate = base_rate - rebate_per_unit;

	frappe.model.set_value(cdt, cdn, "custom_rebate_amount", rebate_per_unit * flt(row.qty));

	if (flt(row.rate).toFixed(4) !== new_rate.toFixed(4)) {
		// triggers ERPNext's built-in amount / net_total / grand_total recalculation
		frappe.model.set_value(cdt, cdn, "rate", new_rate);
	}
}

function recalculate_all_rebates(frm) {
	(frm.doc.items || []).forEach((row) => calculate_row_rebate(frm, row.doctype, row.name));
}

// ─── Consolidated Monthly Bill dialog (unchanged) ─────────────────

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