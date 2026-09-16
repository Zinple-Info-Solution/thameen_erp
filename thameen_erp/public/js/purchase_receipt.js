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
		hide_other_receipt_create_buttons(frm);
	},
});

// Only Purchase Invoice, Purchase Return and our own Delivery Trips belong
// on a cement receipt's Create menu — Landed Cost Voucher, Make Stock Entry
// and Retention Stock Entry never apply here.
//
// Core's own controller registers "refresh" the old (cscript) way, and
// Frappe's script_manager runs every new-style `frappe.ui.form.on` handler
// (ours) BEFORE any old-style one — so on the very first refresh, core has
// not added its Create-menu buttons yet and there is nothing here to remove.
// Hunting by data-label straight off `inner_toolbar`, repeated a few times
// after that first pass, catches them once core actually adds them, without
// depending on exactly which group wraps them or how it renders once ours is
// the primary button in that group.
//
// Named and scoped to this file only (not shared with sales_order.js's
// near-identical helper): Frappe keeps doctype_js files loaded once fetched,
// so visiting both a Sales Order and a Purchase Receipt in one session would
// otherwise redeclare the same top-level const/function twice and break both.
const RECEIPT_OTHER_CREATE_BUTTONS = ["Debit Note", "Landed Cost Voucher", "Make Stock Entry", "Retention Stock Entry"];

function hide_other_receipt_create_buttons(frm) {
	const remove = () => {
		if (!frm.page.inner_toolbar) return;
		RECEIPT_OTHER_CREATE_BUTTONS.forEach((label) => {
			frm.page.inner_toolbar.find(`[data-label="${encodeURIComponent(__(label))}"]`).remove();
		});
		frm.page.inner_toolbar.find(".inner-group-button").each(function () {
			if (!$(this).find(".dropdown-item").length) $(this).remove();
		});
	};
	remove();
	[300, 800, 1500].forEach((ms) => setTimeout(remove, ms));
}

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

// A Sales Order here is optional, not required. Traced (see
// `_traced_sales_order` in overrides.po_trips — this receipt was bought via
// "Create Purchase Order for Shortfall" to cover a specific order) it is
// filled in and locked, since that order is the only sane choice. Untraced —
// an ordinary restock purchase — it is left blank and editable: pick one now
// if you already know where this is going, or leave it and set the Sales
// Order directly on the trip later, same as any other draft trip.
function build_receipt_dialog(frm, data, pending) {
	const locked_so = data.sales_order || null;

	const fetch_delivery_location = (so) => {
		if (!so) return;
		frappe.db.get_value("Sales Order", so, "custom_delivery_location").then(({ message }) => {
			if (message && message.custom_delivery_location) {
				dialog.set_value("delivery_location", message.custom_delivery_location);
			}
		});
	};

	const dialog = new frappe.ui.Dialog({
		title: __("Plan Delivery Trips from {0}", [frm.doc.name]),
		size: "extra-large",
		fields: [
			{
				fieldname: "sales_order", fieldtype: "Link", options: "Sales Order",
				label: __("Customer's Sales Order"),
				default: locked_so || undefined,
				read_only: locked_so ? 1 : 0,
				description: locked_so
					? __("Bought for this order's shortfall — set automatically.")
					: __("Optional — leave blank to decide later, directly on the trip."),
				get_query: () => ({
					query: "thameen_erp.overrides.po_trips.sales_orders_for_receipt",
					filters: { purchase_receipt: frm.doc.name },
				}),
				onchange: () => fetch_delivery_location(dialog.get_value("sales_order")),
			},
			{ fieldname: "delivery_location", fieldtype: "Data", label: __("Delivery Site"), hidden: 1 },
			{ fieldtype: "Section Break" },
			{ fieldtype: "HTML", fieldname: "summary" },
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
				method: "thameen_erp.overrides.po_trips.create_trips_from_receipt",
				args: {
					purchase_receipt: frm.doc.name,
					sales_order: values.sales_order,
					delivery_location: values.delivery_location,
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

	// A `default` fills the field but does not fire `onchange` — that only
	// happens on user interaction — so the locked case fetches its delivery
	// site itself instead of waiting for an edit that will never come.
	if (locked_so) fetch_delivery_location(locked_so);

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
