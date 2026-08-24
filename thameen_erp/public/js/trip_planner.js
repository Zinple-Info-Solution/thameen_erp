// Shared trip planner table used by the Sales Order and Purchase Order dialogs.
//
//   thameen.trip_planner.render(dialog, {
//       plan:      [{key, item_code, qty, vehicle, departure_time, label?}],
//       limits:    {key: {label, max}}          max = pending qty per key
//       vehicles:  [{name, capacity, available}],
//       allow_under: true                        may plan less than max
//   })
//   thameen.trip_planner.collect(dialog) -> plan rows with qty > 0
//
// Editing a qty rebalances the last other trip with the same key, so the total
// per key never drifts by accident. Vehicle over-capacity is flagged, not
// blocked. "Same truck, one day apart" is the common real-world answer to
// "we only have one tanker".

frappe.provide("thameen.trip_planner");

thameen.trip_planner.render = function (dialog, opts) {
	dialog.planner = opts;
	thameen.trip_planner._draw(dialog);
};

thameen.trip_planner.collect = function (dialog) {
	return ((dialog.planner && dialog.planner.plan) || [])
		.filter((p) => flt(p.qty) > 0)
		.map((p) => ({
			key: p.key,
			item_code: p.item_code,
			qty: flt(p.qty),
			vehicle: p.vehicle || null,
			departure_time: p.departure_time || null,
			...(p.extra || {}),
		}));
};

thameen.trip_planner.auto_split = function (dialog, vehicle, start_date, days_between) {
	// Rewrite the plan: every key split by this truck's capacity, one trip per day.
	const o = dialog.planner;
	const v = o.vehicles.find((x) => x.name === vehicle);
	if (!v || flt(v.capacity) <= 0) {
		frappe.msgprint(__("Set a Capacity on {0} before planning by it.", [vehicle]));
		return;
	}
	const totals = {};
	o.plan.forEach((p) => (totals[p.key] = flt(totals[p.key]) + flt(p.qty)));
	const template = {};
	o.plan.forEach((p) => (template[p.key] = p));
	const out = [];
	let date = start_date || frappe.datetime.get_today();
	Object.keys(totals).forEach((key) => {
		let left = totals[key];
		while (left > 0.001) {
			const take = Math.min(left, flt(v.capacity));
			out.push({ ...template[key], qty: take, vehicle, departure_time: date });
			left -= take;
			date = frappe.datetime.add_days(date, days_between || 1);
		}
	});
	o.plan = out;
	thameen.trip_planner._draw(dialog);
};

