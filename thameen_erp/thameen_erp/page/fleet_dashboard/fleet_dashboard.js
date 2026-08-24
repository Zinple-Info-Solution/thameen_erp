frappe.pages["fleet-dashboard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Fleet Dashboard"),
		single_column: true,
	});

	const dashboard = new FleetDashboard(page);
	dashboard.render();
};

class FleetDashboard {
	constructor(page) {
		this.page = page;
		this.$body = $('<div class="fleet-dashboard"></div>').appendTo(page.main);
		this.inject_styles();
		this.add_filters();
		this.page.set_primary_action(__("Refresh"), () => this.render(), "refresh");
	}

	inject_styles() {
		if (document.getElementById("fleet-dashboard-styles")) return;
		$(`<style id="fleet-dashboard-styles">
			.fleet-dashboard { padding: 0 0 2rem 0; }
			.fd-grid { display: grid; gap: var(--margin-md, 12px); }
			.fd-kpis { grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); margin-bottom: 1rem; }
			.fd-panels { grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); }
			.fd-card {
				background: var(--card-bg, var(--fg-color));
				border: 1px solid var(--border-color);
				border-radius: var(--border-radius-md, 8px);
				padding: 1rem 1.15rem;
			}
			.fd-kpi-label { font-size: var(--text-sm); color: var(--text-muted);
				text-transform: uppercase; letter-spacing: .04em; }
			.fd-kpi-value { font-size: 1.55rem; font-weight: 600; color: var(--text-color);
				margin-top: .25rem; line-height: 1.2; }
			.fd-kpi-sub { font-size: var(--text-sm); color: var(--text-muted); margin-top: .2rem; }
			.fd-card-title { font-weight: 600; margin-bottom: .85rem; color: var(--text-color); }
			.fd-pos { color: var(--text-on-green, #2e7d32); }
			.fd-neg { color: var(--text-on-red, #c62828); }
			.fd-table { width: 100%; font-size: var(--text-sm); }
			.fd-table td { padding: .35rem .25rem; border-bottom: 1px solid var(--border-color); }
			.fd-table td:last-child { text-align: right; }
			.fd-muted { color: var(--text-muted); }
			.fd-pill { display:inline-block; padding: .1rem .5rem; border-radius: 999px;
				font-size: var(--text-xs); border: 1px solid var(--border-color); }
		</style>`).appendTo(document.head);
	}

	add_filters() {
		this.company = this.page.add_field({
			fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			change: () => this.render(),
		});
		this.from_date = this.page.add_field({
			fieldname: "from_date", label: __("From"), fieldtype: "Date",
			default: frappe.datetime.add_months(frappe.datetime.month_start(), -5),
			change: () => this.render(),
		});
		this.to_date = this.page.add_field({
			fieldname: "to_date", label: __("To"), fieldtype: "Date",
			default: frappe.datetime.month_end(),
			change: () => this.render(),
		});
	}

	render() {
		this.$body.html(`<div class="text-muted p-4">${__("Loading fleet data…")}</div>`);
		frappe.call({
			method: "thameen_erp.thameen_erp.page.fleet_dashboard.fleet_dashboard.get_dashboard_data",
			args: {
				company: this.company.get_value(),
				from_date: this.from_date.get_value(),
				to_date: this.to_date.get_value(),
			},
			callback: ({ message }) => {
				if (!message) return;
				this.data = message;
				this.$body.empty();
				this.render_kpis();
				this.render_panels();
			},
		});
	}

