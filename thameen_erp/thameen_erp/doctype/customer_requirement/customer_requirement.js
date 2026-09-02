frappe.ui.form.on("Customer Requirement", {
	setup(frm) {
		// Only stock items can be trucked. Services and non-stock items have no
		// Bin, so they would break every availability check downstream.
		frm.set_query("item_code", "items", () => ({
			filters: { is_stock_item: 1, disabled: 0 },
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.status === "Approved" && !frm.doc.sales_order) {
			frm.add_custom_button(__("Sales Order"), () => {
				frappe.model.open_mapped_doc({
					method: "thameen_erp.thameen_erp.doctype.customer_requirement.customer_requirement.make_sales_order",
					frm: frm,
				});
			}, __("Create")).addClass("btn-primary");
		}
		if (frm.doc.sales_order) {
			frm.add_custom_button(__("Sales Order"), () =>
				frappe.set_route("Form", "Sales Order", frm.doc.sales_order), __("View"));
		}
	},

	customer(frm) {
		if (frm.doc.customer) frm.set_value("customer_name", null);
	},
});

frappe.ui.form.on("Customer Requirement Item", {
	qty: (frm, cdt, cdn) => set_amount(frm, cdt, cdn),
	rate: (frm, cdt, cdn) => set_amount(frm, cdt, cdn),
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item_code) return;
		frappe.db.get_value("Item", row.item_code, "stock_uom").then(({ message }) => {
			if (message) frappe.model.set_value(cdt, cdn, "uom", message.stock_uom);
		});
		frappe.call({
			method: "thameen_erp.api.check_stock_availability",
			args: { item_code: row.item_code, qty: row.qty || 0 },
			callback({ message }) {
				if (!message) return;
				// Warehouse and qty, nothing else. The aggregate totals above
				// them were the same numbers a second time.
				const lines = []
					.concat(message.warehouses || [], message.trucks || [])
					.slice(0, 6)
					.map((w) => `${frappe.utils.escape_html(w.warehouse)} ${format_number(w.qty)}`)
					.join("<br>");

				frappe.show_alert({
					message:
						`<b>${frappe.utils.escape_html(row.item_code)}</b><br>` +
						`<span class="small">${lines || __("no stock anywhere")}</span>`,
					indicator: message.sufficient ? "green" : "orange",
				});
			},
		});
	},
});

function set_amount(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "amount", flt(row.qty) * flt(row.rate));
}
