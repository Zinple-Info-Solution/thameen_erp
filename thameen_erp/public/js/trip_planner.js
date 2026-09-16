// Shared trip planner table used by the Sales Order and Purchase Receipt
// dialogs.
//
//   thameen.trip_planner.render(dialog, {
//       plan:      [{key, item_code, qty, vehicle, driver, departure_time, label?}],
//       limits:    {key: {label, max}}          max = pending qty per key
//       vehicles:  [{name, capacity, free, on_truck, driver}],
//       drivers:   [{name, full_name}],          optional — omit to hide the column's choices
//       allow_under: true                        may plan less than max
//   })
//   thameen.trip_planner.collect(dialog) -> plan rows with qty > 0, each carrying vehicle/driver/departure_time
//
// `departure_time` is a full "YYYY-MM-DD HH:mm:ss" string throughout — the
// Date and Time columns are two plain inputs over the same value, split and
// rejoined by `_split_dt` on every edit.
//
// Two ceilings apply to every row's qty, and they behave differently:
//
//   - The line's own total: never claim more of a line than the order still
//     owes. Typing a bigger number, or picking a truck that would push a row
//     higher, is clamped and the excess pulled back off the last sibling row
//     of that line — nothing about the order changed, so nothing is created.
//   - A vehicle's rated capacity: a truck cannot carry more than it is rated
//     for, whether picked from the dropdown or typed straight into the qty
//     box. Unlike the line ceiling, this excess IS still qty the order
//     needs — it opens a fresh row (or feeds one already waiting for a
//     truck) instead of vanishing.
//
// Choosing a truck also defaults the row's qty down to whatever of THIS item
// already sits on that truck, when that is less than what was planned —
// dispatch is telling the trip what is really there, not guessing at how
// much more room is free. An empty truck carries no such signal and leaves
// the qty exactly as it was. Either way, the same row spawns for whatever
// the truck can't cover.

frappe.provide("thameen.trip_planner");

const TP_TOL = 0.001;

// Free space on a truck, or null when it has no Capacity rated.
thameen.trip_planner._free = function (dialog, name) {
	const v = (dialog.planner.vehicles || []).find((x) => x.name === name);
	if (!v || !flt(v.capacity)) return null;
	return flt(v.free !== undefined ? v.free : v.available);
};

// The truck's own rating, full stop — not reduced by what other trips have
// already claimed. This is the ceiling a qty is not allowed past: a
// 30-capacity truck takes at most 30 on THIS trip, regardless of what else
// it is promised to elsewhere that day.
thameen.trip_planner._capacity = function (dialog, name) {
	const v = (dialog.planner.vehicles || []).find((x) => x.name === name);
	if (!v || !flt(v.capacity)) return null;
	return flt(v.capacity);
};

// Trucks already used on another row. One truck, one trip in a single plan.
// A row typed down to zero holds no real trip any more — collect() already
// drops it at creation time — so it must not keep its truck out of every
// other row's dropdown for the rest of the session.
thameen.trip_planner._taken = function (dialog, except_index) {
	return new Set(
		(dialog.planner.plan || [])
			.map((p, i) => (i === except_index || flt(p.qty) <= TP_TOL ? null : p.vehicle))
			.filter(Boolean)
	);
};

// Same rule for drivers: one driver cannot be on two trips in one plan.
thameen.trip_planner._taken_drivers = function (dialog, except_index) {
	return new Set(
		(dialog.planner.plan || [])
			.map((p, i) => (i === except_index || flt(p.qty) <= TP_TOL ? null : p.driver))
			.filter(Boolean)
	);
};

// "YYYY-MM-DD HH:mm:ss" / ISO -> the {date, time} pair the two plain inputs
// need. Missing or unparsable time falls back to a sensible default so a
// row created before a date was picked does not render an empty clock.
thameen.trip_planner._split_dt = function (value) {
	const s = String(value || "").replace("T", " ");
	const date = s.slice(0, 10);
	const time = /^\d{2}:\d{2}/.test(s.slice(11, 16)) ? s.slice(11, 16) : "09:00";
	return { date, time };
};

