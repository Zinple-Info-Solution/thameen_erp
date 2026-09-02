const STAGES = ["Scheduled", "Loading", "In Transit", "Delivered", "POD Pending", "Completed"];

// One field, one journey. Mirrors ROUTE_MAP in overrides/delivery_trip.py —
// change both together.
const ROUTE_MAP = {
	"Warehouse to Customer": ["Own Warehouse", "Customer"],
	"Supplier to Customer": ["Direct from Supplier", "Customer"],
	"Supplier to Warehouse": ["Direct from Supplier", "Own Warehouse"],
	"Supplier to Decide After Loading": ["Direct from Supplier", "Decide After Loading"],
};

const NEXT_STATUS = {
	Scheduled: "Loading",
	Loading: "In Transit",
	"In Transit": "Delivered",
	Delivered: "POD Pending",
	"POD Pending": "Completed",
};

const STAGE_HINT = {
	Loading: __("This raises a Material Transfer from the loading warehouse onto the vehicle warehouse."),
	Delivered: __("This raises the Delivery Note(s) from the vehicle warehouse against the Sales Order."),
	Completed: __("This releases the vehicle and closes the Sales Order if it is fully delivered."),
};

frappe.ui.form.on("Delivery Trip", {
	setup(frm) {
		frm.set_query("custom_loading_warehouse", () => ({
			filters: { is_group: 0, company: frm.doc.company, custom_is_vehicle_warehouse: 0 },
		}));

		frm.set_query("custom_sales_order", () => ({
			filters: { docstatus: 1, status: ["not in", ["Closed", "Completed", "Cancelled"]] },
		}));

		frm.set_query("source_warehouse", "custom_trip_items", () => ({
			filters: { is_group: 0, company: frm.doc.company, custom_is_vehicle_warehouse: 0 },
		}));

		frm.set_query("sales_order", "custom_trip_items", () => ({
			filters: { docstatus: 1, status: ["not in", ["Closed", "Completed", "Cancelled"]] },
		}));
		// Custom query so every plate in the dropdown shows its free space and
		// what is physically on the truck right now. `items` hides trucks
		// already carrying a different cement; `trip` stops this trip
		// counting against its own truck when free space is worked out.
		frm.set_query("vehicle", () => ({
			query: "thameen_erp.overrides.vehicle_stock.vehicle_query",
			filters: {
				custom_status: ["in", ["Available", "Assigned"]],
				items: distinct_items(frm),
				trip: frm.is_new() ? null : frm.doc.name,
			},
		}));

		frm.set_query("custom_purchase_order", () => ({
			filters: {
				docstatus: ["<", 2],
				supplier: frm.doc.custom_supplier || undefined,
				status: ["not in", ["Closed", "Cancelled"]],
			},
		}));

		frm.set_query("custom_loading_warehouse", () => ({
			filters: { is_group: 0, company: frm.doc.company, custom_is_vehicle_warehouse: 0 },
		}));
		frm.set_query("custom_supplier_warehouse", () => ({
			filters: { is_group: 0, custom_is_vehicle_warehouse: 0 },
		}));
		frm.set_query("custom_customer_warehouse", () => ({
			filters: { is_group: 0, custom_is_vehicle_warehouse: 0 },
		}));

		frm.set_query("custom_target_warehouse", () => ({
			filters: { is_group: 0, company: frm.doc.company, custom_is_vehicle_warehouse: 0 },
		}));

		// Freight is a service charge — a stock item here is rejected server-side.
		frm.set_query("custom_transportation_item", () => ({
			filters: { is_stock_item: 0, disabled: 0 },
		}));
	},

	refresh(frm) {
		add_destination_buttons(frm);
		if (frm.doc.custom_destination_type === "Decide After Loading") {
			frm.dashboard.add_comment(
				__("Destination not decided yet. The truck collects from the supplier; choose Deliver to Customer or Deliver to Own Warehouse before marking Delivered."),
				"orange",
				true
			);
		}
		if (frm.doc.custom_destination_type === "Own Warehouse") {
			frm.dashboard.add_comment(
				__("Inbound trip: {0} → {1}. Delivered unloads into the warehouse — no Delivery Note, no POD.", [
					frappe.utils.escape_html(frm.doc.custom_supplier || __("supplier")),
					frappe.utils.escape_html(frm.doc.custom_target_warehouse || __("target warehouse")),
				]),
				"blue",
				true
			);
		}
		add_stock_buttons(frm);

		if (frm.doc.docstatus === 0) {
			add_get_items_button(frm);
			add_load_buttons(frm);
			add_split_trip_button(frm);
			add_split_by_item_button(frm);
			add_procurement_buttons(frm);
			return;
		}
		if (frm.doc.docstatus !== 1) return;

		add_status_buttons(frm);
		show_progress(frm);
		add_view_buttons(frm);
		add_procurement_buttons(frm);
	},

	after_save(frm) {
		auto_split_by_item(frm);
	},

	// The route is the field the dispatcher picks. Supply Source and
	// Destination are derived from it here as well as in validate(), so the
	// fields that depend on them (Supplier, Target Warehouse, Purchase Order)
	// appear the moment the route is chosen rather than after a save.
	custom_trip_route(frm) {
		const pair = ROUTE_MAP[frm.doc.custom_trip_route];
		if (!pair) return;
		const [supply, destination] = pair;
		if (frm.doc.custom_supply_source !== supply) frm.set_value("custom_supply_source", supply);
		if (frm.doc.custom_destination_type !== destination) {
			frm.set_value("custom_destination_type", destination);
		}
		if (supply === "Direct from Supplier" && !frm.doc.custom_supplier) {
			frappe.db
				.get_single_value("Thameen Fleet Settings", "default_cement_supplier")
				.then((supplier) => {
					if (supplier) frm.set_value("custom_supplier", supplier);
				});
		}
		if (destination !== "Own Warehouse" && frm.doc.custom_target_warehouse) {
			frm.set_value("custom_target_warehouse", null);
		}
	},

	vehicle(frm) {
		if (!frm.doc.vehicle) return;
		frappe.db
			.get_value("Vehicle", frm.doc.vehicle, [
				"custom_assigned_driver",
				"custom_capacity",
				"last_odometer",
			])
			.then(({ message }) => {
				if (!message) return;
				if (message.custom_assigned_driver && !frm.doc.driver) {
					frm.set_value("driver", message.custom_assigned_driver);
				}
				if (message.last_odometer && !frm.doc.custom_starting_odometer) {
					frm.set_value("custom_starting_odometer", message.last_odometer);
				}
				after_vehicle_chosen(frm);
			});
	},

	custom_ending_odometer(frm) {
		const { custom_starting_odometer: s, custom_ending_odometer: e } = frm.doc;
		if (s && e && e >= s) frm.set_value("total_distance", e - s);
	},
});

frappe.ui.form.on("Delivery Trip Item", {
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item_code) return;
		frappe.db.get_value("Item", row.item_code, ["item_name", "stock_uom"]).then(({ message }) => {
			if (!message) return;
			frappe.model.set_value(cdt, cdn, "item_name", message.item_name);
			frappe.model.set_value(cdt, cdn, "stock_uom", message.stock_uom);
			if (!row.uom) frappe.model.set_value(cdt, cdn, "uom", message.stock_uom);
		});
	},

	qty(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		frappe.model.set_value(cdt, cdn, "amount", flt(row.qty) * flt(row.rate));
		check_stock(frm, row);
	},

	custom_trip_items_remove(frm) {
		frm.refresh_field("custom_trip_items");
	},
});