	render_kpis() {
		const k = this.data.kpi;
		const cards = [
			[__("Trips"), k.total_trips, __("{0} completed", [k.completed_trips])],
			[__("Distance"), format_number(k.distance, null, 0), __("total km")],
			[__("Delivered Qty"), format_number(k.delivered_qty, null, 2), ""],
			[__("Freight Revenue"), format_currency(k.freight_revenue), ""],
			[__("Fleet Cost"), format_currency(k.fleet_cost), ""],
			[__("Contribution"), format_currency(k.contribution), "",
				k.contribution >= 0 ? "fd-pos" : "fd-neg"],
			[__("Cost / Km"), format_currency(k.cost_per_km), ""],
			[__("Active Vehicles"), k.active_vehicles, ""],
		];

		const html = cards.map(([label, value, sub, cls = ""]) => `
			<div class="fd-card">
				<div class="fd-kpi-label">${label}</div>
				<div class="fd-kpi-value ${cls}">${value}</div>
				${sub ? `<div class="fd-kpi-sub">${sub}</div>` : ""}
			</div>`).join("");

		$(`<div class="fd-grid fd-kpis">${html}</div>`).appendTo(this.$body);
	}

	render_panels() {
		const $grid = $('<div class="fd-grid fd-panels"></div>').appendTo(this.$body);

		this.panel($grid, __("Monthly Freight & Trips"),
			bar_chart(this.data.monthly.map((m) => ({
				label: m.month, value: flt(m.freight), secondary: flt(m.trips),
			}))));

		this.panel($grid, __("Fleet Status"),
			donut_chart(this.data.fleet_status.map((s) => ({
				label: s.status, value: s.count,
			}))));

		this.panel($grid, __("Top Vehicles by Freight"),
			bar_chart(this.data.top_vehicles.map((v) => ({
				label: v.vehicle, value: flt(v.freight), secondary: flt(v.trips),
			})), true));

		this.panel($grid, __("POD Pending"), list_table(
			this.data.pod_pending.map((t) => [
				`<a href="/app/delivery-trip/${encodeURIComponent(t.name)}">${frappe.utils.escape_html(t.name)}</a>`,
				frappe.utils.escape_html(t.vehicle || "—"),
				frappe.datetime.str_to_user(t.departure_time),
			]), __("No trips awaiting proof of delivery.")));

		this.panel($grid, __("Document & Licence Alerts"), list_table(
			[
				...this.data.alerts.documents.map((d) => [
					`<a href="/app/vehicle-document/${encodeURIComponent(d.name)}">${frappe.utils.escape_html(d.vehicle)}</a>`,
					frappe.utils.escape_html(d.document_type),
					`<span class="fd-pill ${d.status === "Expired" ? "fd-neg" : ""}">${frappe.datetime.str_to_user(d.expiry_date)}</span>`,
				]),
				...this.data.alerts.licences.map((l) => [
					`<a href="/app/driver/${encodeURIComponent(l.name)}">${frappe.utils.escape_html(l.full_name || l.name)}</a>`,
					__("Driving Licence"),
					`<span class="fd-pill fd-neg">${frappe.datetime.str_to_user(l.expiry_date)}</span>`,
				]),
			], __("Nothing expiring in the next 30 days.")));
	}

	panel($parent, title, body_html) {
		$(`<div class="fd-card">
			<div class="fd-card-title">${title}</div>
			${body_html}
		</div>`).appendTo($parent);
	}
}

// ---------------------------------------------------------------------------
// Hand-drawn SVG charts. Colours come from CSS variables so light and dark
// themes are both respected without a second stylesheet.
// ---------------------------------------------------------------------------

function empty_state(message) {
	return `<div class="fd-muted text-center" style="padding:2rem 0">${message}</div>`;
}