// What of THIS item already sits on this truck, or null if the truck carries
// none of it (empty, or loaded with something else entirely — the picker
// already keeps a conflicting truck out of the list).
thameen.trip_planner._qty_on_vehicle = function (dialog, vehicle, item_code) {
	const v = (dialog.planner.vehicles || []).find((x) => x.name === vehicle);
	if (!v || !item_code) return null;
	const row = (v.on_truck_items || []).find((it) => it.item_code === item_code);
	return row ? flt(row.qty) : null;
};

// Picking a vehicle caps the row at what this truck can actually take. Two
// independent ceilings apply, and whichever is smaller wins:
//
//   - capacity (the truck's own rating): a hard physical limit, checked even
//     on an EMPTY truck — a 30-capacity truck cannot be handed a 40-qty row
//     just because it has nothing on it yet to contradict that. This is the
//     flat rating, not shrunk by what other trips already have promised —
//     one trip is bounded by what the truck can carry, full stop.
//   - what of this item is already on the truck: a soft, informational
//     default (there is more room, but that is not what is actually loaded)
//     that only ever pulls a qty already above it down to it — it never
//     raises a row, and never applies at all to an empty truck, which has no
//     such number to offer.
//
// Either way, whatever this truck cannot cover does not vanish and does not
// get folded into some other row silently — it opens a fresh row on the same
// line with no truck chosen yet, so the dispatcher picks a second vehicle
// for exactly what the first one could not take.
thameen.trip_planner._set_qty_from_vehicle = function (dialog, index) {
	const o = dialog.planner;
	const plan = o.plan;
	const row = plan[index];
	const before = flt(row.qty);

	const capacity = thameen.trip_planner._capacity(dialog, row.vehicle);
	const on_vehicle = thameen.trip_planner._qty_on_vehicle(dialog, row.vehicle, row.item_code);

	let target = before;
	if (capacity !== null) target = Math.min(target, capacity);
	if (on_vehicle !== null) target = Math.min(target, on_vehicle);

	if (target + TP_TOL >= before) return;

	const short = before - target;
	row.qty = target;

	// Reuse a sibling row that has no truck yet, so re-picking a vehicle on
	// the same line does not keep spawning empty rows.
	const spare = plan.find((p, j) => j !== index && p.key === row.key && !p.vehicle);
	if (spare) {
		spare.qty = flt(spare.qty) + short;
	} else {
		plan.push({ ...row, vehicle: null, driver: null, driver_manual: false, qty: short });
	}
};

// The one place a row's qty is written from anywhere but the qty input
// itself. Two ceilings apply, and they are handled differently on purpose:
//
//   - Capacity: a truck cannot be typed past what it can physically hold.
//     What that clamp removes is still qty the ORDER needs — nothing about
//     the line changed — so it does not vanish: it opens a fresh row (or
//     feeds one already waiting for a truck), same as picking a truck with
//     too little room does in `_set_qty_from_vehicle`.
//   - The line's own total: never claim more of a line than the order still
//     owes. A sibling row absorbs the difference either way (gives up space
//     when this row grows, picks up the remainder when it shrinks) — that
//     sibling's own current qty is excluded from the ceiling, not counted
//     against it, so typing 20 into a row that only had 15 because a sibling
//     is sitting on the other 85 of a 100 line still reaches 20, pulling the
//     5 it needs straight off that sibling in the same edit.
thameen.trip_planner._apply_qty = function (dialog, index, new_qty) {
	const o = dialog.planner;
	const plan = o.plan;
	const row = plan[index];
	const key = row.key;

	const capacity = thameen.trip_planner._capacity(dialog, row.vehicle);
	let requested = Math.max(flt(new_qty), 0);
	let overflow = 0;
	if (capacity !== null && requested > capacity + TP_TOL) {
		overflow = requested - capacity;
		requested = capacity;
	}

	let sibling = -1;
	for (let j = plan.length - 1; j >= 0; j--) {
		if (j !== index && plan[j].key === key) {
			sibling = j;
			break;
		}
	}

	const fixed_elsewhere = plan.reduce(
		(sum, p, j) => (j !== index && j !== sibling && p.key === key ? sum + flt(p.qty) : sum),
		0
	);
	const ceiling = flt((o.limits[key] || {}).max) - fixed_elsewhere;

	const nq = Math.min(requested, Math.max(ceiling, 0));
	const diff = nq - flt(row.qty);
	row.qty = nq;
	if (sibling >= 0) {
		plan[sibling].qty = Math.max(flt(plan[sibling].qty) - diff, 0);
	}

	if (overflow > TP_TOL) {
		const spare = plan.find((p, j) => j !== index && p.key === key && !p.vehicle);
		if (spare) {
			spare.qty = flt(spare.qty) + overflow;
		} else {
			plan.push({ ...row, vehicle: null, driver: null, driver_manual: false, qty: overflow });
		}
	}
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
			driver: p.driver || null,
			departure_time: p.departure_time || null,
			...(p.extra || {}),
		}));
};