function check_stock(frm, row) {
	if (!row.item_code || !row.qty) return;
	frappe.call({
		method: "thameen_erp.api.check_stock_availability",
		args: { item_code: row.item_code, qty: row.qty },
		callback({ message }) {
			if (message && !message.sufficient) {
				frappe.show_alert({
					message: __("{0}: available {1}, requested {2}.", [
						row.item_code,
						message.available_qty,
						message.requested_qty,
					]),
					indicator: "orange",
				});
			}
		},
	});
}

function add_get_items_button(frm) {
	frm.add_custom_button(
		__("Get Items from Sales Order"),
		() => {
			const dialog = new frappe.ui.Dialog({
				title: __("Pull Pending Items"),
				fields: [
					{
						fieldname: "sales_order",
						fieldtype: "Link",
						options: "Sales Order",
						label: __("Sales Order"),
						reqd: 1,
						default: frm.doc.custom_sales_order,
						get_query: () => ({
							filters: {
								docstatus: 1,
								status: ["not in", ["Closed", "Completed", "Cancelled"]],
							},
						}),
					},
					{
						fieldname: "delivery_location",
						fieldtype: "Data",
						label: __("Delivery Location / Site"),
						default: frm.doc.custom_delivery_location,
						description: __("Leave blank to pull every pending row on the order."),
					},
				],
				primary_action_label: __("Get Items"),
				primary_action(values) {
					frappe.call({
						method: "thameen_erp.overrides.sales_order.get_trip_rows",
						args: values,
						freeze: true,
						callback({ message }) {
							dialog.hide();
							if (!message || !message.length) {
								frappe.msgprint(
									__("Nothing pending on that order for this location.")
								);
								return;
							}
							apply_rows(frm, message);
						},
					});
				},
			});
			dialog.show();
		},
		__("Get Items From")
	).addClass("btn-primary");
}

function apply_rows(frm, rows) {
	const locations = new Set(rows.map((r) => (r.delivery_location || "").trim()).filter(Boolean));

	if (locations.size > 1) {
		frappe.msgprint({
			title: __("Multiple Locations"),
			indicator: "orange",
			message: __(
				"Those rows go to {0} different sites: {1}. One trip serves one site — pull them one location at a time, or use Create > Delivery Trips on the Sales Order to plan them all at once.",
				[locations.size, Array.from(locations).join(", ")]
			),
		});
		return;
	}

	rows.forEach((row) => {
		const child = frm.add_child("custom_trip_items");
		Object.assign(child, {
			sales_order: row.sales_order,
			so_detail: row.so_detail,
			item_code: row.item_code,
			item_name: row.item_name,
			qty: row.qty,
			uom: row.uom,
			conversion_factor: row.conversion_factor,
			stock_uom: row.stock_uom,
			rate: row.rate,
			amount: row.amount,
			source_warehouse: row.source_warehouse,
			delivery_location: row.delivery_location,
		});
	});

	if (locations.size === 1 && !frm.doc.custom_delivery_location) {
		frm.set_value("custom_delivery_location", Array.from(locations)[0]);
	}

	frm.refresh_field("custom_trip_items");
	frappe.show_alert({ message: __("{0} row(s) added.", [rows.length]), indicator: "green" });

	// Several different items pulled onto one trip: save straight away so the
	// after_save handler can split them into one trip per item.
	if (distinct_items(frm).length > 1) {
		frappe.db.get_single_value("Thameen Fleet Settings", "one_item_per_trip").then((on) => {
			if (on) frm.save();
		});
	}
}

function add_status_buttons(frm) {
	const next = NEXT_STATUS[frm.doc.status];
	if (!next) return;

	frm.page.set_primary_action(__("Mark {0}", [next]), () => {
		if (next === "Completed" && !frm.doc.custom_pod_received) {
			frappe.msgprint({
				title: __("POD Required"),
				indicator: "red",
				message: __("Attach at least one Proof of Delivery document first."),
			});
			return;
		}

		const advance = () =>
			frappe.call({
				method: "thameen_erp.overrides.delivery_trip.set_trip_status",
				args: { trip: frm.doc.name, status: next },
				freeze: true,
				freeze_message: __("Updating trip…"),
				callback: () => {
					// The truck is loaded; if nobody has said where it tips,
					// ask now rather than at the last moment.
					if (next === "Loading" && frm.doc.custom_destination_type === "Decide After Loading") {
						frm.reload_doc().then(() => ask_destination(frm));
					} else {
						frm.reload_doc();
					}
				},
			});

		if (["Delivered"].includes(next) && frm.doc.custom_destination_type === "Decide After Loading") {
			ask_destination(frm);
			return;
		}

		if (STAGE_HINT[next]) {
			frappe.confirm(STAGE_HINT[next] + "<br><br>" + __("Continue?"), advance);
		} else {
			advance();
		}
	});
}

function add_view_buttons(frm) {
	const orders = Array.from(
		new Set((frm.doc.custom_trip_items || []).map((row) => row.sales_order).filter(Boolean))
	);

	orders.forEach((order) => {
		frm.add_custom_button(
			order,
			() => frappe.set_route("Form", "Sales Order", order),
			__("Sales Orders")
		);
	});

	const notes = Array.from(
		new Set((frm.doc.custom_trip_items || []).map((row) => row.delivery_note).filter(Boolean))
	);

	notes.forEach((note) => {
		frm.add_custom_button(
			note,
			() => frappe.set_route("Form", "Delivery Note", note),
			__("Delivery Notes")
		);
	});
}

function show_progress(frm) {
	const idx = STAGES.indexOf(frm.doc.status);
	if (idx < 0) return;
	frm.dashboard.add_progress(__("Trip Progress"), [
		{
			title: frm.doc.status,
			width: ((idx + 1) / STAGES.length) * 100 + "%",
			progress_class:
				frm.doc.status === "Completed" ? "progress-bar-success" : "progress-bar-info",
		},
	]);
}

// ---------------------------------------------------------------------------
// Vehicle load: does this trip actually fit on the chosen truck?
//
// Two numbers matter and they are not the same. Capacity is the truck's rating
// and never moves. Available is what is left once the loads already committed
// to that truck on other open trips are taken off. Planning against capacity
// when 180 of 300 is already spoken for is how trucks get double-booked.
// ---------------------------------------------------------------------------

function check_vehicle_load(frm, opts) {
	opts = opts || {};
	if (!frm.doc.vehicle || frm.doc.docstatus > 1) return;

	frappe.call({
		method: "thameen_erp.overrides.vehicle_load.get_vehicle_load",
		args: { vehicle: frm.doc.vehicle, trip: frm.doc.name },
		callback({ message }) {
			if (!message || !message.has_capacity) {
				if (opts.prompt && message && !message.has_capacity) {
					frappe.msgprint({
						title: __("Vehicle Not Rated"),
						indicator: "orange",
						message: __(
							"{0} has no Capacity set, so the load cannot be checked. Set Capacity on the Vehicle — the trip will not submit without it.",
							[frm.doc.vehicle]
						),
					});
				}
				return;
			}

			if (message.fits || !opts.prompt || frm.doc.docstatus !== 0) return;

			// Zero free space is not "a bit too much" — splitting cannot fix
			// it, because every load would still need somewhere to go. Say so
			// instead of opening a split plan that cannot balance.
			if (flt(message.available_qty) <= 0.001) {
				show_no_room(frm, message);
				return;
			}

			offer_split(frm, message);
		},
	});
}

