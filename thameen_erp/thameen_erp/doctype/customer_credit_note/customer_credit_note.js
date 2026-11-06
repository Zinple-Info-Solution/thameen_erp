frappe.ui.form.on("Customer Credit Note", {
	setup(frm) {
		frm.set_query("sales_invoice", () => ({
			filters: { customer: frm.doc.customer, docstatus: 1, is_return: 0 },
		}));
	},

	refresh(frm) {
		if (frm.doc.return_invoice) {
			frm.add_custom_button(__("Return Invoice"), () =>
				frappe.set_route("Form", "Sales Invoice", frm.doc.return_invoice), __("View"));
		}
		if (frm.doc.docstatus === 0 && frm.doc.sales_invoice) {
			frm.add_custom_button(__("Fetch Invoice Lines"), () => fetch_lines(frm));
		}
	},

	get_items(frm) {
		fetch_lines(frm);
	},

	sales_invoice(frm) {
		if (frm.doc.sales_invoice && !(frm.doc.items || []).length) fetch_lines(frm);
	},
});

function fetch_lines(frm) {
	frappe.call({
		method: "thameen_erp.thameen_erp.doctype.customer_credit_note.customer_credit_note.get_invoice_lines",
		args: { sales_invoice: frm.doc.sales_invoice },
		freeze: true,
		callback({ message }) {
			if (!message || !message.length) {
				frappe.msgprint(__("No lines on this invoice are available to credit."));
				return;
			}
			frm.clear_table("items");
			message.forEach((line) => {
				const row = frm.add_child("items");
				Object.assign(row, {
					sales_invoice_item: line.sales_invoice_item,
					item_code: line.item_code,
					qty: line.qty,
					rate: line.rate,
					max_creditable: line.max_creditable,
					amount: line.amount,
				});
			});
			frm.refresh_field("items");
		},
	});
}
