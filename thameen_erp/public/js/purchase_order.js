// Purchase Order — either a view link to whatever Delivery Trip is already
// attached to it (a Direct-from-Supplier trip set up by hand on the trip's
// own Trip Route field points its Purchase Order button back here), or a
// button to send one or more empty trucks to collect it in person.
//
// Planning a trip off cement bought here otherwise happens later, once the
// goods are actually received — see purchase_receipt.js. Receiving first
// keeps stock and accounts accurate the moment the supplier delivers,
// instead of only when a truck eventually gets around to loading it.

frappe.ui.form.on("Purchase Order", {
	refresh(frm) {
		if (frm.doc.custom_delivery_trip) {
			frm.add_custom_button(frm.doc.custom_delivery_trip,
				() => frappe.set_route("Form", "Delivery Trip", frm.doc.custom_delivery_trip), __("View"));
		}
		// Offered either way — a PO already linked to a trip (the older
		// trip-raises-its-own-PO flow above) can still get its own,
		// separate pickup trip(s). create_pickup_trips warns rather than
		// silently overwriting an existing link.
		if (frm.doc.docstatus === 1) {
			frm.add_custom_button(__("Send Vehicle to Collect"), () => open_pickup_trip_dialog(frm))
				.addClass("btn-primary");
		}
	},
});

const PK_TOL = 0.001;

// One row per truck — a PO too big for one truck's capacity is exactly why
// this is a table, not a single Vehicle field. Each row becomes its own
// pickup trip; nothing is loaded onto any of them until each one's own
// Purchase Receipt is submitted.
function open_pickup_trip_dialog(frm) {
	frappe.call({
		method: "thameen_erp.overrides.procurement.pending_pickup_qty",
		args: { purchase_order: frm.doc.name },
		freeze: true,
		callback({ message: pending }) {
			if (!pending || !pending.total_pending) {
				frappe.msgprint(__("Nothing left on this PO for a pickup trip to claim — it may already be fully covered by other pickup trips."));
				return;
			}
			if (!pending.vehicles || !pending.vehicles.length) {
				frappe.msgprint(__("No empty vehicle is available right now."));
				return;
			}
			render_pickup_dialog(frm, pending, pending.vehicles, pending.drivers || []);
		},
	});
}

function render_pickup_dialog(frm, pending, vehicle_opts, driver_opts) {
	const plan = [{ vehicle: null, qty: pending.total_pending, driver: null, departure_time: frappe.datetime.now_datetime() }];

	const dialog = new frappe.ui.Dialog({
		title: __("Send Vehicle to Collect {0}", [frm.doc.name]),
		size: "large",
		fields: [{ fieldtype: "HTML", fieldname: "plan" }],
		primary_action_label: __("Create Trip(s)"),
		primary_action() {
			const rows = plan
				.filter((r) => r.vehicle && flt(r.qty) > 0)
				.map((r) => ({ vehicle: r.vehicle, qty: flt(r.qty), driver: r.driver, departure_time: r.departure_time }));
			if (!rows.length) {
				frappe.msgprint(__("Add at least one vehicle with a quantity."));
				return;
			}
			frappe.call({
				method: "thameen_erp.overrides.procurement.create_pickup_trips",
				args: { purchase_order: frm.doc.name, plan: JSON.stringify(rows) },
				freeze: true,
				callback({ message: created }) {
					dialog.hide();
					if (created && created.length === 1) frappe.set_route("Form", "Delivery Trip", created[0]);
					else if (created && created.length) frm.reload_doc();
				},
			});
		},
	});
	dialog.show();
	draw_pickup_plan(dialog, plan, pending, vehicle_opts, driver_opts);
}