function bar_chart(rows, horizontal = false) {
	if (!rows || !rows.length) return empty_state(__("No data in this period."));

	const W = 480, H = 220, pad = { t: 12, r: 12, b: 34, l: 52 };
	const max = Math.max(...rows.map((r) => r.value), 1);
	const innerW = W - pad.l - pad.r;
	const innerH = H - pad.t - pad.b;
	const step = innerW / rows.length;
	const barW = Math.min(step * 0.6, 46);

	const gridlines = [0, 0.25, 0.5, 0.75, 1].map((f) => {
		const y = pad.t + innerH * (1 - f);
		return `<line x1="${pad.l}" y1="${y}" x2="${W - pad.r}" y2="${y}"
			stroke="var(--border-color)" stroke-width="1" ${f ? 'stroke-dasharray="3 3"' : ""}/>
			<text x="${pad.l - 8}" y="${y + 4}" text-anchor="end"
			font-size="10" fill="var(--text-muted)">${format_number(max * f, null, 0)}</text>`;
	}).join("");

	const bars = rows.map((r, i) => {
		const h = Math.max((r.value / max) * innerH, r.value > 0 ? 2 : 0);
		const x = pad.l + step * i + (step - barW) / 2;
		const y = pad.t + innerH - h;
		const label = String(r.label).length > 10 ? String(r.label).slice(0, 9) + "…" : r.label;
		return `
			<rect x="${x}" y="${y}" width="${barW}" height="${h}" rx="3"
				fill="var(--blue-500, #2490ef)" opacity="0.9">
				<title>${frappe.utils.escape_html(String(r.label))}: ${format_currency(r.value)}${
					r.secondary != null ? ` · ${r.secondary} ${__("trips")}` : ""}</title>
			</rect>
			<text x="${x + barW / 2}" y="${H - pad.b + 16}" text-anchor="middle"
				font-size="10" fill="var(--text-muted)">${frappe.utils.escape_html(String(label))}</text>`;
	}).join("");

	return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img"
		aria-label="${__("Bar chart")}">${gridlines}${bars}</svg>`;
}

function donut_chart(rows) {
	const data = (rows || []).filter((r) => r.value > 0);
	if (!data.length) return empty_state(__("No vehicles recorded."));

	const palette = {
		Available: "var(--green-500, #28a745)",
		Assigned: "var(--blue-500, #2490ef)",
		"On Trip": "var(--purple-500, #7c4dff)",
		"Under Maintenance": "var(--orange-500, #f5a623)",
		"Out of Service": "var(--red-500, #e24c4c)",
	};

	const total = data.reduce((s, r) => s + r.value, 0);
	const cx = 110, cy = 110, r = 82, thickness = 26;
	let angle = -Math.PI / 2;

	const arcs = data.map((row, i) => {
		const sweep = (row.value / total) * Math.PI * 2;
		const end = angle + sweep;
		const large = sweep > Math.PI ? 1 : 0;
		const p = (rad, radius) => [cx + Math.cos(rad) * radius, cy + Math.sin(rad) * radius];
		const [x1, y1] = p(angle, r);
		const [x2, y2] = p(end, r);
		angle = end;
		return `<path d="M ${x1} ${y1} A ${r} ${r} 0 ${large} 1 ${x2} ${y2}"
			fill="none" stroke="${palette[row.label] || "var(--gray-400, #9ea7ad)"}"
			stroke-width="${thickness}" stroke-linecap="butt">
			<title>${frappe.utils.escape_html(String(row.label))}: ${row.value}</title></path>`;
	}).join("");

	const legend = data.map((row) => `
		<div style="display:flex;align-items:center;gap:.5rem;font-size:var(--text-sm);margin:.2rem 0">
			<span style="width:10px;height:10px;border-radius:2px;display:inline-block;
				background:${palette[row.label] || "var(--gray-400, #9ea7ad)"}"></span>
			<span>${frappe.utils.escape_html(String(row.label))}</span>
			<span class="fd-muted" style="margin-left:auto">${row.value}</span>
		</div>`).join("");

	return `<div style="display:flex;gap:1rem;align-items:center;flex-wrap:wrap">
		<svg viewBox="0 0 220 220" width="200" role="img" aria-label="${__("Fleet status")}">
			${arcs}
			<text x="110" y="105" text-anchor="middle" font-size="26" font-weight="600"
				fill="var(--text-color)">${total}</text>
			<text x="110" y="126" text-anchor="middle" font-size="11"
				fill="var(--text-muted)">${__("vehicles")}</text>
		</svg>
		<div style="flex:1;min-width:150px">${legend}</div>
	</div>`;
}

function list_table(rows, empty_message) {
	if (!rows || !rows.length) return empty_state(empty_message);
	const body = rows.map((cells) =>
		`<tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("");
	return `<table class="fd-table"><tbody>${body}</tbody></table>`;
}