thameen.trip_planner._draw = function (dialog) {
	const o = dialog.planner;
	const wrapper = dialog.fields_dict.plan.$wrapper;
	const plan = o.plan;
	// What a truck's dropdown entry says about its load: while browsing
	// options, one number only — the qty of THIS item already on the truck
	// (the same number the qty column gets capped to on selection). Capacity
	// only stands in for that when there is nothing on the truck to quote —
	// an empty truck has no qty of its own yet. The fuller picture (both
	// capacity AND qty together) shows up once a truck is actually chosen,
	// in the Available stock column.
	const vehicle_remaining_label = (v, item_code) => {
		if (v.is_empty) {
			return flt(v.capacity)
				? `${__("empty")} · ${__("cap")} ${format_number(v.capacity)}`
				: __("no capacity set");
		}
		const on_item = (v.on_truck_items || []).find((it) => it.item_code === item_code);
		const qty = on_item ? flt(on_item.qty) : flt(v.on_truck);
		return format_number(qty);
	};

	const vehicle_opts = (value, index, item_code) => {
		const taken = thameen.trip_planner._taken(dialog, index);
		return [`<option value="">${__("— choose later —")}</option>`]
			.concat(
				o.vehicles
					.filter((v) => v.name === value || !taken.has(v.name))
					// A truck already carrying a different cement is not a
					// candidate for this line — empty trucks take anything.
					.filter(
						(v) =>
							v.name === value ||
							!item_code ||
							v.is_empty ||
							(v.on_truck_items || []).some((it) => it.item_code === item_code)
					)
					.map(
						(v) =>
							`<option value="${frappe.utils.escape_html(v.name)}" ${v.name === value ? "selected" : ""}>` +
							`${frappe.utils.escape_html(v.name)} · ${vehicle_remaining_label(v, item_code)}</option>`
					)
			)
			.join("");
	};

	// Named drivers come from `o.drivers` (the Sales Order dialog supplies the
	// full active roster). Callers that only pass vehicles — the Purchase
	// Order dialog today — still get each truck's own driver as an option, so
	// a row auto-filled from its vehicle shows a real selection instead of
	// looking like nothing was picked.
	const driver_opts = (value, index) => {
		const taken = thameen.trip_planner._taken_drivers(dialog, index);
		const known = new Map((o.drivers || []).map((d) => [d.name, d.full_name || d.name]));
		(o.vehicles || []).forEach((v) => {
			if (v.driver && !known.has(v.driver)) known.set(v.driver, v.driver);
		});
		if (value && !known.has(value)) known.set(value, value);
		return [`<option value="">${__("— choose later —")}</option>`]
			.concat(
				Array.from(known.entries())
					.filter(([name]) => name === value || !taken.has(name))
					.map(
						([name, label]) =>
							`<option value="${frappe.utils.escape_html(name)}" ${name === value ? "selected" : ""}>` +
							`${frappe.utils.escape_html(label)}</option>`
					)
			)
			.join("");
	};

	// Once a truck is actually chosen, spell out both numbers together — what
	// it is rated for and what of THIS item is really on it — instead of the
	// single figure the dropdown makes do with while still browsing options.
	const truck_state = (p) => {
		if (!p.vehicle) return `<span class="text-muted">—</span>`;
		const v = o.vehicles.find((x) => x.name === p.vehicle);
		if (!v) return `<span class="text-muted">—</span>`;
		if (!flt(v.capacity)) return `<span class="text-danger">${__("no capacity set")}</span>`;
		const on_item = (v.on_truck_items || []).find((it) => it.item_code === p.item_code);
		const qty = on_item ? flt(on_item.qty) : 0;
		const available = qty > 0 ? format_number(qty) : `<span class="text-muted">${__("empty")}</span>`;
		return (
			`<span class="small">${__("cap")} <b>${format_number(v.capacity)}</b> · ` +
			`${__("available qty")} <b>${available}</b></span>`
		);
	};

	const rows = plan
		.map((p, i) => {
			// Compared against FREE space, not the rating: a truck with 180 of
			// its 300 already promised elsewhere has 120, not 300.
			const free = thameen.trip_planner._free(dialog, p.vehicle);
			const over = free !== null && flt(p.qty) > free + TP_TOL;
			const { date, time } = thameen.trip_planner._split_dt(p.departure_time);
			return `<tr class="${over ? "table-warning" : ""}">
				<td>${i + 1}</td>
				<td>${frappe.utils.escape_html(p.label || p.item_code)}</td>
				<td><input type="text" inputmode="decimal" class="form-control input-xs tp-qty no-spin" data-i="${i}" value="${p.qty}" style="width:110px">
					${over ? `<div class="text-danger small">${__("over by {0}", [format_number(flt(p.qty) - free)])}</div>` : ""}</td>
				<td><select class="form-control input-xs tp-vehicle" data-i="${i}">${vehicle_opts(p.vehicle, i, p.item_code)}</select></td>
				<td>${truck_state(p)}</td>
				<td><select class="form-control input-xs tp-driver" data-i="${i}">${driver_opts(p.driver, i)}</select></td>
				<td><input type="date" class="form-control input-xs tp-date" data-i="${i}" value="${date}" style="width:120px"></td>
				<td><input type="time" class="form-control input-xs tp-time" data-i="${i}" value="${time}" style="width:90px"></td>
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
			</span>
		</div>
		<table class="table table-bordered small">
			<thead><tr>
				<th style="width:4%">${__("No")}</th><th style="width:16%">${__("Line")}</th><th style="width:10%">${__("Qty")}</th>
				<th>${__("Vehicle")}</th><th style="width:12%">${__("Available stock")}</th>
				<th>${__("Driver")}</th>
				<th style="width:11%">${__("Date")}</th><th style="width:9%">${__("Time")}</th><th style="width:6%"></th>
			</tr></thead>
			<tbody>${rows}</tbody>
		</table>
		<div class="small mb-2">${__("Planned")}: ${balance}</div>`);

	dialog.get_primary_btn().prop("disabled", !ok);

	wrapper.find(".tp-qty").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		thameen.trip_planner._apply_qty(dialog, i, $(this).val());
		thameen.trip_planner._draw(dialog);
	});
	wrapper.find(".tp-vehicle").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		plan[i].vehicle = $(this).val() || null;
		// Default the row to this truck's usual driver, unless the dispatcher
		// already picked one by hand — a manual pick survives a truck change.
		if (!plan[i].driver_manual) {
			const v = (o.vehicles || []).find((x) => x.name === plan[i].vehicle);
			plan[i].driver = (v && v.driver) || null;
		}
		// Cap the qty to what this item already sits on the truck — not to
		// free capacity — and split off a new row for whatever this truck
		// cannot cover.
		thameen.trip_planner._set_qty_from_vehicle(dialog, i);
		thameen.trip_planner._draw(dialog);
	});
	wrapper.find(".tp-driver").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		plan[i].driver = $(this).val() || null;
		plan[i].driver_manual = true;
		thameen.trip_planner._draw(dialog);
	});
	wrapper.find(".tp-date").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		const time = thameen.trip_planner._split_dt(plan[i].departure_time).time;
		plan[i].departure_time = `${$(this).val()} ${time}:00`;
	});
	wrapper.find(".tp-time").on("change", function () {
		const i = parseInt($(this).data("i"), 10);
		const date = thameen.trip_planner._split_dt(plan[i].departure_time).date || frappe.datetime.get_today();
		plan[i].departure_time = `${date} ${$(this).val()}:00`;
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
};