function show_no_room(frm, load) {
	const holding = (load.on_truck_items || [])
		.map((i) => `${frappe.utils.escape_html(i.item_code)} ${format_number(i.qty)}`)
		.join(", ");

	const html = `<p>${__("{0} has no free space, so nothing can be planned onto it.", [
		`<b>${frappe.utils.escape_html(frm.doc.vehicle)}</b>`,
	])}</p>
	<table class="table table-bordered small">
		<tbody>
			<tr><td>${__("Rated capacity")}</td><td class="text-right">${format_number(load.capacity)}</td></tr>
			<tr><td>${__("Physically on truck")}</td><td class="text-right">${format_number(load.on_truck_qty)}</td></tr>
			<tr><td>${__("Promised to other trips")}</td><td class="text-right">${format_number(load.committed_qty)}</td></tr>
			<tr><td><b>${__("Free")}</b></td><td class="text-right"><b>0</b></td></tr>
		</tbody>
	</table>
	${holding ? `<p class="small text-muted">${__("Currently carrying")}: ${holding}</p>` : ""}
	<p class="small text-muted">${__(
		"Unload the truck, choose another vehicle, or move this trip to a day when the truck is free."
	)}</p>`;

	const d = new frappe.ui.Dialog({
		title: __("No Room on Truck"),
		fields: [{ fieldtype: "HTML", options: html }],
		primary_action_label: __("Open Vehicle"),
		primary_action() {
			d.hide();
			frappe.set_route("Form", "Vehicle", frm.doc.vehicle);
		},
	});
	d.show();
}

// ---------------------------------------------------------------------------
// One truck chosen, one dialog at most
//
// Two different questions get asked when a vehicle is picked, and they have a
// strict order:
//
//   1. Is the cement THERE?   — yard + truck, via check_trip_stock
//   2. Does it FIT?           — capacity, via check_vehicle_load
//
// Stock wins. Splitting a trip you cannot fill is busywork: the split just
// turns one unfillable trip into three, and the dispatcher still has to go and
// buy the cement. So when stock is short the Insufficient Stock dialog opens
// on its own and the split is not offered at all. Only once the cement exists
// does "it will not fit on one truck" become the real problem.
//
// They also used to fire as two parallel calls, so whichever server response
// landed second threw its dialog on top of the first. Now they are sequenced.
// ---------------------------------------------------------------------------

function after_vehicle_chosen(frm) {
	if (!frm.doc.vehicle || frm.doc.docstatus !== 0) return;
	if (!(frm.doc.custom_trip_items || []).length) return;

	// A trip that has never been saved has no rows server-side to check.
	if (frm.is_new()) {
		check_vehicle_load(frm, { prompt: true });
		return;
	}

	frappe.call({
		method: "thameen_erp.overrides.procurement.check_trip_stock",
		args: { trip: frm.doc.name, vehicle: frm.doc.vehicle },
		callback({ message }) {
			if (!message) return;

			// Nothing to fill the trip with. This dialog, and only this one —
			// a direct trip says so in its own words, because its stock is a
			// Purchase Order rather than a yard.
			if (!message.sufficient) {
				if (message.supply_source === "Direct from Supplier") {
					show_direct_supply_check(frm, message);
				} else {
					offer_procurement(frm, message);
				}
				return;
			}

			// Cement exists. Now: is there room on this truck for it?
			check_vehicle_load(frm, { prompt: true });
		},
	});
}

function offer_split(frm, load) {
	const committed_rows = (load.committed_trips || [])
		.map(
			(trip) =>
				`<tr><td>${frappe.utils.get_form_link("Delivery Trip", trip.name, true)}</td>
				 <td>${__(trip.status)}</td>
				 <td class="text-right">${format_number(trip.qty)}</td></tr>`
		)
		.join("");

	const why = committed_rows
		? `<details class="mb-2"><summary class="small text-muted">${__("Already committed to this truck")}</summary>
		   <table class="table table-bordered small mt-1">
		     <thead><tr><th>${__("Trip")}</th><th>${__("Status")}</th><th class="text-right">${__("Qty")}</th></tr></thead>
		     <tbody>${committed_rows}</tbody>
		   </table></details>`
		: "";


	const dialog = new frappe.ui.Dialog({
		title: __("Split Trip"),
		size: "extra-large",
		fields: [
			{ fieldtype: "HTML", fieldname: "summary", options: why },
			{
				fieldname: "use_capacity",
				fieldtype: "Check",
				label: __("Plan against full capacity"),
			},
			{ fieldtype: "Section Break" },
			{ fieldtype: "HTML", fieldname: "plan" },
		],
		primary_action_label: __("Split into Several Trips"),
		primary_action() {
			const use_capacity = dialog.get_value("use_capacity") ? 1 : 0;
			frappe.call({
				method: "thameen_erp.overrides.vehicle_load.split_trip",
				args: {
					trip: frm.doc.name,
					vehicle: frm.doc.vehicle,
					use_capacity,
					plan: JSON.stringify(collect_plan(dialog)),
				},
				freeze: true,
				freeze_message: __("Splitting…"),
				callback() {
					dialog.hide();
					frm.reload_doc();
				},
			});
		},
	});

	dialog.fields_dict.use_capacity.$input.on("change", () => refresh_plan(frm, dialog));
	dialog.show();
	refresh_plan(frm, dialog);
}

function refresh_plan(frm, dialog) {
	const wrapper = dialog.fields_dict.plan.$wrapper;
	wrapper.html(`<p class="text-muted">${__("Working out the split…")}</p>`);

	frappe.call({
		method: "thameen_erp.overrides.vehicle_load.preview_split",
		args: {
			trip: frm.doc.name,
			vehicle: frm.doc.vehicle,
			use_capacity: dialog.get_value("use_capacity") ? 1 : 0,
		},
		callback({ message }) {
			if (!message || !message.loads) {
				wrapper.html(`<p class="text-muted">${__("Nothing to split.")}</p>`);
				return;
			}
			// Seed the editable plan from the suggestion.
			const start = message.departure_time ? frappe.datetime.str_to_obj(message.departure_time) : new Date();
			dialog.plan = message.loads.map((load, index) => ({
				vehicle: index === 0 ? frm.doc.vehicle : null,
				departure_time: frappe.datetime.obj_to_str(frappe.datetime.add_days(start, index)),
				items: load.items.map((i) => ({ item_code: i.item_code, uom: i.uom, qty: flt(i.qty) })),
			}));
			dialog.vehicles = message.vehicles || [];
			dialog.plan_use_capacity = dialog.get_value("use_capacity") ? 1 : 0;
			dialog.totals = {};
			dialog.plan.forEach((l) => l.items.forEach((i) => (dialog.totals[i.item_code] = flt(dialog.totals[i.item_code]) + i.qty)));
			// Size every row against the truck on it before the first render.
			reflow_plan(dialog);
			render_plan(frm, dialog);
		},
	});
}

// ---------------------------------------------------------------------------
// The split plan
//
// The plan is a list of loads. Each load has one truck, one departure and a
// set of item quantities. Two invariants hold at all times:
//
//   * per item, the quantities across all loads add up to exactly what the
//     trip carries — a split moves cement between trucks, it never creates or
//     destroys any;
//   * a load never carries more than its truck's free space.
//
// Choosing a truck therefore RESIZES that load to the truck and pushes the
// remainder down the list. Pick a 10-tonne truck for the first load of a
// 50-tonne trip and it takes 10; pick a 20-tonne truck for the second and it
// takes 20, with the last 20 spread over whatever rows are left.
// ---------------------------------------------------------------------------

const PLAN_TOL = 0.001;

function load_total(load) {
	return (load.items || []).reduce((sum, item) => sum + flt(item.qty), 0);
}