thameen.trip_planner._draw = function (dialog) {
	const o = dialog.planner;
	const wrapper = dialog.fields_dict.plan.$wrapper;
	const plan = o.plan;
	const cap_of = (n) => {
		const v = o.vehicles.find((x) => x.name === n);
		return v ? flt(v.capacity) : 0;
	};
	const vehicle_opts = (value) =>
		[`<option value="">${__("— choose later —")}</option>`]
			.concat(
				o.vehicles.map(
					(v) =>
						`<option value="${frappe.utils.escape_html(v.name)}" ${v.name === value ? "selected" : ""}>` +
						`${frappe.utils.escape_html(v.name)} · ${__("cap")} ${format_number(v.capacity)} · ${__("free")} ${format_number(v.available)}</option>`
				)
			)
			.join("");

	const rows = plan
		.map((p, i) => {
			const cap = cap_of(p.vehicle);
			const over = cap && flt(p.qty) > cap + 0.001;
			return `<tr class="${over ? "table-warning" : ""}">
				<td>${i + 1}</td>
				<td>${frappe.utils.escape_html(p.label || p.item_code)}</td>
				<td><input type="number" step="any" min="0" class="form-control input-xs tp-qty" data-i="${i}" value="${p.qty}" style="width:110px">
					${over ? `<div class="text-danger small">${__("over {0}", [format_number(cap)])}</div>` : ""}</td>
				<td><select class="form-control input-xs tp-vehicle" data-i="${i}">${vehicle_opts(p.vehicle)}</select></td>
				<td><input type="date" class="form-control input-xs tp-date" data-i="${i}" value="${(p.departure_time || "").slice(0, 10)}"></td>
				<td><a class="text-danger small tp-remove" data-i="${i}">${__("remove")}</a></td>
			</tr>`;
		})
		.join("");

	const placed = {};
	plan.forEach((p) => (placed[p.key] = flt(placed[p.key]) + flt(p.qty)));
	let ok = plan.some((p) => flt(p.qty) > 0);
	const balance = Object.keys(o.limits)
		.map((key) => {
			const lim = o.limits[key];
			const got = flt(placed[key]);
			const diff = got - flt(lim.max);
			if (diff > 0.001 || (!o.allow_under && diff < -0.001)) ok = false;
			const cls = diff > 0.001 ? "text-danger" : Math.abs(diff) < 0.001 ? "text-success" : o.allow_under ? "text-warning" : "text-danger";
			const note = Math.abs(diff) < 0.001 ? " ✓" : diff > 0 ? ` (${__("over by {0}", [format_number(diff)])})` : ` (${__("{0} left", [format_number(-diff)])})`;
			return `<span class="${cls} mr-3">${frappe.utils.escape_html(lim.label)}: ${format_number(got)} / ${format_number(lim.max)}${note}</span>`;
		})
		.join("");

	wrapper.html(`
		<div class="d-flex justify-content-between align-items-center mb-2">
			<span class="small">${__("{0} trip(s). Quantities in stock units. Edit any qty, truck or date — the last trip of the same line rebalances.", [plan.length])}</span>
			<span>
				<button class="btn btn-xs btn-default tp-add">${__("+ Add trip")}</button>
				<button class="btn btn-xs btn-default tp-same ml-1">${__("Same truck for all, one day apart")}</button>
			</span>
		</div>
		<table class="table table-bordered small">
			<thead><tr>
				<th style="width:5%">#</th><th style="width:22%">${__("Line")}</th><th style="width:15%">${__("Qty")}</th>
				<th>${__("Vehicle")}</th><th style="width:16%">${__("Departure")}</th><th style="width:8%"></th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
		<div class="small mb-2">${__("Planned")}: ${balance}</div>`);

	dialog.get_primary_btn().prop("disabled", !ok);

	wrapper.find(".tp-qty").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		const nq = Math.max(flt($(this).val()), 0);
		const diff = nq - flt(plan[i].qty);
		plan[i].qty = nq;
		for (let j = plan.length - 1; j >= 0; j--) {
			if (j !== i && plan[j].key === plan[i].key) {
				plan[j].qty = Math.max(flt(plan[j].qty) - diff, 0);
				break;
			}
		}
		thameen.trip_planner._draw(dialog);
	});
	wrapper.find(".tp-vehicle").on("change", function () {
		plan[parseInt($(this).data("i"), 10)].vehicle = $(this).val() || null;
		thameen.trip_planner._draw(dialog);
	});
	wrapper.find(".tp-date").on("change", function () {
		plan[parseInt($(this).data("i"), 10)].departure_time = $(this).val();
	});
	wrapper.find(".tp-remove").on("click", function () {
		const i = parseInt($(this).data("i"), 10);
		const sib = plan.findIndex((p, j) => j !== i && p.key === plan[i].key);
		if (sib >= 0) plan[sib].qty = flt(plan[sib].qty) + flt(plan[i].qty);
		plan.splice(i, 1);
		thameen.trip_planner._draw(dialog);
	});
	wrapper.find(".tp-add").on("click", () => {
		const last = plan[plan.length - 1];
		if (!last) return;
		plan.push({ ...last, qty: 0, departure_time: frappe.datetime.add_days(last.departure_time || frappe.datetime.get_today(), 1) });
		thameen.trip_planner._draw(dialog);
	});
	wrapper.find(".tp-same").on("click", () => {
		const v = (o.same_truck && o.same_truck()) || (plan[0] && plan[0].vehicle);
		if (!v) {
			frappe.msgprint(__("Choose a truck first."));
			return;
		}
		const base = (o.start_date && o.start_date()) || frappe.datetime.get_today();
		const step = (o.days_between && o.days_between()) || 1;
		plan.forEach((p, i) => {
			p.vehicle = v;
			p.departure_time = frappe.datetime.add_days(base, i * step);
		});
		thameen.trip_planner._draw(dialog);
	});
};
