frappe.ui.form.on("Vehicle", {
	refresh(frm) {
		if (frm.is_new()) return;

		if (frm.doc.custom_cost_center) {
			frm.add_custom_button(__("Cost Center"), () =>
				frappe.set_route("Form", "Cost Center", frm.doc.custom_cost_center), __("View"));
		}
		if (frm.doc.custom_vehicle_warehouse) {
			// Live Bin figures, not a report the filters might not reach. The
			// Stock Balance report needs company and a date range as well as a
			// warehouse; handing it a bare warehouse showed an empty report.
			frm.add_custom_button(__("Truck Stock"), () => show_truck_stock(frm), __("View"));
			frm.add_custom_button(__("Stock Movements"), () =>
				frappe.set_route("List", "Stock Entry", { custom_vehicle: frm.doc.name }), __("View"));
		}
		frm.add_custom_button(__("Trips"), () =>
			frappe.set_route("List", "Delivery Trip", { vehicle: frm.doc.name }), __("View"));
		frm.add_custom_button(__("Documents"), () =>
			frappe.set_route("List", "Vehicle Document", { vehicle: frm.doc.name }), __("View"));
		frm.add_custom_button(__("Profitability"), () =>
			frappe.set_route("query-report", "Vehicle Profitability",
				{ vehicle: frm.doc.name }), __("View"));

		frm.add_custom_button(__("Add Document"), () => {
			frappe.new_doc("Vehicle Document", { vehicle: frm.doc.name });
		}, __("Create"));
		frm.add_custom_button(__("Log Fuel / Service"), () => {
			frappe.new_doc("Vehicle Log", { license_plate: frm.doc.name });
		}, __("Create"));

		// Manual stock movement. Loading is the everyday case (pre-load a truck
		// before the order lands); unloading brings short-delivered cement back.
		if (frm.doc.custom_vehicle_warehouse) {
			frm.add_custom_button(__("Load Stock"), () => open_load_dialog(frm, "load"), __("Stock"));
			frm.add_custom_button(__("Unload Stock"), () => open_load_dialog(frm, "unload"), __("Stock"));
			frm.page.set_inner_btn_group_as_primary(__("Stock"));
		}

		// The stored Committed / On Truck / Available figures are a cache kept
		// warm by hooks. This is the repair button for when one did not fire.
		frm.add_custom_button(__("Recalculate Load"), () => {
			frappe.call({
				method: "thameen_erp.overrides.vehicle_load.recalculate_vehicle_load",
				args: { vehicle: frm.doc.name },
				freeze: true,
				freeze_message: __("Recounting…"),
				callback: () => frm.reload_doc(),
			});
		}, __("Stock"));

		render_status_banner(frm);
	},

	custom_assigned_driver(frm) {
		if (!frm.doc.custom_assigned_driver) return;
		frappe.db.get_value("Driver", frm.doc.custom_assigned_driver, "employee")
			.then(({ message }) => {
				if (message && message.employee) frm.set_value("employee", message.employee);
			});
	},
});

function render_status_banner(frm) {
	frappe.db.get_list("Vehicle Document", {
		filters: { vehicle: frm.doc.name, status: ["in", ["Expired", "Expiring Soon"]] },
		fields: ["name", "document_type", "expiry_date", "status"],
		limit: 10,
	}).then((rows) => {
		if (!rows.length) return;
		const lines = rows.map((r) =>
			`<li>${frappe.utils.escape_html(r.document_type)} — ${r.status} (${frappe.datetime.str_to_user(r.expiry_date)})</li>`
		).join("");
		frm.dashboard.add_comment(
			`<b>${__("Document attention required")}</b><ul>${lines}</ul>`,
			rows.some((r) => r.status === "Expired") ? "red" : "orange",
			true
		);
	});
}

// ---------------------------------------------------------------------------
// Stock on the truck
//
// Three numbers live on this form and they mean different things:
//   Capacity       rated     — what the truck can carry when empty
//   Committed Qty  promised  — planned qty on submitted open trips
//   On Truck       physical  — actual stock in the vehicle warehouse
// On Truck is read from stock, never stored here.
// ---------------------------------------------------------------------------