function plan_grand_total(dialog) {
	return Object.keys(dialog.totals || {}).reduce((sum, code) => sum + flt(dialog.totals[code]), 0);
}

function vehicle_of(dialog, name) {
	return (dialog.vehicles || []).find((v) => v.name === name);
}

// Free space on a truck, or null when it has no Capacity rated — an unrated
// truck cannot be planned against, so its load is left exactly as it is.
function vehicle_free(dialog, name) {
	const v = vehicle_of(dialog, name);
	if (!v || !flt(v.capacity)) return null;
	// "Plan against full capacity" means the other trips on that truck will be
	// done by the time this one loads, so ignore what is committed.
	if (dialog.plan_use_capacity) return flt(v.capacity);
	return flt(v.free !== undefined ? v.free : v.available);
}

// Trucks already chosen on another row. A truck carries one load per plan.
function taken_vehicles(dialog, except_index) {
	return new Set(
		(dialog.plan || [])
			.map((load, i) => (i === except_index ? null : load.vehicle))
			.filter(Boolean)
	);
}

// Work out how much each load should carry, then rewrite the quantities to
// match. A load with a truck takes exactly what that truck can hold, in row
// order. Everything still unplaced sits on ONE row — the first without a truck
// — so the tail collapses to a single remainder instead of splitting into
// fractions across rows nobody has assigned yet.
function reflow_plan(dialog) {
	const plan = dialog.plan || [];
	if (!plan.length) return;

	const grand = plan_grand_total(dialog);
	const targets = new Array(plan.length).fill(0);
	let left = grand;

	plan.forEach((load, i) => {
		// A quantity typed by hand is the dispatcher's decision and outranks
		// the truck's rating — they may know the truck will be emptied first.
		if (load.manual) {
			targets[i] = Math.max(Math.min(load_total(load), left), 0);
			left -= targets[i];
			return;
		}
		const free = vehicle_free(dialog, load.vehicle);
		if (free === null) return;
		targets[i] = Math.max(Math.min(free, left), 0);
		left -= targets[i];
	});

	if (left > PLAN_TOL) {
		const idx = plan.findIndex((l) => !l.manual && vehicle_free(dialog, l.vehicle) === null);
		if (idx === -1) {
			// Every row is spoken for and there is still cement: it needs a
			// further truck, not an overloaded one.
			plan.push({
				vehicle: null,
				departure_time: frappe.datetime.add_days(
					plan[plan.length - 1].departure_time || frappe.datetime.get_today(),
					1
				),
				items: [],
			});
			targets.push(left);
		} else {
			targets[idx] = left;
		}
		left = 0;
	}

	apply_targets(dialog, targets);
	prune_empty_loads(dialog);
}

// Pour the trip's quantities into the loads, filling each up to its target.
// Rebuilding beats nudging: it cannot drift out of balance.
function apply_targets(dialog, targets) {
	const codes = Object.keys(dialog.totals || {});
	const pool = {};
	codes.forEach((code) => (pool[code] = flt(dialog.totals[code])));

	const uom_of = {};
	(dialog.plan || []).forEach((load) =>
		(load.items || []).forEach((item) => {
			if (item.uom) uom_of[item.item_code] = item.uom;
		})
	);

	dialog.plan.forEach((load, index) => {
		let room = Math.max(flt(targets[index]), 0);
		const items = [];
		codes.forEach((code) => {
			if (room <= PLAN_TOL || pool[code] <= PLAN_TOL) return;
			const take = Math.min(pool[code], room);
			items.push({ item_code: code, uom: uom_of[code], qty: take });
			pool[code] -= take;
			room -= take;
		});
		load.items = items;
	});

	// Rounding crumbs go on the last load so the balance still reads zero.
	const leftovers = codes.filter((code) => pool[code] > PLAN_TOL);
	if (leftovers.length) {
		const last = dialog.plan[dialog.plan.length - 1];
		leftovers.forEach((code) => {
			const existing = last.items.find((i) => i.item_code === code);
			if (existing) existing.qty = flt(existing.qty) + pool[code];
			else last.items.push({ item_code: code, uom: uom_of[code], qty: pool[code] });
			pool[code] = 0;
		});
	}
}

// A load with nothing on it is noise — unless it is the first, which is this
// trip itself and always stays.
function prune_empty_loads(dialog) {
	dialog.plan = dialog.plan.filter((load, index) => index === 0 || load_total(load) > PLAN_TOL);
}

function vehicle_select_html(dialog, value, cls, index) {
	const taken = taken_vehicles(dialog, index);
	const opts = [`<option value="">${__("— choose later —")}</option>`]
		.concat(
			(dialog.vehicles || [])
				.filter((v) => v.name === value || !taken.has(v.name))
				.map((v) => {
					const free = flt(v.free !== undefined ? v.free : v.available);
					// What it is carrying, so an empty truck is obvious at a
					// glance. Trucks holding a different cement are already
					// filtered out server-side.
					const holding = (v.on_truck_items || []).length
						? (v.on_truck_items || [])
								.map((i) => `${i.item_code} ${format_number(i.qty)}`)
								.join(", ")
						: __("empty");
					return (
						`<option value="${frappe.utils.escape_html(v.name)}" ${v.name === value ? "selected" : ""}>` +
						`${frappe.utils.escape_html(v.name)} · ${__("free")} ${format_number(free)} ${__("of")} ${format_number(v.capacity)}` +
						` · ${frappe.utils.escape_html(holding)}</option>`
					);
				})
		)
		.join("");
	return `<select class="form-control input-xs ${cls}" data-index="${index}">${opts}</select>`;
}

// What the chosen truck is physically carrying, named by its warehouse. The
// free-space figures are already on every option in the Vehicle dropdown, so
// repeating them here was the same numbers twice on one row.
function vehicle_state_html(dialog, load) {
	if (!load.vehicle) return `<span class="text-muted">—</span>`;
	const v = vehicle_of(dialog, load.vehicle);
	if (!v) return `<span class="text-muted">—</span>`;
	if (!flt(v.capacity)) return `<span class="text-danger small">${__("no capacity set")}</span>`;
	if (!flt(v.on_truck)) return `<span class="text-muted small">${__("empty")}</span>`;

	const lines = (v.on_truck_items || []).length
		? (v.on_truck_items || [])
				.map((i) => `${frappe.utils.escape_html(i.item_code)} <b>${format_number(i.qty)}</b>`)
				.join("<br>")
		: `<b>${format_number(v.on_truck)}</b>`;

	return (
		`<span class="small">${frappe.utils.escape_html(v.warehouse || load.vehicle)}<br>` +
		`${lines}</span>`
	);
}

