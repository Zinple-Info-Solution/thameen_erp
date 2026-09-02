// Shared trip planner table used by the Sales Order and Purchase Order dialogs.
//
//   thameen.trip_planner.render(dialog, {
//       plan:      [{key, item_code, qty, vehicle, departure_time, label?}],
//       limits:    {key: {label, max}}          max = pending qty per key
//       vehicles:  [{name, capacity, free, on_truck}],
//       allow_under: true                        may plan less than max
//   })
//   thameen.trip_planner.collect(dialog) -> plan rows with qty > 0
//
// Two rules hold while the dialog is open. Per line, the quantities across all
// rows never exceed what the order still owes — typing a bigger number is
// clamped, not accepted and flagged later. And choosing a truck resizes that
// row to the truck's FREE space, pushing what no longer fits onto the other
// rows of the same line, or onto a fresh row when every existing one is
// already spoken for.
//
// Free space, not rated capacity: a truck with 180 of its 300 promised to
// other open trips has 120. Planning against 300 is how trucks get
// double-booked. "Same truck, one day apart" deliberately reuses one plate
// across days and is the common answer to "we only have one tanker".

frappe.provide("thameen.trip_planner");

const TP_TOL = 0.001;

// Free space on a truck, or null when it has no Capacity rated.
thameen.trip_planner._free = function (dialog, name) {
	const v = (dialog.planner.vehicles || []).find((x) => x.name === name);
	if (!v || !flt(v.capacity)) return null;
	return flt(v.free !== undefined ? v.free : v.available);
};

// Trucks already used on another row. One truck, one trip in a single plan.
thameen.trip_planner._taken = function (dialog, except_index) {
	return new Set(
		(dialog.planner.plan || [])
			.map((p, i) => (i === except_index ? null : p.vehicle))
			.filter(Boolean)
	);
};

// Choosing a truck sizes that row to the space it has. Everything still
// unplaced on that line collapses onto ONE row — the first without a truck —
// so the tail shrinks and disappears as trucks are assigned, instead of
// splitting into fractions across rows nobody has picked yet.
thameen.trip_planner._fit_row = function (dialog, index) {
	const o = dialog.planner;
	const row = o.plan[index];
	const free = thameen.trip_planner._free(dialog, row.vehicle);
	if (free === null) return;

	const line = o.plan.filter((p) => p.key === row.key);
	const line_total = line.reduce((sum, p) => sum + flt(p.qty), 0);

	let left = line_total;
	// Rows with a truck take what that truck holds, in the order they appear.
	o.plan.forEach((p) => {
		if (p.key !== row.key) return;
		const p_free = thameen.trip_planner._free(dialog, p.vehicle);
		if (p_free === null) {
			p.qty = 0;
			return;
		}
		p.qty = Math.max(Math.min(p_free, left), 0);
		left -= p.qty;
	});

	if (left > TP_TOL) {
		const spare = o.plan.find(
			(p) => p.key === row.key && thameen.trip_planner._free(dialog, p.vehicle) === null
		);
		if (spare) {
			spare.qty = left;
		} else {
			o.plan.push({
				...row,
				qty: left,
				vehicle: null,
				departure_time: frappe.datetime.add_days(
					row.departure_time || frappe.datetime.get_today(),
					1
				),
			});
		}
	}

	// Rows left at zero are noise, unless a line would end up with none at all.
	o.plan = o.plan.filter(
		(p) => flt(p.qty) > TP_TOL || o.plan.filter((q) => q.key === p.key).length === 1
	);
};

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
	const vehicle_opts = (value, index) => {
		const taken = thameen.trip_planner._taken(dialog, index);
		return [`<option value="">${__("— choose later —")}</option>`]
			.concat(
				o.vehicles
					.filter((v) => v.name === value || !taken.has(v.name))
					.map((v) => {
						const free = flt(v.free !== undefined ? v.free : v.available);
						return (
							`<option value="${frappe.utils.escape_html(v.name)}" ${v.name === value ? "selected" : ""}>` +
							`${frappe.utils.escape_html(v.name)} · ${__("cap")} ${format_number(v.capacity)} · ${__("free")} ${format_number(free)}</option>`
						);
					})
			)
			.join("");
	};

	// What the chosen truck is physically carrying, named by its warehouse.
	// The free-space figures are already on every option in the dropdown to
	// the left, so repeating them here was the same numbers twice on one row.
	const truck_state = (p) => {
		if (!p.vehicle) return `<span class="text-muted">—</span>`;
		const v = o.vehicles.find((x) => x.name === p.vehicle);
		if (!v) return `<span class="text-muted">—</span>`;
		if (!flt(v.capacity)) return `<span class="text-danger">${__("no capacity set")}</span>`;
		if (!flt(v.on_truck)) return `<span class="text-muted small">${__("empty")}</span>`;
		return (
			`<span class="small">${frappe.utils.escape_html(v.warehouse || p.vehicle)} ` +
			`<b>${format_number(v.on_truck)}</b></span>`
		);
	};

	const rows = plan
		.map((p, i) => {
			// Compared against FREE space, not the rating: a truck with 180 of
			// its 300 already promised elsewhere has 120, not 300.
			const free = thameen.trip_planner._free(dialog, p.vehicle);
			const over = free !== null && flt(p.qty) > free + TP_TOL;
			return `<tr class="${over ? "table-warning" : ""}">
				<td>${i + 1}</td>
				<td>${frappe.utils.escape_html(p.label || p.item_code)}</td>
				<td><input type="number" step="any" min="0" class="form-control input-xs tp-qty" data-i="${i}" value="${p.qty}" style="width:110px">
					${over ? `<div class="text-danger small">${__("over by {0}", [format_number(flt(p.qty) - free)])}</div>` : ""}</td>
				<td><select class="form-control input-xs tp-vehicle" data-i="${i}">${vehicle_opts(p.vehicle, i)}</select></td>
				<td>${truck_state(p)}</td>
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
			<span class="small text-muted">${__("{0} trip(s)", [plan.length])}</span>
			<span>
				<button class="btn btn-xs btn-default tp-add">${__("+ Add trip")}</button>
				<button class="btn btn-xs btn-default tp-same ml-1">${__("Same truck for all, one day apart")}</button>
			</span>
		</div>
		<table class="table table-bordered small">
			<thead><tr>
				<th style="width:5%">#</th><th style="width:20%">${__("Line")}</th><th style="width:14%">${__("Qty")}</th>
				<th>${__("Vehicle")}</th><th style="width:16%">${__("Available stock")}</th>
				<th style="width:14%">${__("Departure")}</th><th style="width:7%"></th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
		<div class="small mb-2">${__("Planned")}: ${balance}</div>`);

	dialog.get_primary_btn().prop("disabled", !ok);

	wrapper.find(".tp-qty").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		// Never let a row take more of a line than the order still owes.
		const elsewhere = plan.reduce(
			(sum, p, j) => (j !== i && p.key === plan[i].key ? sum + flt(p.qty) : sum),
			0
		);
		const ceiling = flt((o.limits[plan[i].key] || {}).max) - elsewhere;
		const nq = Math.min(Math.max(flt($(this).val()), 0), Math.max(ceiling, 0));
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
		const i = parseInt($(this).data("i"), 10);
		plan[i].vehicle = $(this).val() || null;
		thameen.trip_planner._fit_row(dialog, i);
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
