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

		hide_other_receipt_create_buttons(frm);
		add_delivery_trips_button_unless_vehicle_warehouse(frm);
		add_waiting_trip_button(frm);
		add_create_trip_after_pickup_button(frm);
	},

	// `link_shortfall_receipt_to_trip` (overrides.procurement) links the trip
	// server-side, inside this same submit, via a raw db write — not part of
	// the doc this form already has in memory, so a reload is what actually
	// picks it up. No navigation away any more — staying on the receipt and
	// reloading it is enough: `refresh` already shows the "View" button once
	// `custom_delivery_trip` is set, or the "Assign a Vehicle" / "Delivery
	// Trips" buttons otherwise, so there is always a button to follow from
	// here instead of the page jumping on its own.
	on_submit(frm) {
		frm.reload_doc();
	},
});

// This receipt just landed a pickup trip's Purchase Order straight onto its
// truck — the trip's own status is already "Trip Reached and Loaded" by the
// time this form loads (see procurement.link_shortfall_receipt_to_trip,
// which sets that in the same submit this receipt just went through).
// Nothing here is asked for by hand: vehicle, driver and items all come
// straight off the truck server-side.
function add_create_trip_after_pickup_button(frm) {
	if (!frm.doc.custom_delivery_trip) return;

	frappe.db
		.get_value("Delivery Trip", frm.doc.custom_delivery_trip, ["custom_trip_route", "status"])
		.then(({ message }) => {
			if (!message || message.custom_trip_route !== "Warehouse to Supplier") return;
			if (message.status !== "Trip Reached and Loaded") return;

			frm.add_custom_button(__("Create Delivery Trip"), () => open_trip_after_pickup_dialog(frm))
				.addClass("btn-primary");
		});
}

function open_trip_after_pickup_dialog(frm) {
	frappe.db.get_value("Delivery Trip", frm.doc.custom_delivery_trip, "vehicle").then(({ message }) => {
		const vehicle = message && message.vehicle;
		frappe.call({
			method: "thameen_erp.overrides.vehicle_stock.get_truck_stock_summary",
			args: { vehicle },
			freeze: true,
			callback({ message: truck }) {
				const rows = ((truck && truck.items) || [])
					.map(
						(i) => `<tr>
							<td>${frappe.utils.escape_html(i.item_code)}</td>
							<td class="text-right">${format_number(i.qty)}</td>
							<td>${frappe.utils.escape_html(i.stock_uom || "")}</td>
						</tr>`
					)
					.join("");

				const html = `
					<p>${__("{0} is already loaded — nothing here is edited, this just creates the trip.", [
						frappe.utils.escape_html(vehicle || __("The vehicle")),
					])}</p>
					<table class="table table-bordered small">
						<thead><tr><th>${__("Item")}</th><th class="text-right">${__("On truck")}</th><th>${__("UOM")}</th></tr></thead>
						<tbody>${rows}</tbody>
					</table>`;

				const dialog = new frappe.ui.Dialog({
					title: __("Create Delivery Trip"),
					fields: [{ fieldtype: "HTML", options: html }],
					primary_action_label: __("Create Trip"),
					primary_action() {
						frappe.call({
							method: "thameen_erp.overrides.procurement.create_trip_after_pickup",
							args: { purchase_receipt: frm.doc.name },
							freeze: true,
							callback({ message: trip }) {
								dialog.hide();
								if (trip) frappe.set_route("Form", "Delivery Trip", trip);
							},
						});
					},
				});
				dialog.show();
			},
		});
	});
}

// Whether this receipt's stock was bought to fill a specific trip's
// shortfall (either directly, or via "Material Request instead" that a
// buyer later turned into a Purchase Order by hand — Frappe's own default
// doc-mapping carries `custom_delivery_trip` through both hops with no
// extra code), and that trip is still just sitting there Draft with no
// vehicle assigned: point straight at it instead of leaving the dispatcher
// to go find it. Once a vehicle is on the trip, or it is past Draft, this
// stops showing — nothing left to "go finish".
function add_waiting_trip_button(frm) {
	// Skip the round trip entirely for a plain restock — nothing on this
	// receipt even references a Purchase Order, so there is no trip to trace
	// back to. `row.purchase_order` is already sitting in the doc, no fetch
	// needed to check it.
	if (!(frm.doc.items || []).some((r) => r.purchase_order)) return;

	frappe.call({
		method: "thameen_erp.overrides.po_trips.waiting_trip_for_receipt",
		args: { purchase_receipt: frm.doc.name },
		callback({ message: trip }) {
			if (!trip) return;
			frm.add_custom_button(
				__("Assign a Vehicle — {0}", [trip]),
				() => frappe.set_route("Form", "Delivery Trip", trip)
			).addClass("btn-primary");
		},
	});
}

// A receipt accepted straight into a vehicle warehouse is filling a specific
// trip's shortfall — see `link_shortfall_receipt_to_trip` in
// overrides.procurement, which links that trip automatically on submit.
// There is nothing new to plan from here in that case; the button only makes
// sense for an ordinary restock into an ordinary (non-vehicle) warehouse.
function add_delivery_trips_button_unless_vehicle_warehouse(frm) {
	const warehouses = Array.from(new Set((frm.doc.items || []).map((r) => r.warehouse).filter(Boolean)));
	const add_button = () => frm.add_custom_button(__("Delivery Trips"), () => open_receipt_planner(frm), __("Create")).addClass("btn-primary");

	if (!warehouses.length) {
		add_button();
		return;
	}
	frappe.db
		.get_list("Warehouse", {
			filters: { name: ["in", warehouses], custom_is_vehicle_warehouse: 1 },
			fields: ["name"],
			limit: 1,
		})
		.then((rows) => {
			if (!rows || !rows.length) add_button();
		})
		// A failed lookup must not silently remove the button along with it —
		// fall back to showing it, same as before this check existed.
		.catch(() => add_button());
}

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
		// hide_full: a truck already full of this item (or of something else,
		// filtered separately above) is not a candidate for freshly-received
		// stock — only an empty truck or one with real room left is worth
		// offering.
		hide_full: true,
	});
}