function render_plan(frm, dialog) {
	const wrapper = dialog.fields_dict.plan.$wrapper;
	const plan = dialog.plan;

	const rows = plan
		.map((load, index) => {
			const total = load_total(load);
			const free = vehicle_free(dialog, load.vehicle);
			const over = free !== null && total > free + PLAN_TOL;
			const items = load.items.length
				? load.items
						.map(
							(i, k) =>
								`<div class="d-flex align-items-center mb-1">
									<span style="min-width:90px">${frappe.utils.escape_html(i.item_code)}</span>
									<input type="number" step="any" min="0" class="form-control input-xs plan-qty" style="width:110px"
										data-index="${index}" data-item="${k}" value="${i.qty}">
									<span class="text-muted small ml-1">${frappe.utils.escape_html(i.uom || "")}</span>
								</div>`
						)
						.join("")
				: `<span class="text-muted small">${__("nothing on this trip")}</span>`;
			const label =
				index === 0
					? `<b>${__("This trip")}</b><br><span class="text-muted small">${frm.doc.name}</span>`
					: `<b>${__("New trip {0}", [index + 1])}</b>` +
					  (plan.length > 2 ? `<br><a class="small text-danger plan-remove" data-index="${index}">${__("remove")}</a>` : "");
			return `<tr class="${over ? "table-warning" : ""}">
				<td>${label}</td>
				<td>${items}</td>
				<td class="text-right"><b>${format_number(total)}</b>
					${over ? `<br><span class="text-danger small">${__("over by {0}", [format_number(total - free)])}</span>` : ""}
					${load.manual ? `<br><a class="small text-muted plan-auto" data-index="${index}">${__("auto")}</a>` : ""}</td>
				<td>${vehicle_select_html(dialog, load.vehicle, "plan-vehicle", index)}</td>
				<td>${vehicle_state_html(dialog, load)}</td>
				<td><input type="date" class="form-control input-xs plan-date" data-index="${index}"
					value="${(load.departure_time || "").slice(0, 10)}"></td>
			</tr>`;
		})
		.join("");

	// Per-item balance: must be exactly zero to split.
	const placed = {};
	plan.forEach((l) => l.items.forEach((i) => (placed[i.item_code] = flt(placed[i.item_code]) + flt(i.qty))));
	const balance = Object.keys(dialog.totals).map((code) => ({
		code,
		diff: flt(placed[code]) - flt(dialog.totals[code]),
	}));
	const balanced = balance.every((b) => Math.abs(b.diff) < PLAN_TOL);
	const balance_html = balance
		.map(
			(b) =>
				`<span class="${Math.abs(b.diff) < PLAN_TOL ? "text-success" : "text-danger"} mr-3">` +
				`${frappe.utils.escape_html(b.code)}: ${format_number(placed[b.code])} / ${format_number(dialog.totals[b.code])}` +
				(Math.abs(b.diff) < PLAN_TOL ? " ✓" : ` (${b.diff > 0 ? "+" : ""}${format_number(b.diff)})`) +
				`</span>`
		)
		.join("");

	// Row 1 IS this trip, so it must keep something. When its truck has no room
	// the message names the truck and says why, rather than just refusing.
	const first_empty = load_total(plan[0]) <= PLAN_TOL;
	let warning = "";
	if (first_empty) {
		const v = vehicle_of(dialog, plan[0].vehicle);
		warning = v
			? `<div class="text-danger small mb-2">${__(
					"{0} has no room — rated {1}, {2} already on it. Pick another truck for row 1, or unload it first.",
					[v.name, format_number(v.capacity), format_number(Math.max(flt(v.on_truck), flt(v.committed)))]
			  )}</div>`
			: `<div class="text-danger small mb-2">${__("Row 1 needs a truck with free space.")}</div>`;
	}

	wrapper.html(`
		<div class="d-flex justify-content-between align-items-center mb-2">
			<span class="small text-muted">${__("{0} trip(s)", [plan.length])}</span>
			<span>
				<button class="btn btn-xs btn-default plan-add">${__("+ Add trip")}</button>
				<button class="btn btn-xs btn-default plan-same-truck ml-1">${__("Same truck, one day apart")}</button>
			</span>
		</div>
		<table class="table table-bordered small">
			<thead><tr>
				<th style="width:14%">${__("Trip")}</th>
				<th>${__("Items & qty")}</th>
				<th class="text-right" style="width:9%">${__("Total")}</th>
				<th style="width:24%">${__("Vehicle")}</th>
				<th style="width:16%">${__("Available stock")}</th>
				<th style="width:13%">${__("Departure")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
		<div class="small mb-2">${balance_html}</div>
		${warning}`);

	// A row carrying more than its truck can hold is not a plan, it is the
	// same problem moved sideways. The rows are already flagged in red above;
	// this stops the button acting on them.
	const any_over = plan.some((l) => {
		const free = vehicle_free(dialog, l.vehicle);
		return free !== null && load_total(l) > free + PLAN_TOL;
	});

	dialog.get_primary_btn().prop("disabled", !balanced || first_empty || any_over);

	wrapper.find(".plan-qty").on("change", function () {
		const index = parseInt($(this).data("index"), 10);
		const k = parseInt($(this).data("item"), 10);
		const item = plan[index].items[k];
		// Never accept more of an item than the trip actually carries.
		const elsewhere = plan.reduce((sum, l, j) => {
			if (j === index) return sum;
			const other = l.items.find((x) => x.item_code === item.item_code);
			return sum + (other ? flt(other.qty) : 0);
		}, 0);
		const ceiling = flt(dialog.totals[item.item_code]) - elsewhere + 0;
		const new_qty = Math.min(Math.max(flt($(this).val()), 0), Math.max(flt(item.qty), ceiling));
		const diff = new_qty - flt(item.qty);
		item.qty = new_qty;
		plan[index].manual = true;
		// Rebalance: the last OTHER trip carrying this item absorbs the change.
		for (let j = plan.length - 1; j >= 0; j--) {
			if (j === index) continue;
			const other = plan[j].items.find((x) => x.item_code === item.item_code);
			if (other) {
				other.qty = Math.max(flt(other.qty) - diff, 0);
				break;
			}
		}
		render_plan(frm, dialog);
	});
	wrapper.find(".plan-vehicle").on("change", function () {
		const index = parseInt($(this).data("index"), 10);
		plan[index].vehicle = $(this).val() || null;
		// Picking a truck resizes its load to the space that truck has, and
		// pushes whatever no longer fits onto the rows below. That is a fresh
		// decision, so any quantity typed on this row is released.
		plan[index].manual = false;
		reflow_plan(dialog);
		render_plan(frm, dialog);
	});
	wrapper.find(".plan-auto").on("click", function () {
		// Hand back to the automatic fit for this row.
		plan[parseInt($(this).data("index"), 10)].manual = false;
		reflow_plan(dialog);
		render_plan(frm, dialog);
	});
	wrapper.find(".plan-date").on("change", function () {
		plan[parseInt($(this).data("index"), 10)].departure_time = $(this).val();
	});
	wrapper.find(".plan-remove").on("click", function () {
		const index = parseInt($(this).data("index"), 10);
		plan.splice(index, 1);
		reflow_plan(dialog);
		render_plan(frm, dialog);
	});
	wrapper.find(".plan-add").on("click", () => {
		// Halve the biggest row without a truck. Appending an empty row would
		// be pruned straight away, since the remainder lives on one row.
		let idx = -1;
		plan.forEach((l, i) => {
			if (l.vehicle) return;
			if (idx === -1 || load_total(l) > load_total(plan[idx])) idx = i;
		});
		if (idx === -1) idx = plan.length - 1;

		const half = load_total(plan[idx]) / 2;
		if (half <= PLAN_TOL) return;

		const moved = [];
		plan[idx].items.forEach((item) => {
			const take = flt(item.qty) / 2;
			item.qty = flt(item.qty) - take;
			moved.push({ item_code: item.item_code, uom: item.uom, qty: take });
		});
		plan.splice(idx + 1, 0, {
			vehicle: null,
			departure_time: frappe.datetime.add_days(
				plan[idx].departure_time || frappe.datetime.get_today(),
				1
			),
			items: moved,
		});
		render_plan(frm, dialog);
	});
	wrapper.find(".plan-same-truck").on("click", () => {
		// One truck doing every load, a day apart. Capacity is per load here,
		// so the same plate legitimately repeats.
		const base = plan[0].departure_time || frappe.datetime.get_today();
		plan.forEach((l, i) => {
			l.vehicle = frm.doc.vehicle;
			l.departure_time = frappe.datetime.add_days(base, i);
		});
		render_plan(frm, dialog);
	});
}

