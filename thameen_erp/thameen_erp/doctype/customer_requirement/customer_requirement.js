frappe.ui.form.on("Customer Requirement", {
	refresh(frm) {
		render_credit_banner(frm);

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
				frm.dashboard.add_comment(
					__("{0}: {1} in warehouses, {2} on trucks", [
						row.item_code, message.warehouse_qty, message.truck_qty]),
					message.sufficient ? "green" : "orange", true);
			},
		});
	},
});

function set_amount(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "amount", flt(row.qty) * flt(row.rate));
}

function render_credit_banner(frm) {
	if (!frm.doc.customer || frm.is_new()) return;
	const over = frm.doc.credit_limit && !frm.doc.credit_check_passed;
	frm.dashboard.add_comment(
		__("Credit limit {0} · Outstanding {1} · Available {2}", [
			format_currency(frm.doc.credit_limit),
			format_currency(frm.doc.outstanding_amount),
			format_currency(frm.doc.available_credit),
		]),
		over ? "red" : "blue",
		true
	);
}
