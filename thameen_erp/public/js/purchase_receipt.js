// Purchase Receipt → Delivery Trips.
//
// The receipt already put this cement in a real warehouse and posted the
// stock/accounting entries for it — the trip planned here just trucks it on
// to the customer it was bought for. Always "Own Warehouse" supply source:
// Loading is a plain Material Transfer from that warehouse, not a second
// Purchase Receipt.

frappe.ui.form.on("Purchase Receipt", {
	setup(frm) {
		// Only a vehicle with an open pickup trip against one of THIS
		// receipt's own Purchase Orders is offered — picking any other truck
		// is not even possible, so there is nothing left to validate by hand.
		frm.set_query("vehicle", "custom_pickup_vehicles", () => ({
			query: "thameen_erp.overrides.procurement.pickup_vehicle_query",
			filters: { purchase_orders: JSON.stringify(receipt_po_names(frm)) },
		}));
	},

	refresh(frm) {
		if (frm.doc.custom_delivery_trip) {
			frm.add_custom_button(frm.doc.custom_delivery_trip,
				() => frappe.set_route("Form", "Delivery Trip", frm.doc.custom_delivery_trip), __("View"));
		}
		if (frm.doc.docstatus === 0) {
			add_add_pickup_vehicles_button(frm);
			// A receipt mapped straight off a Purchase Order (or one you are
			// still filling in) that has an open pickup trip against it: the
			// Pickup Vehicles table fills itself in, so there is nothing left
			// to click before submitting — same lookup the button above runs
			// by hand, just run once automatically. Only once per document,
			// not on every refresh: deleting a row you don't want on this
			// receipt must stay deleted, not get silently re-added the next
			// time the form re-renders.
			if (!frm.__pickup_vehicles_auto_filled) {
				frm.__pickup_vehicles_auto_filled = true;
				if (!(frm.doc.custom_pickup_vehicles || []).length) fill_pickup_vehicles(frm, { silent: true });
			}
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

function receipt_po_names(frm) {
	return Array.from(new Set((frm.doc.items || []).map((r) => r.purchase_order).filter(Boolean)));
}

// One receipt can settle several trucks at once — the Pickup Vehicles table
// says which. Filling it by hand means knowing which vehicles are even out
// on a pickup trip for this PO; this does that lookup once and adds
// whichever ones are not already in the table, same idea as
// `add_waiting_trip_button` below but for the newer multi-vehicle case.
//
// Used two ways: automatically once, the first time a draft with a pickup
// trip's PO on it loads with the table still empty (see `refresh` above —
// `silent: true` there, so an ordinary receipt with nothing to settle stays
// quiet), and by the "Add Pickup Vehicles" button below for topping it up
// by hand afterward (rows added later, a truck forgotten the first time).
function fill_pickup_vehicles(frm, opts) {
	const silent = !!(opts && opts.silent);
	const po_names = receipt_po_names(frm);
	if (!po_names.length) return;

	frappe.call({
		method: "thameen_erp.overrides.procurement.pickup_vehicles_for_pos",
		args: { purchase_orders: JSON.stringify(po_names) },
		freeze: !silent,
		callback({ message: candidates }) {
			if (!candidates || !candidates.length) {
				if (!silent) frappe.msgprint(__("No vehicle has an open pickup trip against this receipt's Purchase Order(s)."));
				return;
			}
			const already = new Set((frm.doc.custom_pickup_vehicles || []).map((r) => r.vehicle));
			let added = 0;
			candidates
				.filter((c) => !already.has(c.vehicle))
				.forEach((c) => {
					const { claims, purchase_order, ...fields } = c;
					const row = frm.add_child("custom_pickup_vehicles");
					Object.assign(row, fields);
					apply_pickup_warehouse_to_items(frm, { warehouse: c.warehouse, claims, purchase_order });
					added += 1;
				});
			frm.refresh_field("custom_pickup_vehicles");
			frm.refresh_field("items");
			if (added) {
				frappe.show_alert({ message: __("{0} pickup vehicle(s) filled in — check and submit.", [added]), indicator: "green" });
			} else if (!silent) {
				frappe.show_alert({ message: __("Already listed."), indicator: "orange" });
			}
		},
	});
}

function add_add_pickup_vehicles_button(frm) {
	const po_names = receipt_po_names(frm);
	if (!po_names.length) return;
	frm.add_custom_button(__("Add Pickup Vehicles"), () => fill_pickup_vehicles(frm, { silent: false }));
}

frappe.ui.form.on("Purchase Receipt Pickup Vehicle", {
	vehicle(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.vehicle) {
			frappe.model.set_value(cdt, cdn, "delivery_trip", null);
			frappe.model.set_value(cdt, cdn, "driver", null);
			frappe.model.set_value(cdt, cdn, "warehouse", null);
			return;
		}
		frappe.call({
			method: "thameen_erp.overrides.procurement.pickup_trip_for_vehicle",
			args: { vehicle: row.vehicle, purchase_orders: JSON.stringify(receipt_po_names(frm)) },
			callback({ message: info }) {
				frappe.model.set_value(cdt, cdn, "delivery_trip", info ? info.trip : null);
				frappe.model.set_value(cdt, cdn, "driver", info ? info.driver : null);
				frappe.model.set_value(cdt, cdn, "warehouse", info ? info.warehouse : null);
				if (info) apply_pickup_warehouse_to_items(frm, info);
			},
		});
	},
});

const PICKUP_QTY_TOL = 0.01;

// Picking the vehicle here is the whole point — no one should then have to
// go set the same warehouse a second time, by hand, on every item row it
// applies to.
//
// A row is matched to a claim by, in order:
//   1. `po_detail` — precise, but only works for a row pulled in via "Get
//      Items From Purchase Order" (or the receipt's own standard Create
//      button off the PO), which is the only thing that ever stamps
//      `purchase_order_item` onto a row in the first place.
//   2. `purchase_order` + item code — the fallback for a row typed in by
//      hand, which never gets `purchase_order_item` set at all.
//
// One PO line picked up by several trucks still arrives as ONE combined row
// on the receipt (the user is not expected to know in advance how many
// trucks it will take, or to split it by hand) — "Get Items From Purchase
// Order" just carries the line's full qty through unchanged. So a claim
// whose qty does not exactly match any row is not left alone: the smallest
// row still big enough to cover it is split — this claim's qty carved into
// its own new row on this vehicle's warehouse, the rest staying behind on
// the original row for the next vehicle's claim to match against.
function apply_pickup_warehouse_to_items(frm, info) {
	if (!info.warehouse || !info.claims || !info.claims.length) return;
	let changed = false;

	const same_line = (item, claim) =>
		item.purchase_order_item
			? item.purchase_order_item === claim.po_detail
			: item.purchase_order === info.purchase_order && item.item_code === claim.item_code;

	info.claims.forEach((claim) => {
		const claim_qty = flt(claim.qty);
		if (claim_qty <= 0) return;

		const items = frm.doc.items || [];

		// Already sitting on this vehicle's own warehouse with the right qty
		// — nothing left to do for this claim.
		const already = items.some(
			(item) =>
				item.warehouse === info.warehouse &&
				same_line(item, claim) &&
				Math.abs(flt(item.qty) * (flt(item.conversion_factor) || 1) - claim_qty) < PICKUP_QTY_TOL
		);
		if (already) return;

		const candidates = items.filter((item) => item.warehouse !== info.warehouse && same_line(item, claim));

		// A row whose qty already matches this claim exactly just needs its
		// warehouse corrected — no split needed.
		const exact = candidates.find(
			(item) => Math.abs(flt(item.qty) * (flt(item.conversion_factor) || 1) - claim_qty) < PICKUP_QTY_TOL
		);
		if (exact) {
			frappe.model.set_value(exact.doctype, exact.name, "warehouse", info.warehouse);
			changed = true;
			return;
		}

		// Otherwise carve this claim's qty out of the smallest row still big
		// enough to cover it, leaving the remainder on that row for another
		// vehicle's claim.
		const donor = candidates
			.filter((item) => flt(item.qty) * (flt(item.conversion_factor) || 1) > claim_qty + PICKUP_QTY_TOL)
			.sort((a, b) => flt(a.qty) - flt(b.qty))[0];
		if (!donor) return;

		const conv = flt(donor.conversion_factor) || 1;
		const donor_stock_qty = flt(donor.qty) * conv;

		const new_row = frm.add_child("items");
		Object.assign(new_row, {
			item_code: donor.item_code,
			item_name: donor.item_name,
			description: donor.description,
			uom: donor.uom,
			stock_uom: donor.stock_uom,
			conversion_factor: donor.conversion_factor,
			rate: donor.rate,
			purchase_order: donor.purchase_order,
			purchase_order_item: donor.purchase_order_item,
			warehouse: info.warehouse,
			qty: claim_qty / conv,
		});
		frappe.model.set_value(donor.doctype, donor.name, "qty", (donor_stock_qty - claim_qty) / conv);
		changed = true;
	});

	if (changed) {
		frm.refresh_field("items");
		frappe.show_alert({ message: __("Item row(s) split and warehouse(s) matched to {0}.", [info.warehouse]), indicator: "green" });
	}
}

// Re-runs the same matching for every vehicle already in the Pickup
// Vehicles table — covers a row typed in (or edited) AFTER the vehicle was
// already picked, when the one-shot `vehicle` handler above had nothing to
// match against yet.
function reapply_all_pickup_warehouses(frm) {
	const rows = (frm.doc.custom_pickup_vehicles || []).filter((r) => r.vehicle);
	if (!rows.length) return;

	frappe.call({
		method: "thameen_erp.overrides.procurement.pickup_vehicles_for_pos",
		args: { purchase_orders: JSON.stringify(receipt_po_names(frm)) },
		callback({ message: candidates }) {
			const by_vehicle = {};
			(candidates || []).forEach((c) => (by_vehicle[c.vehicle] = c));
			rows.forEach((row) => {
				const info = by_vehicle[row.vehicle];
				if (info) apply_pickup_warehouse_to_items(frm, info);
			});
			frm.refresh_field("items");
		},
	});
}

frappe.ui.form.on("Purchase Receipt Item", {
	item_code(frm) {
		reapply_all_pickup_warehouses(frm);
	},
	qty(frm) {
		reapply_all_pickup_warehouses(frm);
	},
	purchase_order(frm) {
		reapply_all_pickup_warehouses(frm);
	},
});

// The follow-on trip for a pickup that just reached "Trip Reached and
// Loaded" is created automatically, server-side, the moment its receipt is
// submitted (see procurement.link_shortfall_receipt_to_trip) — no dialog,
// nothing to confirm, since vehicle/driver/items all come straight off the
// truck with no choice involved. This button only ever appears as a
// fallback, for the rare case that automatic creation failed (already
// messaged on submit) and one of this receipt's pickup trips is still
// waiting on its follow-on trip.
function add_create_trip_after_pickup_button(frm) {
	frappe.call({
		method: "thameen_erp.overrides.procurement.pending_second_leg_trips",
		args: { purchase_receipt: frm.doc.name },
		callback({ message: pending }) {
			if (!pending || !pending.length) return;

			pending.forEach((row) => {
				const label = pending.length > 1 ? __("Create Delivery Trip — {0}", [row.vehicle]) : __("Create Delivery Trip");
				frm.add_custom_button(label, () => {
					frappe.call({
						method: "thameen_erp.overrides.procurement.create_trip_after_pickup",
						args: { purchase_receipt: frm.doc.name, pickup_trip: row.name },
						freeze: true,
						callback({ message: trip }) {
							if (trip) frm.reload_doc();
						},
					});
				}).addClass("btn-primary");
			});
		},
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