function collect_plan(dialog) {
	return (dialog.plan || [])
		.map((l) => ({
			vehicle: l.vehicle || null,
			departure_time: l.departure_time || null,
			items: l.items.filter((i) => flt(i.qty) > 0).map((i) => ({ item_code: i.item_code, qty: flt(i.qty) })),
		}))
		.filter((l) => l.items.length);
}

// Split is now a button, not only something the app offers when it notices the
// truck is too small. A dispatcher who already knows the load needs two trucks
// should not have to overfill one first to be shown the dialog.
function add_split_trip_button(frm) {
	if (!frm.doc.vehicle || frm.is_new()) return;
	if (!(frm.doc.custom_trip_items || []).length) return;

	frm.add_custom_button(__("Split Trip"), () => {
		frappe.call({
			method: "thameen_erp.overrides.vehicle_load.get_vehicle_load",
			args: { vehicle: frm.doc.vehicle, trip: frm.doc.name },
			freeze: true,
			callback({ message }) {
				if (!message || !message.has_capacity) {
					frappe.msgprint(
						__("Set a Capacity on {0} before splitting — the split sizes each trip against it.", [
							frm.doc.vehicle,
						])
					);
					return;
				}
				offer_split(frm, message);
			},
		});
	});
}

// Two read-only views that used to be dashboard banners shouting on every
// refresh. On a button they are there when wanted and silent otherwise.
function add_stock_buttons(frm) {
	if (frm.doc.vehicle) {
		frm.add_custom_button(__("Check Stock Against Vehicle"), () => show_vehicle_check(frm), __("Stock"));
	}
	frm.add_custom_button(__("View All Warehouse Stock"), () => show_all_warehouse_stock(frm), __("Stock"));
}

// What the truck holds, what it is promised, and whether this trip fits.
function show_vehicle_check(frm) {
	frappe.call({
		method: "thameen_erp.overrides.vehicle_load.get_vehicle_load",
		args: { vehicle: frm.doc.vehicle, trip: frm.doc.name },
		freeze: true,
		callback({ message }) {
			if (!message) return;
			const load = message;

			const rows = (load.on_truck_items || [])
				.map(
					(i) => `<tr>
						<td>${frappe.utils.escape_html(i.item_code)}</td>
						<td class="text-right">${format_number(i.qty)}</td>
						<td class="text-right">${format_number(i.on_loaded_trips || 0)}</td>
					</tr>`
				)
				.join("");

			const fits = load.fits;
			const html = `
				<table class="table table-bordered small">
					<tbody>
						<tr><td>${__("Rated capacity")}</td><td class="text-right">${format_number(load.capacity)}</td></tr>
						<tr><td>${__("Promised to other trips that day")}</td><td class="text-right">${format_number(load.committed_qty)}</td></tr>
						<tr><td><b>${__("Free")}</b></td><td class="text-right"><b>${format_number(load.available_qty)}</b></td></tr>
						<tr><td>${__("This trip needs")}</td><td class="text-right">${format_number(load.planned_qty)}</td></tr>
						<tr class="${fits ? "" : "table-danger"}">
							<td><b>${fits ? __("Fits") : __("Over by")}</b></td>
							<td class="text-right"><b>${fits ? "✓" : format_number(load.overflow_qty)}</b></td>
						</tr>
					</tbody>
				</table>
				${rows
					? `<div class="small text-muted mb-1">${__("On the truck now")}</div>
					   <table class="table table-bordered small">
						 <thead><tr><th>${__("Item")}</th><th class="text-right">${__("Qty")}</th>
						 <th class="text-right">${__("On loaded trips")}</th></tr></thead>
						 <tbody>${rows}</tbody>
					   </table>`
					: `<p class="text-muted small">${__("The truck is empty.")}</p>`}`;

			const d = new frappe.ui.Dialog({
				title: __("{0} — stock check", [frm.doc.vehicle]),
				size: "small",
				fields: [{ fieldtype: "HTML", options: html }],
			});
			if (!fits) {
				d.set_primary_action(__("Split Trip"), () => {
					d.hide();
					offer_split(frm, load);
				});
			}
			d.show();
		},
	});
}

// Every warehouse holding any item on this trip, so the dispatcher can see
// where to load from without leaving the form.
function show_all_warehouse_stock(frm) {
	const items = distinct_items(frm);
	if (!items.length) {
		frappe.msgprint(__("Add an item to the trip first."));
		return;
	}

	frappe.call({
		method: "thameen_erp.api.stock_by_warehouse",
		args: { items: JSON.stringify(items) },
		freeze: true,
		callback({ message }) {
			const lines = message || [];
			const body = lines.length
				? `<table class="table table-bordered small">
					<thead><tr>
						<th>${__("Item")}</th><th>${__("Warehouse")}</th>
						<th class="text-right">${__("Qty")}</th><th>${__("Type")}</th>
					</tr></thead>
					<tbody>${lines
						.map(
							(r) => `<tr>
								<td>${frappe.utils.escape_html(r.item_code)}</td>
								<td>${frappe.utils.escape_html(r.warehouse)}</td>
								<td class="text-right">${format_number(r.qty)}</td>
								<td>${r.is_vehicle ? __("truck") : __("yard")}</td>
							</tr>`
						)
						.join("")}</tbody>
				   </table>`
				: `<p class="text-muted">${__("None of these items is in any warehouse.")}</p>`;

			new frappe.ui.Dialog({
				title: __("Stock by warehouse"),
				size: "large",
				fields: [{ fieldtype: "HTML", options: body }],
			}).show();
		},
	});
}

function add_load_buttons(frm) {
	if (!frm.doc.vehicle || frm.is_new()) return;

	frm.add_custom_button(__("Check Load"), () => check_vehicle_load(frm, { prompt: true }));

	frm.add_custom_button(__("Split Into Trips"), () => {
		frappe.call({
			method: "thameen_erp.overrides.vehicle_load.get_vehicle_load",
			args: { vehicle: frm.doc.vehicle, trip: frm.doc.name },
			callback({ message }) {
				if (!message || !message.has_capacity) {
					frappe.msgprint(
						__("Set a Capacity on {0} before splitting.", [frm.doc.vehicle])
					);
					return;
				}
				// Deliberately offered even when it fits: a dispatcher may want
				// two half-loaded trucks going out together rather than one.
				offer_split(frm, message);
			},
		});
	});
}


// ---------------------------------------------------------------------------
// One item per trip
// ---------------------------------------------------------------------------

function distinct_items(frm) {
	return Array.from(new Set((frm.doc.custom_trip_items || []).map((r) => r.item_code).filter(Boolean)));
}

function auto_split_by_item(frm) {
	if (frm.doc.docstatus !== 0 || frm.is_new()) return;
	const items = distinct_items(frm);
	if (items.length <= 1) return;

	frappe.db.get_single_value("Thameen Fleet Settings", "one_item_per_trip").then((on) => {
		if (!on) return;
		frappe.show_alert({
			message: __("{0} different items on one trip — splitting into {0} trips…", [items.length]),
			indicator: "orange",
		});
		frappe.call({
			method: "thameen_erp.overrides.procurement.split_trip_by_item",
			args: { trip: frm.doc.name },
			freeze: true,
			freeze_message: __("Creating one trip per item…"),
			callback: () => frm.reload_doc(),
		});
	});
}

