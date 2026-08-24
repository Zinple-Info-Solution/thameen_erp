const STAGES = ["Scheduled", "Loading", "In Transit", "Delivered", "POD Pending", "Completed"];

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
		// what is physically on the truck right now.
		frm.set_query("vehicle", () => ({
			query: "thameen_erp.overrides.vehicle_stock.vehicle_query",
			filters: { custom_status: ["in", ["Available", "Assigned"]] },
		}));

		frm.set_query("custom_purchase_order", () => ({
			filters: {
				docstatus: ["<", 2],
				supplier: frm.doc.custom_supplier || undefined,
				status: ["not in", ["Closed", "Cancelled"]],
			},
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
		if (frm.doc.docstatus === 0) {
			add_get_items_button(frm);
			add_load_buttons(frm);
			add_split_by_item_button(frm);
			add_procurement_buttons(frm);
			check_vehicle_load(frm, { prompt: false });
			check_trip_stock(frm, { prompt: false });
			return;
		}
		if (frm.doc.docstatus !== 1) return;

		add_status_buttons(frm);
		show_progress(frm);
		add_view_buttons(frm);
		add_procurement_buttons(frm);
		check_vehicle_load(frm, { prompt: false });
		check_trip_stock(frm, { prompt: false });
	},

	after_save(frm) {
		auto_split_by_item(frm);
	},

	custom_destination_type(frm) {
		if (frm.doc.custom_destination_type === "Own Warehouse" && frm.doc.custom_supply_source !== "Direct from Supplier") {
			frm.set_value("custom_supply_source", "Direct from Supplier");
		}
	},

	custom_supply_source(frm) {
		if (frm.doc.custom_supply_source !== "Direct from Supplier") return;
		if (frm.doc.custom_supplier) return;
		frappe.db
			.get_single_value("Thameen Fleet Settings", "default_cement_supplier")
			.then((supplier) => {
				if (supplier) frm.set_value("custom_supplier", supplier);
			});
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
				// The full load check replaces the old capacity-only comment:
				// it also accounts for what other open trips already hold.
				check_vehicle_load(frm, { prompt: true });
				// And: is the cement actually there — on the truck or in the yard?
				if (!frm.is_new()) check_trip_stock(frm, { prompt: true });
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

function total_planned(frm) {
	return (frm.doc.custom_trip_items || []).reduce((sum, row) => sum + flt(row.qty), 0);
}

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
					frm.dashboard.add_comment(
						__("{0} has no Capacity set, so the load cannot be checked.", [frm.doc.vehicle]),
						"orange",
						true
					);
				}
				return;
			}

			render_load_indicator(frm, message);

			if (!message.fits && opts.prompt && frm.doc.docstatus === 0) {
				offer_split(frm, message);
			}
		},
	});
}

