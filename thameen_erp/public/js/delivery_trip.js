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
		frm.set_query("vehicle", () => ({
    filters: {
        custom_status: ["in", ["Available", "Assigned"]],
    },
}));

		// Freight is a service charge — a stock item here is rejected server-side.
		frm.set_query("custom_transportation_item", () => ({
			filters: { is_stock_item: 0, disabled: 0 },
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0) {
			add_get_items_button(frm);
			add_load_buttons(frm);
			check_vehicle_load(frm, { prompt: false });
			return;
		}
		if (frm.doc.docstatus !== 1) return;

		add_status_buttons(frm);
		show_progress(frm);
		add_view_buttons(frm);
		check_vehicle_load(frm, { prompt: false });
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
				callback: () => frm.reload_doc(),
			});

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

	frm.dashboard.add_comment(
		`${frm.doc.vehicle}: ${parts.join(" · ")}` +
			(load.fits ? "" : ` — ${__("over by {0}", [format_number(load.overflow_qty)])}`),
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
		size: "large",
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
					assignments: JSON.stringify(collect_assignments(dialog)),
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

			const rows = message.loads
				.map((load, index) => {
					const items = load.items
						.map(
							(item) =>
								`${frappe.utils.escape_html(item.item_code)} — ${format_number(item.qty)} ${frappe.utils.escape_html(item.uom || "")}`
						)
						.join("<br>");
					const label =
						index === 0
							? `<b>${__("This trip")}</b><br><span class="text-muted small">${frm.doc.name}</span>`
							: `<b>${__("New trip {0}", [index + 1])}</b>`;
					const picker =
						index === 0
							? `<span class="text-muted small">${frappe.utils.escape_html(frm.doc.vehicle)}</span>`
							: `<input class="form-control input-xs split-vehicle" data-index="${index - 1}"
							     placeholder="${__("optional")}" style="min-width:120px">`;
					return `<tr>
						<td>${label}</td>
						<td>${items}</td>
						<td class="text-right">${format_number(load.total_qty)}</td>
						<td>${picker}</td>
					</tr>`;
				})
				.join("");

			wrapper.html(`
				<p>${__("{0} trip(s) in total. The follow-on trips are created as drafts.", [message.trip_count])}</p>
				<table class="table table-bordered">
					<thead><tr>
						<th style="width:22%">${__("Trip")}</th>
						<th>${__("Items")}</th>
						<th class="text-right" style="width:12%">${__("Qty")}</th>
						<th style="width:22%">${__("Vehicle")}</th>
					</tr></thead>
					<tbody>${rows}</tbody>
				</table>
				<p class="text-muted small">${__(
					"Leave a vehicle blank to let the dispatcher choose later. Delivery location, warehouse and freight settings are copied from this trip."
				)}</p>`);

			wrapper.find(".split-vehicle").each(function () {
				const $input = $(this);
				$input.autocomplete({
					minLength: 0,
					source(request, response) {
						frappe.call({
							method: "frappe.client.get_list",
							args: {
								doctype: "Vehicle",
								filters: { custom_status: ["in", ["Available", "Assigned"]] },
								fields: ["name", "custom_available_qty"],
								limit_page_length: 20,
							},
							callback({ message: vehicles }) {
								response(
									(vehicles || []).map((v) => ({
										label: `${v.name} (${__("free")} ${format_number(v.custom_available_qty)})`,
										value: v.name,
									}))
								);
							},
						});
					},
				});
			});
		},
	});
}

function collect_assignments(dialog) {
	const out = [];
	dialog.$wrapper.find(".split-vehicle").each(function () {
		out[parseInt($(this).data("index"), 10)] = $(this).val() || null;
	});
	return out;
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