function add_split_by_item_button(frm) {
	if (frm.is_new()) return;
	const items = distinct_items(frm);
	if (items.length <= 1) return;

	frappe.db.get_single_value("Thameen Fleet Settings", "one_item_per_trip").then((on) => {
		if (!on) return;
		frm.dashboard.add_comment(
			__("This trip carries {0} different items ({1}). One trip carries one item — it will not submit until it is split.", [
				items.length,
				items.map(frappe.utils.escape_html).join(", "),
			]),
			"orange",
			true
		);
		frm.add_custom_button(__("Split by Item"), () => {
			frappe.confirm(
				__("Keep {0} on this trip and move each other item onto its own draft trip?", [
					frappe.utils.escape_html(items[0]),
				]),
				() =>
					frappe.call({
						method: "thameen_erp.overrides.procurement.split_trip_by_item",
						args: { trip: frm.doc.name },
						freeze: true,
						freeze_message: __("Splitting by item…"),
						callback: () => frm.reload_doc(),
					})
			);
		}).addClass("btn-warning");
	});
}

// ---------------------------------------------------------------------------
// Is the cement actually there?
//
// The load check above asks whether the trip FITS on the truck. This asks
// whether the trip can be FILLED: what is already on the truck and free, what
// the loading warehouse holds, and what is simply missing. A missing quantity
// gets a dialog that raises the purchase rather than leaving the dispatcher to
// discover an empty yard at loading time.
// ---------------------------------------------------------------------------

// Two callers, two behaviours.
//
//   auto (vehicle chosen)  silent when the trip is covered; opens the
//                          Insufficient Stock dialog when it is not.
//   show (Check Stock)     always opens the full per-row table.
//
// The old red "Stock check — NOT covered" dashboard banner is gone either way:
// it repainted on every refresh whether or not anyone had asked.
function check_trip_stock(frm, opts) {
	opts = opts || {};
	if (frm.is_new() || frm.doc.docstatus > 1) return;
	if (!(frm.doc.custom_trip_items || []).length) {
		if (opts.show) frappe.msgprint(__("Add an item to the trip first."));
		return;
	}

	frappe.call({
		method: "thameen_erp.overrides.procurement.check_trip_stock",
		args: { trip: frm.doc.name, vehicle: frm.doc.vehicle },
		freeze: !!opts.show,
		callback({ message }) {
			if (!message) return;

			if (opts.show) {
				show_stock_check(frm, message);
				return;
			}

			// Covered — say nothing at all.
			if (message.sufficient) return;
			if (message.supply_source === "Direct from Supplier") return;
			if (frm.doc.docstatus !== 0) return;
			offer_procurement(frm, message);
		},
	});
}

// Warehouse name and qty per row. A bare "short 50" never told anyone which
// yard to send the truck to, which is the only reason to open this.
function show_stock_check(frm, check) {
	if (check.supply_source === "Direct from Supplier") {
		show_direct_supply_check(frm, check);
		return;
	}

	const rows = (check.rows || [])
		.map(
			(r) => `<tr class="${r.shortfall ? "table-danger" : ""}">
				<td>${frappe.utils.escape_html(r.item_code)}</td>
				<td class="text-right">${format_number(r.planned_qty)}</td>
				<td>${r.on_truck_free ? frappe.utils.escape_html(frm.doc.vehicle || "") : ""}</td>
				<td class="text-right">${r.on_truck_free ? format_number(r.on_truck_free) : "—"}</td>
				<td>${frappe.utils.escape_html(r.source_warehouse || "")}</td>
				<td class="text-right">${r.from_source ? format_number(r.from_source) : "—"}</td>
				<td class="text-right">${r.shortfall ? `<b>${format_number(r.shortfall)}</b>` : "—"}</td>
			</tr>`
		)
		.join("");

	const html = `<table class="table table-bordered small">
		<thead><tr>
			<th>${__("Item")}</th>
			<th class="text-right">${__("Planned")}</th>
			<th>${__("Vehicle")}</th>
			<th class="text-right">${__("On truck")}</th>
			<th>${__("Warehouse")}</th>
			<th class="text-right">${__("In warehouse")}</th>
			<th class="text-right">${__("Short")}</th>
		</tr></thead>
		<tbody>${rows}</tbody>
	</table>`;

	const d = new frappe.ui.Dialog({
		title: check.sufficient ? __("Stock check — covered") : __("Stock check — not covered"),
		size: "large",
		fields: [{ fieldtype: "HTML", options: html }],
	});

	if (!check.sufficient && frm.doc.docstatus === 0) {
		d.set_primary_action(__("Order Shortfall…"), () => {
			d.hide();
			offer_procurement(frm, check);
		});
	}
	d.show();
}

function show_direct_supply_check(frm, check) {
	const po = check.purchase_order;
	let text;
	if (!po) {
		text = __("This trip collects straight from the supplier, but it has no Purchase Order yet. The Purchase Order is the only thing backing the cement — the trip will not submit without one.");
	} else if (check.po_docstatus === 0) {
		text = __("{0} is still a draft. The buyer must submit it before this trip can go.", [
			frappe.utils.get_form_link("Purchase Order", po, true),
		]);
	} else if (!check.sufficient) {
		text = __("{0} is {1}, so it cannot supply this trip.", [
			frappe.utils.get_form_link("Purchase Order", po, true),
			check.po_status,
		]);
	} else {
		text = __("{0} is submitted. Loading will receive the cement straight onto the truck.", [
			frappe.utils.get_form_link("Purchase Order", po, true),
		]);
	}

	const d = new frappe.ui.Dialog({
		title: check.sufficient ? __("Direct from supplier") : __("No Stock Behind This Trip"),
		fields: [{ fieldtype: "HTML", options: `<p>${text}</p>` }],
	});

	if (po) {
		d.set_primary_action(__("Open Purchase Order"), () => {
			d.hide();
			frappe.set_route("Form", "Purchase Order", po);
		});
	} else if (frm.doc.docstatus === 0 && frm.doc.custom_supplier) {
		d.set_primary_action(__("Create Purchase Order"), () => {
			frappe.call({
				method: "thameen_erp.overrides.procurement.make_purchase_order",
				args: { trip: frm.doc.name, supplier: frm.doc.custom_supplier, mode: "direct" },
				freeze: true,
				callback() {
					d.hide();
					frm.reload_doc();
				},
			});
		});
	}
	d.show();
}