function render_load_indicator(frm, load) {
	const colour = load.fits ? "green" : "red";
	const parts = [
		__("Capacity {0}", [format_number(load.capacity)]),
		__("committed {0}", [format_number(load.committed_qty)]),
		__("available {0}", [format_number(load.available_qty)]),
		__("this trip {0}", [format_number(load.planned_qty)]),
	];

	const on_truck = (load.on_truck_items || []).length
		? (load.on_truck_items || [])
				.map(
					(item) =>
						`${frappe.utils.escape_html(item.item_code)} ${format_number(item.qty)}` +
						(item.on_loaded_trips
							? ` <span class="text-muted">(${__("{0} on loaded trips", [format_number(item.on_loaded_trips)])})</span>`
							: "")
				)
				.join(", ")
		: __("empty");

	frm.dashboard.add_comment(
		`${frm.doc.vehicle}: ${parts.join(" · ")}` +
			(load.fits ? "" : ` — ${__("over by {0}", [format_number(load.overflow_qty)])}`) +
			`<br><span class="small">${__("On truck now")}: ${on_truck}` +
			(load.vehicle_warehouse
				? ` · ${frappe.utils.get_form_link("Warehouse", load.vehicle_warehouse, true, __("warehouse"))}`
				: "") +
			`</span>`,
		colour,
		true
	);
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
		? `<p class="text-muted small">${__("Already committed to this truck:")}</p>
		   <table class="table table-bordered small">
		     <thead><tr><th>${__("Trip")}</th><th>${__("Status")}</th><th class="text-right">${__("Qty")}</th></tr></thead>
		     <tbody>${committed_rows}</tbody>
		   </table>`
		: "";

	const summary = `
		<p>${__("{0} cannot carry this trip in one go.", [frm.doc.vehicle])}</p>
		<table class="table table-bordered">
			<tbody>
				<tr><td>${__("Rated capacity")}</td><td class="text-right">${format_number(load.capacity)}</td></tr>
				<tr><td>${__("Already committed")}</td><td class="text-right">${format_number(load.committed_qty)}</td></tr>
				<tr><td><b>${__("Free right now")}</b></td><td class="text-right"><b>${format_number(load.available_qty)}</b></td></tr>
				<tr><td>${__("This trip needs")}</td><td class="text-right">${format_number(load.planned_qty)}</td></tr>
				<tr><td><b>${__("Over by")}</b></td><td class="text-right"><b>${format_number(load.overflow_qty)}</b></td></tr>
			</tbody>
		</table>
		${why}`;

	const dialog = new frappe.ui.Dialog({
		title: __("Not Enough Room"),
		size: "extra-large",
		fields: [
			{ fieldtype: "HTML", fieldname: "summary", options: summary },
			{
				fieldname: "use_capacity",
				fieldtype: "Check",
				label: __("Ignore what is committed and plan against full capacity"),
				description: __(
					"Tick this if the other trips will be finished before this one loads. The first trip then takes a full truckload instead of only the free space."
				),
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
		secondary_action_label: __("Keep as One Trip"),
		secondary_action() {
			dialog.hide();
			frappe.show_alert({
				message: __("Left as one trip. It will still submit — the overload is a warning, not a block."),
				indicator: "orange",
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
			dialog.totals = {};
			dialog.plan.forEach((l) => l.items.forEach((i) => (dialog.totals[i.item_code] = flt(dialog.totals[i.item_code]) + i.qty)));
			render_plan(frm, dialog);
		},
	});
}

function vehicle_select_html(dialog, value, cls, index) {
	const opts = [`<option value="">${__("— choose later —")}</option>`]
		.concat(
			(dialog.vehicles || []).map(
				(v) =>
					`<option value="${frappe.utils.escape_html(v.name)}" ${v.name === value ? "selected" : ""}>` +
					`${frappe.utils.escape_html(v.name)} · ${__("cap")} ${format_number(v.capacity)} · ${__("free")} ${format_number(v.available)}</option>`
			)
		)
		.join("");
	return `<select class="form-control input-xs ${cls}" data-index="${index}">${opts}</select>`;
}

function render_plan(frm, dialog) {
	const wrapper = dialog.fields_dict.plan.$wrapper;
	const plan = dialog.plan;
	const cap_of = (name) => {
		const v = (dialog.vehicles || []).find((x) => x.name === name);
		return v ? flt(v.capacity) : 0;
	};

	const rows = plan
		.map((load, index) => {
			const total = load.items.reduce((a, i) => a + flt(i.qty), 0);
			const cap = cap_of(load.vehicle);
			const over = cap && total > cap + 0.001;
			const items = load.items
				.map(
					(i, k) =>
						`<div class="d-flex align-items-center mb-1">
							<span style="min-width:90px">${frappe.utils.escape_html(i.item_code)}</span>
							<input type="number" step="any" min="0" class="form-control input-xs plan-qty" style="width:110px"
								data-index="${index}" data-item="${k}" value="${i.qty}">
							<span class="text-muted small ml-1">${frappe.utils.escape_html(i.uom || "")}</span>
						</div>`
				)
				.join("");
			const label =
				index === 0
					? `<b>${__("This trip")}</b><br><span class="text-muted small">${frm.doc.name}</span>`
					: `<b>${__("New trip {0}", [index + 1])}</b>` +
					  (plan.length > 2 ? `<br><a class="small text-danger plan-remove" data-index="${index}">${__("remove")}</a>` : "");
			return `<tr class="${over ? "table-warning" : ""}">
				<td>${label}</td>
				<td>${items}</td>
				<td class="text-right"><b>${format_number(total)}</b>${over ? `<br><span class="text-danger small">${__("over {0}", [format_number(cap)])}</span>` : ""}</td>
				<td>${vehicle_select_html(dialog, load.vehicle, "plan-vehicle", index)}</td>
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
	const balanced = balance.every((b) => Math.abs(b.diff) < 0.001);
	const balance_html = balance
		.map(
			(b) =>
				`<span class="${Math.abs(b.diff) < 0.001 ? "text-success" : "text-danger"} mr-3">` +
				`${frappe.utils.escape_html(b.code)}: ${format_number(placed[b.code])} / ${format_number(dialog.totals[b.code])}` +
				(Math.abs(b.diff) < 0.001 ? " ✓" : ` (${b.diff > 0 ? "+" : ""}${format_number(b.diff)})`) +
				`</span>`
		)
		.join("");

	wrapper.html(`
		<div class="d-flex justify-content-between align-items-center mb-2">
			<span>${__("{0} trip(s). Edit any quantity, truck or date — the last trip rebalances to keep the total.", [plan.length])}</span>
			<span>
				<button class="btn btn-xs btn-default plan-add">${__("+ Add trip")}</button>
				<button class="btn btn-xs btn-default plan-same-truck ml-1">${__("Same truck, one day apart")}</button>
			</span>
		</div>
		<table class="table table-bordered small">
			<thead><tr>
				<th style="width:16%">${__("Trip")}</th>
				<th>${__("Items & qty")}</th>
				<th class="text-right" style="width:10%">${__("Total")}</th>
				<th style="width:28%">${__("Vehicle")}</th>
				<th style="width:14%">${__("Departure")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
		<div class="small mb-2">${__("Placed")}: ${balance_html}</div>
		<p class="text-muted small">${__("Site, loading warehouse, freight item, supplier and order links are copied to every new trip. Over-capacity trips are allowed but flagged.")}</p>`);

	dialog.get_primary_btn().prop("disabled", !balanced);

	wrapper.find(".plan-qty").on("change", function () {
		const index = parseInt($(this).data("index"), 10);
		const k = parseInt($(this).data("item"), 10);
		const item = plan[index].items[k];
		const new_qty = Math.max(flt($(this).val()), 0);
		const diff = new_qty - flt(item.qty);
		item.qty = new_qty;
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
		plan[parseInt($(this).data("index"), 10)].vehicle = $(this).val() || null;
		render_plan(frm, dialog);
	});
	wrapper.find(".plan-date").on("change", function () {
		plan[parseInt($(this).data("index"), 10)].departure_time = $(this).val();
	});
	wrapper.find(".plan-remove").on("click", function () {
		const index = parseInt($(this).data("index"), 10);
		// Hand its quantities to the previous trip so nothing is lost.
		plan[index].items.forEach((i) => {
			const target = plan[index - 1].items.find((x) => x.item_code === i.item_code) || plan[0].items.find((x) => x.item_code === i.item_code);
			if (target) target.qty = flt(target.qty) + flt(i.qty);
		});
		plan.splice(index, 1);
		render_plan(frm, dialog);
	});
	wrapper.find(".plan-add").on("click", () => {
		const last = plan[plan.length - 1];
		plan.push({
			vehicle: null,
			departure_time: frappe.datetime.add_days(last.departure_time || frappe.datetime.get_today(), 1),
			items: last.items.map((i) => ({ item_code: i.item_code, uom: i.uom, qty: 0 })),
		});
		render_plan(frm, dialog);
	});
	wrapper.find(".plan-same-truck").on("click", () => {
		const base = plan[0].departure_time || frappe.datetime.get_today();
		plan.forEach((l, i) => {
			l.vehicle = frm.doc.vehicle;
			l.departure_time = frappe.datetime.add_days(base, i);
		});
		render_plan(frm, dialog);
	});
}

function collect_plan(dialog) {
	return (dialog.plan || []).map((l) => ({
		vehicle: l.vehicle || null,
		departure_time: l.departure_time || null,
		items: l.items.filter((i) => flt(i.qty) > 0).map((i) => ({ item_code: i.item_code, qty: flt(i.qty) })),
	})).filter((l) => l.items.length);
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

function check_trip_stock(frm, opts) {
	opts = opts || {};
	if (frm.is_new() || frm.doc.docstatus > 1) return;
	if (!(frm.doc.custom_trip_items || []).length) return;
	if (frm.doc.docstatus === 1 && !["Scheduled"].includes(frm.doc.status)) return;

	frappe.call({
		method: "thameen_erp.overrides.procurement.check_trip_stock",
		args: { trip: frm.doc.name, vehicle: frm.doc.vehicle },
		callback({ message }) {
			if (!message) return;
			render_stock_indicator(frm, message);
			if (!message.sufficient && opts.prompt && frm.doc.docstatus === 0) {
				if (message.supply_source === "Direct from Supplier") return;
				offer_procurement(frm, message);
			}
		},
	});
}

function render_stock_indicator(frm, check) {
	if (check.supply_source === "Direct from Supplier") {
		const po = check.purchase_order;
		let text, colour;
		if (!po) {
			text = __("Direct from supplier — no Purchase Order yet. Use Create > Purchase Order.");
			colour = "red";
		} else if (check.po_docstatus === 0) {
			text = __("Direct from supplier — {0} is still a draft. The buyer must submit it before loading.", [
				frappe.utils.get_form_link("Purchase Order", po, true),
			]);
			colour = "orange";
		} else if (!check.sufficient) {
			text = __("Direct from supplier — {0} is {1}.", [frappe.utils.get_form_link("Purchase Order", po, true), check.po_status]);
			colour = "red";
		} else {
			text = __("Direct from supplier — {0} submitted. Loading will receive the cement straight onto the truck.", [
				frappe.utils.get_form_link("Purchase Order", po, true),
			]);
			colour = "green";
		}
		frm.dashboard.add_comment(text, colour, true);
		return;
	}

	const rows = (check.rows || [])
		.map((r) => {
			const bits = [];
			if (r.on_truck_free) bits.push(__("{0} on truck", [format_number(r.on_truck_free)]));
			if (r.from_source) bits.push(__("{0} in {1}", [format_number(r.from_source), frappe.utils.escape_html(r.source_warehouse || "")]));
			if (r.shortfall) bits.push(`<b>${__("short {0}", [format_number(r.shortfall)])}</b>`);
			return `${frappe.utils.escape_html(r.item_code)} ${format_number(r.planned_qty)}: ${bits.join(", ") || __("nothing available")}`;
		})
		.join("<br>");

	frm.dashboard.add_comment(
		(check.sufficient ? __("Stock check — covered.") : __("Stock check — NOT covered.")) + `<br><span class="small">${rows}</span>`,
		check.sufficient ? "green" : "red",
		true
	);
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

	frm.add_custom_button(__("Check Stock"), () => check_trip_stock(frm, { prompt: true }));

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