// A plain reading of what is in the vehicle warehouse right now, with the
// three numbers that are easy to confuse set side by side.
function show_truck_stock(frm) {
	frappe.call({
		method: "thameen_erp.overrides.vehicle_stock.get_truck_stock_summary",
		args: { vehicle: frm.doc.name },
		freeze: true,
		callback({ message }) {
			if (!message) return;

			const rows = (message.items || [])
				.map(
					(i) => `<tr>
						<td>${frappe.utils.escape_html(i.item_code)}</td>
						<td>${frappe.utils.escape_html(i.item_name || "")}</td>
						<td class="text-right">${format_number(i.qty)}</td>
						<td class="text-right">${format_number(i.on_loaded_trips)}</td>
						<td class="text-right"><b>${format_number(i.free)}</b></td>
						<td>${frappe.utils.escape_html(i.stock_uom || "")}</td>
					</tr>`
				)
				.join("");

			const table = rows
				? `<table class="table table-bordered small">
						<thead><tr>
							<th>${__("Item")}</th><th>${__("Name")}</th>
							<th class="text-right">${__("On truck")}</th>
							<th class="text-right">${__("On loaded trips")}</th>
							<th class="text-right">${__("Free")}</th>
							<th>${__("UOM")}</th>
						</tr></thead>
						<tbody>${rows}</tbody>
					</table>`
				: `<p class="text-muted">${__("The truck is empty.")}</p>`;

			const summary = `<table class="table table-bordered small">
				<tbody>
					<tr><td>${__("Rated capacity")}</td><td class="text-right">${format_number(message.capacity)} ${frappe.utils.escape_html(message.capacity_uom || "")}</td></tr>
					<tr><td>${__("Committed to open trips")}</td><td class="text-right">${format_number(message.committed_qty)}</td></tr>
					<tr><td>${__("Physically on truck")}</td><td class="text-right">${format_number(message.on_truck_qty)}</td></tr>
					<tr><td><b>${__("Physical space left")}</b></td><td class="text-right"><b>${message.physical_space === null ? "—" : format_number(message.physical_space)}</b></td></tr>
				</tbody>
			</table>`;

			const d = new frappe.ui.Dialog({
				title: __("Stock on {0}", [frm.doc.name]),
				size: "large",
				fields: [{ fieldtype: "HTML", options: summary + table }],
				primary_action_label: __("Open Stock Balance"),
				primary_action() {
					d.hide();
					frappe.set_route("query-report", "Stock Balance", {
						company: frm.doc.custom_company || frappe.defaults.get_user_default("Company"),
						from_date: frappe.datetime.add_months(frappe.datetime.get_today(), -12),
						to_date: frappe.datetime.get_today(),
						warehouse: frm.doc.custom_vehicle_warehouse,
					});
				},
			});
			d.show();
		},
	});
}

function open_load_dialog(frm, direction) {
	const loading = direction === "load";

	const dialog = new frappe.ui.Dialog({
		title: loading ? __("Load Stock onto {0}", [frm.doc.name]) : __("Unload Stock from {0}", [frm.doc.name]),
		size: "large",
		fields: [
			{
				fieldname: "warehouse",
				fieldtype: "Link",
				options: "Warehouse",
				label: loading ? __("From Warehouse") : __("To Warehouse"),
				reqd: 1,
				get_query: () => ({
					filters: { is_group: 0, company: frm.doc.custom_company, custom_is_vehicle_warehouse: 0 },
				}),
				onchange: () => refresh_preview(frm, dialog, direction),
			},
			{ fieldtype: "Column Break" },
			{ fieldtype: "HTML", fieldname: "truck_now" },
			{ fieldtype: "Section Break" },
			{
				fieldname: "items",
				fieldtype: "Table",
				label: __("Items"),
				cannot_add_rows: false,
				in_place_edit: true,
				reqd: 1,
				data: [],
				fields: [
					{
						fieldname: "item_code",
						fieldtype: "Link",
						options: "Item",
						label: __("Item"),
						in_list_view: 1,
						reqd: 1,
						columns: 4,
						get_query: () => ({ filters: { is_stock_item: 1, disabled: 0 } }),
						onchange() {
							const row = this.doc;
							if (!row.item_code) return;
							frappe.db.get_value("Item", row.item_code, "stock_uom").then(({ message }) => {
								if (message && !row.uom) {
									row.uom = message.stock_uom;
									dialog.fields_dict.items.grid.refresh();
								}
							});
							if (loading) suggest_warehouse(frm, dialog, row.item_code);
						},
					},
					{ fieldname: "qty", fieldtype: "Float", label: __("Qty"), in_list_view: 1, reqd: 1, columns: 2,
						onchange: () => refresh_preview(frm, dialog, direction) },
					{ fieldname: "uom", fieldtype: "Link", options: "UOM", label: __("UOM"), in_list_view: 1, columns: 2 },
				],
			},
			{ fieldtype: "Section Break" },
			{ fieldtype: "HTML", fieldname: "preview" },
			{ fieldtype: "Small Text", fieldname: "remarks", label: __("Remarks") },
		],
		primary_action_label: loading ? __("Load") : __("Unload"),
		primary_action(values) {
			submit_manual_load(frm, dialog, direction, values, 0);
		},
	});

	// Pre-fill the table with what is on the truck for an unload, so the
	// usual "bring everything back" is two clicks.
	frappe.call({
		method: "thameen_erp.overrides.vehicle_stock.get_truck_stock_summary",
		args: { vehicle: frm.doc.name },
		callback({ message }) {
			const items = (message && message.items) || [];
			const now = items.length
				? items.map((i) => `${frappe.utils.escape_html(i.item_code)} ${format_number(i.qty)} ${frappe.utils.escape_html(i.stock_uom || "")}`).join("<br>")
				: __("empty");
			dialog.fields_dict.truck_now.$wrapper.html(
				`<div class="small text-muted">${__("On truck now")}</div><div>${now}</div>` +
				(message && message.capacity ? `<div class="small text-muted">${__("Capacity {0}", [format_number(message.capacity)])}</div>` : "")
			);
			if (!loading && items.length) {
				dialog.fields_dict.items.df.data = items.map((i) => ({ item_code: i.item_code, qty: i.qty, uom: i.stock_uom }));
				dialog.fields_dict.items.grid.refresh();
				refresh_preview(frm, dialog, direction);
			}
		},
	});

	dialog.show();
}