function draw_pickup_plan(dialog, plan, pending, vehicle_opts, driver_opts) {
	const wrapper = dialog.fields_dict.plan.$wrapper;

	const taken_vehicles = (except_i) => new Set(plan.map((r, i) => (i === except_i ? null : r.vehicle)).filter(Boolean));
	const taken_drivers = (except_i) => new Set(plan.map((r, i) => (i === except_i ? null : r.driver)).filter(Boolean));

	const vehicle_select = (value, i) => {
		const taken = taken_vehicles(i);
		return [`<option value="">${__("— choose —")}</option>`]
			.concat(
				vehicle_opts
					.filter((v) => v.name === value || !taken.has(v.name))
					.map((v) => {
						const label = v.capacity ? `${v.name} (${format_number(v.capacity)})` : v.name;
						return `<option value="${frappe.utils.escape_html(v.name)}" ${v.name === value ? "selected" : ""}>${frappe.utils.escape_html(label)}</option>`;
					})
			)
			.join("");
	};

	const driver_select = (value, i) => {
		const taken = taken_drivers(i);
		return [`<option value="">${__("— choose later —")}</option>`]
			.concat(
				driver_opts
					.filter((d) => d.name === value || !taken.has(d.name))
					.map(
						(d) =>
							`<option value="${frappe.utils.escape_html(d.name)}" ${d.name === value ? "selected" : ""}>${frappe.utils.escape_html(d.full_name || d.name)}</option>`
					)
			)
			.join("");
	};

	const total = plan.reduce((sum, r) => sum + flt(r.qty), 0);
	const diff = total - pending.total_pending;
	const balance_cls = Math.abs(diff) < PK_TOL ? "text-success" : diff > 0 ? "text-danger" : "text-warning";
	const balance_note =
		Math.abs(diff) < PK_TOL
			? " ✓"
			: diff > 0
			  ? ` (${__("over by {0}", [format_number(diff)])})`
			  : ` (${__("{0} left", [format_number(-diff)])})`;

	const rows = plan
		.map((r, i) => {
			const { date, time } = split_dt(r.departure_time);
			return `<tr>
				<td>${i + 1}</td>
				<td><select class="form-control input-xs pk-vehicle" data-i="${i}">${vehicle_select(r.vehicle, i)}</select></td>
				<td><input type="text" inputmode="decimal" class="form-control input-xs pk-qty no-spin" data-i="${i}" value="${r.qty}" style="width:100px"></td>
				<td><select class="form-control input-xs pk-driver" data-i="${i}">${driver_select(r.driver, i)}</select></td>
				<td><input type="date" class="form-control input-xs pk-date" data-i="${i}" value="${date}" style="width:120px"></td>
				<td><input type="time" class="form-control input-xs pk-time" data-i="${i}" value="${time}" style="width:90px"></td>
				<td><a class="text-danger small pk-remove" data-i="${i}">${__("remove")}</a></td>
			</tr>`;
		})
		.join("");

	wrapper.html(`
		<div class="d-flex justify-content-between align-items-center mb-2">
			<span class="small text-muted">${__("{0} vehicle(s)", [plan.length])}</span>
			<button class="btn btn-xs btn-default pk-add">${__("+ Add vehicle")}</button>
		</div>
		<table class="table table-bordered small">
			<thead><tr>
				<th style="width:4%">${__("No")}</th>
				<th>${__("Vehicle")}</th>
				<th style="width:11%">${__("Qty")}</th>
				<th>${__("Driver")}</th>
				<th style="width:13%">${__("Date")}</th>
				<th style="width:10%">${__("Time")}</th>
				<th style="width:6%"></th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
		<div class="small ${balance_cls}">${__("Planned")}: ${format_number(total)} / ${format_number(pending.total_pending)}${balance_note}</div>
	`);

	const capacity_of = (name) => flt((vehicle_opts.find((v) => v.name === name) || {}).capacity);

	wrapper.find(".pk-vehicle").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		plan[i].vehicle = $(this).val() || null;
		const cap = capacity_of(plan[i].vehicle);
		if (cap && flt(plan[i].qty) > cap) plan[i].qty = cap;
		draw_pickup_plan(dialog, plan, pending, vehicle_opts, driver_opts);
	});
	wrapper.find(".pk-driver").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		plan[i].driver = $(this).val() || null;
	});
	wrapper.find(".pk-qty").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		const cap = capacity_of(plan[i].vehicle);
		plan[i].qty = cap ? Math.min(flt($(this).val()), cap) : flt($(this).val());
		draw_pickup_plan(dialog, plan, pending, vehicle_opts, driver_opts);
	});
	wrapper.find(".pk-date").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		const time = split_dt(plan[i].departure_time).time;
		plan[i].departure_time = `${$(this).val()} ${time}:00`;
	});
	wrapper.find(".pk-time").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		const date = split_dt(plan[i].departure_time).date || frappe.datetime.get_today();
		plan[i].departure_time = `${date} ${$(this).val()}:00`;
	});
	wrapper.find(".pk-remove").on("click", function () {
		const i = parseInt($(this).data("i"), 10);
		if (plan.length <= 1) return;
		plan.splice(i, 1);
		draw_pickup_plan(dialog, plan, pending, vehicle_opts, driver_opts);
	});
	wrapper.find(".pk-add").on("click", () => {
		const last = plan[plan.length - 1];
		const left = Math.max(pending.total_pending - total, 0);
		plan.push({
			vehicle: null,
			qty: left,
			driver: null,
			departure_time: last ? last.departure_time : frappe.datetime.now_datetime(),
		});
		draw_pickup_plan(dialog, plan, pending, vehicle_opts, driver_opts);
	});
}

// "YYYY-MM-DD HH:mm:ss" -> the {date, time} pair the two plain inputs need.
function split_dt(value) {
	const s = String(value || "").replace("T", " ");
	const date = s.slice(0, 10);
	const time = /^\d{2}:\d{2}/.test(s.slice(11, 16)) ? s.slice(11, 16) : "09:00";
	return { date, time };
}