function offer_procurement(frm, check) {
	const rows = check.shortfalls
		.map(
			(r) => `<tr>
				<td>${frappe.utils.escape_html(r.item_code)}</td>
				<td class="text-right">${format_number(r.planned_qty)}</td>
				<td class="text-right">${format_number(r.on_truck_free)}</td>
				<td class="text-right">${format_number(r.from_source)}</td>
				<td class="text-right"><b>${format_number(r.shortfall)}</b></td>
			</tr>`
		)
		.join("");

	const html = `
		<p>${__("The yard cannot fill this trip. Per item, in stock units:")}</p>
		<table class="table table-bordered small">
			<thead><tr>
				<th>${__("Item")}</th>
				<th class="text-right">${__("Planned")}</th>
				<th class="text-right">${__("On truck")}</th>
				<th class="text-right">${__("In warehouse")}</th>
				<th class="text-right">${__("Short")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
		<p class="text-muted small">${__(
			"Purchase Order: buy the shortfall into the loading warehouse; the trip loads from the yard once it is received. Direct Supply: the truck collects the whole trip at the supplier and delivers straight to site."
		)}</p>`;

	const dialog = new frappe.ui.Dialog({
		title: __("Insufficient Stock"),
		size: "large",
		fields: [
			{ fieldtype: "HTML", fieldname: "summary", options: html },
			{
				fieldname: "supplier",
				fieldtype: "Link",
				options: "Supplier",
				label: __("Supplier"),
				description: __("Used by both buttons below. Blank falls back to the Default Cement Supplier in the settings."),
			},
		],
		primary_action_label: __("Create Purchase Order for Shortfall"),
		primary_action(values) {
			frappe.call({
				method: "thameen_erp.overrides.procurement.make_purchase_order",
				args: { trip: frm.doc.name, supplier: values.supplier, mode: "shortfall" },
				freeze: true,
				callback({ message }) {
					dialog.hide();
					if (message) frappe.set_route("Form", "Purchase Order", message);
				},
			});
		},
		secondary_action_label: __("Switch to Direct Supply"),
		secondary_action() {
			const supplier = dialog.get_value("supplier");
			frappe.confirm(
				__("Change this trip to Direct from Supplier and raise a Purchase Order for the full trip? The cement will be received straight onto the truck at Loading."),
				() =>
					frappe.call({
						method: "thameen_erp.overrides.procurement.switch_to_direct_supply",
						args: { trip: frm.doc.name, supplier },
						freeze: true,
						callback() {
							dialog.hide();
							frm.reload_doc();
						},
					})
			);
		},
	});

	// A third, quieter option for sites where dispatch does not buy.
	dialog.$wrapper.find(".modal-footer").prepend(
		$(`<button class="btn btn-default btn-sm mr-auto">${__("Material Request instead")}</button>`).on("click", () => {
			frappe.call({
				method: "thameen_erp.overrides.procurement.make_material_request",
				args: { trip: frm.doc.name },
				freeze: true,
				callback({ message }) {
					dialog.hide();
					if (message) frappe.set_route("Form", "Material Request", message);
				},
			});
		})
	);

	dialog.show();
}

function add_procurement_buttons(frm) {
	if (frm.is_new()) return;

	frm.add_custom_button(__("Check Stock"), () => check_trip_stock(frm, { show: true }));

	if (frm.doc.custom_purchase_order) {
		frm.add_custom_button(
			frm.doc.custom_purchase_order,
			() => frappe.set_route("Form", "Purchase Order", frm.doc.custom_purchase_order),
			__("View")
		);
	}
	if (frm.doc.custom_purchase_receipt) {
		frm.add_custom_button(
			frm.doc.custom_purchase_receipt,
			() => frappe.set_route("Form", "Purchase Receipt", frm.doc.custom_purchase_receipt),
			__("View")
		);
	}

	if (frm.doc.docstatus !== 0) return;

	if (frm.doc.custom_supply_source === "Direct from Supplier" && !frm.doc.custom_purchase_order) {
		frm.add_custom_button(
			__("Purchase Order"),
			() => {
				if (!frm.doc.custom_supplier) {
					frappe.msgprint(__("Choose the Supplier first."));
					return;
				}
				if (frm.is_dirty()) {
					frappe.msgprint(__("Save the trip first."));
					return;
				}
				frappe.confirm(
					__("Raise a draft Purchase Order on {0} for every row on this trip?", [frm.doc.custom_supplier]),
					() =>
						frappe.call({
							method: "thameen_erp.overrides.procurement.make_purchase_order",
							args: { trip: frm.doc.name, supplier: frm.doc.custom_supplier, mode: "direct" },
							freeze: true,
							callback: () => frm.reload_doc(),
						})
				);
			},
			__("Create")
		).addClass("btn-primary");
	}
}


// ---------------------------------------------------------------------------
// Where is this load going? — decided or changed while the truck is on the road
// ---------------------------------------------------------------------------

function can_redirect(frm) {
	return (
		frm.doc.docstatus === 1 &&
		["Scheduled", "Loading", "In Transit"].includes(frm.doc.status) &&
		frm.doc.custom_supply_source === "Direct from Supplier"
	);
}

function add_destination_buttons(frm) {
	if (!can_redirect(frm)) return;
	const undecided = frm.doc.custom_destination_type === "Decide After Loading";
	frm.add_custom_button(__("Deliver to Customer…"), () => open_redirect_dialog(frm, "Customer"), __("Destination"));
	frm.add_custom_button(__("Deliver to Own Warehouse…"), () => open_redirect_dialog(frm, "Own Warehouse"), __("Destination"));
	if (undecided) frm.page.set_inner_btn_group_as_primary(__("Destination"));
}

function open_redirect_dialog(frm, destination) {
	const items = (frm.doc.custom_trip_items || []).map((r) => r.item_code);
	const fields =
		destination === "Customer"
			? [
					{
						fieldname: "customer", fieldtype: "Link", options: "Customer", label: __("Customer"), reqd: 1,
						onchange: () => {
							const d = cur_dialog;
							d.set_value("sales_order", null);
						},
					},
					{
						fieldname: "sales_order", fieldtype: "Link", options: "Sales Order", label: __("Sales Order"), reqd: 1,
						description: __("Submitted orders of this customer with {0} still to deliver.", [items.join(", ")]),
						get_query: () => ({
							filters: {
								docstatus: 1,
								customer: cur_dialog.get_value("customer") || undefined,
								status: ["not in", ["Closed", "Completed", "Cancelled"]],
								per_delivered: ["<", 100],
							},
						}),
						onchange: () => {
							const d = cur_dialog;
							const so = d.get_value("sales_order");
							if (!so) return;
							frappe.db.get_value("Sales Order", so, ["customer", "custom_delivery_location"]).then(({ message }) => {
								if (!message) return;
								if (message.customer && !d.get_value("customer")) d.set_value("customer", message.customer);
								if (message.custom_delivery_location) d.set_value("delivery_location", message.custom_delivery_location);
							});
						},
					},
					{ fieldname: "delivery_location", fieldtype: "Data", label: __("Delivery Site") },
					{ fieldname: "transportation_charge", fieldtype: "Currency", label: __("Freight for this trip"),
						default: frm.doc.custom_transportation_charge },
			  ]
			: [
					{
						fieldname: "target_warehouse", fieldtype: "Link", options: "Warehouse", label: __("Into Warehouse"), reqd: 1,
						default: frm.doc.custom_target_warehouse,
						get_query: () => ({ filters: { is_group: 0, company: frm.doc.company, custom_is_vehicle_warehouse: 0 } }),
					},
			  ];

	const dialog = new frappe.ui.Dialog({
		title: destination === "Customer" ? __("Deliver {0} to a Customer", [frm.doc.name]) : __("Deliver {0} to Own Warehouse", [frm.doc.name]),
		fields,
		primary_action_label: __("Set Destination"),
		primary_action(values) {
			frappe.call({
				method: "thameen_erp.overrides.po_trips.redirect_trip",
				args: {
					trip: frm.doc.name,
					destination,
					sales_order: values.sales_order,
					target_warehouse: values.target_warehouse,
					delivery_location: values.delivery_location,
					transportation_charge: values.transportation_charge,
				},
				freeze: true,
				freeze_message: __("Pointing the truck…"),
				callback() {
					dialog.hide();
					frm.reload_doc();
				},
			});
		},
	});
	dialog.show();
}


function ask_destination(frm) {
	const d = new frappe.ui.Dialog({
		title: __("Where is this load going?"),
		fields: [{
			fieldtype: "HTML",
			options: `<p>${__("The cement is on {0}. Choose the destination:", [frappe.utils.escape_html(frm.doc.vehicle || __("the truck"))])}</p>`,
		}],
		primary_action_label: __("A Customer…"),
		primary_action() { d.hide(); open_redirect_dialog(frm, "Customer"); },
		secondary_action_label: __("Own Warehouse…"),
		secondary_action() { d.hide(); open_redirect_dialog(frm, "Own Warehouse"); },
	});
	d.show();
}