// Pick the yard holding the most of this item, so the dispatcher does not have
// to hunt for it. Only fills an empty field — a warehouse chosen by hand stays.
function suggest_warehouse(frm, dialog, item_code) {
	frappe.call({
		method: "thameen_erp.overrides.vehicle_stock.find_stock_warehouse",
		args: {
			item_code,
			company: frm.doc.custom_company,
			exclude: frm.doc.custom_vehicle_warehouse,
		},
		callback({ message }) {
			const hint = dialog.fields_dict.warehouse.$wrapper.find(".thameen-wh-hint").remove();
			if (!message || !message.warehouse) {
				dialog.fields_dict.warehouse.$wrapper.append(
					`<div class="thameen-wh-hint text-danger small mt-1">${__(
						"{0} is not in any warehouse. Receive it first, or use Order Shortfall below.",
						[frappe.utils.escape_html(item_code)]
					)}</div>`
				);
				return;
			}
			if (!dialog.get_value("warehouse")) {
				dialog.set_value("warehouse", message.warehouse);
			}
			const others = (message.others || [])
				.map((o) => `${frappe.utils.escape_html(o.warehouse)} ${format_number(o.qty)}`)
				.join(" · ");
			dialog.fields_dict.warehouse.$wrapper.append(
				`<div class="thameen-wh-hint text-muted small mt-1">${frappe.utils.escape_html(
					message.warehouse
				)} ${__("has")} ${format_number(message.qty)}${others ? " · " + others : ""}</div>`
			);
		},
	});
}

function collect_items(dialog) {
	return (dialog.get_value("items") || [])
		.filter((r) => r.item_code && flt(r.qty) > 0)
		.map((r) => ({ item_code: r.item_code, qty: flt(r.qty), uom: r.uom }));
}

function refresh_preview(frm, dialog, direction) {
	const warehouse = dialog.get_value("warehouse");
	const items = collect_items(dialog);
	const wrapper = dialog.fields_dict.preview.$wrapper;
	if (!warehouse || !items.length) {
		wrapper.html("");
		return;
	}
	frappe.call({
		method: "thameen_erp.overrides.vehicle_stock.preview_manual_load",
		args: { vehicle: frm.doc.name, direction, warehouse, items: JSON.stringify(items) },
		callback({ message }) {
			if (!message) return;
			render_preview(wrapper, message, direction, frm.doc.name);
		},
		error: () => wrapper.html(""),
	});
}

function render_preview(wrapper, p, direction, vehicle) {
	const rows = p.rows
		.map((r) => {
			const short = r.shortfall > 0;
			return `<tr class="${short ? "text-danger" : ""}">
				<td>${frappe.utils.escape_html(r.item_code)}</td>
				<td class="text-right">${format_number(r.stock_qty)}</td>
				<td class="text-right">${format_number(r.source_qty)}</td>
				<td class="text-right">${short ? `<b>${format_number(r.shortfall)}</b>` : "—"}</td>
			</tr>`;
		})
		.join("");

	let verdict = "";
	if (p.insufficient) {
		verdict = `<div class="alert alert-danger small">${__("Not enough stock in {0}. The transfer will be refused.", [
			frappe.utils.escape_html(p.rows[0].source_warehouse),
		])}</div>`;
	} else if (p.over_capacity) {
		verdict = `<div class="alert alert-warning small">${__("After loading, {0} will hold {1} — {2} above its rated capacity of {3}. You will be asked to confirm.", [
			frappe.utils.escape_html(vehicle),
			format_number(p.on_truck_after),
			format_number(p.over_by),
			format_number(p.capacity),
		])}</div>`;
	} else {
		verdict = `<div class="alert alert-success small">${__("After this, the truck will hold {0}{1}.", [
			format_number(p.on_truck_after),
			p.capacity ? __(" of {0}", [format_number(p.capacity)]) : "",
		])}</div>`;
	}

	wrapper.html(`
		<table class="table table-bordered small">
			<thead><tr>
				<th>${__("Item")}</th>
				<th class="text-right">${direction === "load" ? __("Loading") : __("Unloading")}</th>
				<th class="text-right">${__("In source")}</th>
				<th class="text-right">${__("Short")}</th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
		${verdict}`);
}

function submit_manual_load(frm, dialog, direction, values, allow_over_capacity) {
	const items = collect_items(dialog);
	if (!items.length) {
		frappe.msgprint(__("Add at least one item with a quantity."));
		return;
	}

	frappe.call({
		method: "thameen_erp.overrides.vehicle_stock.preview_manual_load",
		args: { vehicle: frm.doc.name, direction, warehouse: values.warehouse, items: JSON.stringify(items) },
		freeze: true,
		callback({ message: p }) {
			if (!p) return;

			if (p.insufficient) {
				offer_purchase_for_shortfall(frm, dialog, p, values.warehouse);
				return;
			}

			const go = () =>
				frappe.call({
					method: "thameen_erp.overrides.vehicle_stock.manual_load",
					args: {
						vehicle: frm.doc.name,
						direction,
						warehouse: values.warehouse,
						items: JSON.stringify(items),
						allow_over_capacity: allow_over_capacity ? 1 : 0,
						remarks: values.remarks,
					},
					freeze: true,
					freeze_message: direction === "load" ? __("Loading…") : __("Unloading…"),
					callback() {
						dialog.hide();
						frm.reload_doc();
					},
				});

			if (p.over_capacity && !allow_over_capacity) {
				frappe.confirm(
					__("{0} will hold {1} after loading — {2} above its rated capacity of {3}. Load anyway?", [
						frm.doc.name, format_number(p.on_truck_after), format_number(p.over_by), format_number(p.capacity),
					]),
					() => submit_manual_load(frm, dialog, direction, values, 1)
				);
				return;
			}
			go();
		},
	});
}

function offer_purchase_for_shortfall(frm, dialog, p, warehouse) {
	const lines = p.shortfalls
		.map((s) => `<li>${frappe.utils.escape_html(s.item_code)}: ${__("need {0} more (only {1} in {2})", [
			format_number(s.short), format_number(s.available), frappe.utils.escape_html(warehouse),
		])}</li>`)
		.join("");

	const d = new frappe.ui.Dialog({
		title: __("Insufficient Stock"),
		fields: [
			{ fieldtype: "HTML", options: `<p>${__("The warehouse cannot cover this load:")}</p><ul>${lines}</ul>` },
			{ fieldname: "supplier", fieldtype: "Link", options: "Supplier", label: __("Supplier"),
				description: __("Blank falls back to the Default Cement Supplier in Thameen Fleet Settings.") },
		],
		primary_action_label: __("Create Purchase Order"),
		primary_action(v) {
			frappe.call({
				method: "thameen_erp.overrides.vehicle_stock.make_purchase_order_for_shortfall",
				args: { vehicle: frm.doc.name, warehouse, shortfalls: JSON.stringify(p.shortfalls), supplier: v.supplier },
				freeze: true,
				callback({ message }) {
					d.hide();
					dialog.hide();
					if (message) frappe.set_route("Form", "Purchase Order", message);
				},
			});
		},
		secondary_action_label: __("Back"),
		secondary_action: () => d.hide(),
	});
	frappe.db.get_single_value("Thameen Fleet Settings", "default_cement_supplier").then((s) => {
		if (s) d.set_value("supplier", s);
	});
	d.show();
}
