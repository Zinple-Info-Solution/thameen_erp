"""Delivery Trip (ERPNext standard) used as the cement Trip Sheet.

Why this is a *class override* and not a set of `doc_events`
-----------------------------------------------------------
ERPNext's own `DeliveryTrip.update_status()` derives the status from the
`visited` flag on `delivery_stops`, and it runs on every `on_submit` and
`on_update_after_submit`. A `doc_events` hook always runs *after* the
controller method, so the standard machine overwrote every custom state
("Loading", "Delivered", "POD Pending") back to "Scheduled" the instant the
document was saved. The only correct fix is to replace that method, which
means owning the class.

Lifecycle
---------
    Draft -> Scheduled -> Loading -> In Transit -> Delivered -> POD Pending -> Completed

  * Loading    : one Material Transfer moves every trip row from its loading
                 warehouse onto the vehicle warehouse (the "truck stock").
  * Delivered  : one Delivery Note per Sales Order on the trip, raised from the
                 vehicle warehouse. ERPNext then updates `per_delivered` and the
                 Sales Order status on its own.
  * Completed  : requires POD (configurable). Closes each Sales Order once every
                 trip against it is done and qty is fully delivered.

Status is never changed through `doc.save()`. It is written with `db_set` from
`set_trip_status`, because `status` is a standard read-only field and a plain
save on a submitted document raises UpdateAfterSubmitError.
"""

import frappe
from frappe import _
from frappe.query_builder.functions import Sum
from frappe.utils import flt

from erpnext.stock.doctype.delivery_trip.delivery_trip import DeliveryTrip

from thameen_erp.overrides.vehicle import ensure_vehicle_masters

LIFECYCLE = (
	"Scheduled",
	"Loading",
	"In Transit",
	"Delivered",
	"POD Pending",
	"Completed",
)

NEXT_STATUS = {
	"Scheduled": "Loading",
	"Loading": "In Transit",
	"In Transit": "Delivered",
	"Delivered": "POD Pending",
	"POD Pending": "Completed",
}

TERMINAL_STATES = ("Completed", "Cancelled")

# Trips in these states no longer reserve Sales Order qty — the Delivery Note
# already moved that qty onto `Sales Order Item.delivered_qty`.
CONSUMED_STATES = ("Delivered", "POD Pending", "Completed", "Cancelled")

QTY_TOLERANCE = 0.001


class ThameenDeliveryTrip(DeliveryTrip):
	# ------------------------------------------------------------------
	# Validation
	# ------------------------------------------------------------------

	def validate(self):
		self._migrate_legacy_single_item()
		self._recompute()
		self._validate_trip_items()
		self._validate_trip_qty()
		self._validate_vehicle_capacity()
		self._validate_transport_type()

		# ERPNext: requires a driver to submit, resolves stop addresses.
		super().validate()

	def before_update_after_submit(self):
		"""Frappe does NOT run `validate` on a post-submit save — only this hook.
		Everything derived from the rows has to be recomputed here as well, or the
		POD flag and the odometer distance silently stop updating."""
		self._guard_planned_rows()
		self._recompute()

	def _recompute(self):
		"""Derived values. Safe to run before or after submission."""
		self._set_trip_item_defaults()
		self._set_transportation_item()
		self._set_distance()
		self._set_pod_flag()
		self._sync_header_summary()

	def _set_transportation_item(self):
		"""Resolve the freight charge item, falling back to the company default.

		Resolved here rather than at invoice time so that changing the default in
		Thameen Fleet Settings never re-codes trips that were already delivered
		but not yet billed.
		"""
		if not self.get("custom_transportation_item"):
			self.custom_transportation_item = frappe.db.get_single_value(
				"Thameen Fleet Settings", "transportation_item"
			)

		if self.get("custom_transportation_item"):
			if frappe.get_cached_value("Item", self.custom_transportation_item, "is_stock_item"):
				frappe.throw(
					_("{0} is a stock item and cannot be used as a transportation charge.").format(
						self.custom_transportation_item
					)
				)
		elif flt(self.get("custom_transportation_cost")):
			frappe.throw(
				_(
					"Set a Transportation Charge Item — either on this trip or in Thameen Fleet Settings."
				)
			)

	def _guard_planned_rows(self):
		"""Rows may be re-measured after submit, never re-planned."""
		before = self.get_doc_before_save()
		if not before:
			return

		old = {row.name: row for row in (before.get("custom_trip_items") or [])}
		new = {row.name: row for row in (self.get("custom_trip_items") or [])}

		if set(old) != set(new):
			frappe.throw(
				_("Trip rows cannot be added or removed after submission. Cancel and amend the trip instead.")
			)

		for name, row in new.items():
			if flt(row.qty) != flt(old[name].qty):
				frappe.throw(
					_("Row {0}: Planned Qty cannot be changed after submission. Adjust Delivered Qty instead.").format(
						row.idx
					)
				)
			if flt(row.delivered_qty) > flt(row.qty) + QTY_TOLERANCE:
				frappe.throw(
					_("Row {0}: Delivered Qty {1} cannot exceed Planned Qty {2}.").format(
						row.idx, flt(row.delivered_qty), flt(row.qty)
					)
				)

	def _migrate_legacy_single_item(self):
		"""Trips created before the child table existed still carry the old
		single-item fields. Fold them into a row so one code path serves both."""
		if self.get("custom_trip_items"):
			return
		if not (self.get("custom_item") and flt(self.get("custom_planned_qty"))):
			return

		self.append(
			"custom_trip_items",
			{
				"sales_order": self.get("custom_sales_order"),
				"so_detail": _find_so_detail(self.get("custom_sales_order"), self.custom_item),
				"item_code": self.custom_item,
				"qty": flt(self.custom_planned_qty),
				"delivered_qty": flt(self.get("custom_delivered_qty")),
				"source_warehouse": self.get("custom_loading_warehouse"),
			},
		)

	def _set_trip_item_defaults(self):
		header_location = self.get("custom_delivery_location")

		for row in self.get("custom_trip_items") or []:
			if row.item_code and not (row.item_name and row.stock_uom):
				item = frappe.get_cached_value(
					"Item", row.item_code, ["item_name", "stock_uom"], as_dict=True
				)
				if item:
					row.item_name = row.item_name or item.item_name
					row.stock_uom = row.stock_uom or item.stock_uom

			row.uom = row.uom or row.stock_uom
			row.conversion_factor = flt(row.conversion_factor) or 1.0

			if row.so_detail and not (row.rate and row.delivery_location):
				so_item = frappe.db.get_value(
					"Sales Order Item",
					row.so_detail,
					["rate", "warehouse", "custom_delivery_location"],
					as_dict=True,
				)
				if so_item:
					row.rate = flt(row.rate) or flt(so_item.rate)
					row.source_warehouse = row.source_warehouse or so_item.warehouse
					row.delivery_location = row.delivery_location or so_item.custom_delivery_location

			row.source_warehouse = row.source_warehouse or self.get("custom_loading_warehouse")
			row.delivery_location = row.delivery_location or header_location
			row.amount = flt(row.qty) * flt(row.rate)

	def _validate_trip_items(self):
		rows = self.get("custom_trip_items") or []

		if self._action == "submit":
			# `vehicle` is deliberately optional at draft so a trip can be planned
			# before dispatch picks a truck. It becomes mandatory to submit.
			if not rows:
				frappe.throw(_("Add at least one row to Trip Items before submitting."))
			if not self.get("vehicle"):
				frappe.throw(_("Assign a Vehicle before submitting the trip."))

		labels = {(row.delivery_location or "").strip() for row in rows if row.delivery_location}
		if len({label.lower() for label in labels}) > 1:
			frappe.throw(
				_("A single trip cannot serve more than one delivery location: {0}.").format(
					", ".join(sorted(labels))
				)
			)

		for row in rows:
			if flt(row.qty) <= 0:
				frappe.throw(_("Row {0}: Planned Qty must be greater than zero.").format(row.idx))

	def _set_distance(self):
		start = flt(self.get("custom_starting_odometer"))
		end = flt(self.get("custom_ending_odometer"))
		if start and end:
			if end < start:
				frappe.throw(_("Ending Odometer cannot be less than Starting Odometer."))
			self.total_distance = end - start

	def _validate_trip_qty(self):
		"""A trip may never plan more than the Sales Order still owes."""
		rows = [row for row in (self.get("custom_trip_items") or []) if row.so_detail]
		if not rows:
			return

		keys = list({row.so_detail for row in rows})

		ordered = {
			d.name: d
			for d in frappe.get_all(
				"Sales Order Item",
				filters={"name": ("in", keys)},
				fields=["name", "item_code", "parent", "qty", "delivered_qty"],
			)
		}

		planned_elsewhere = _planned_qty_on_other_trips(keys, self.name)

		for row in rows:
			so_item = ordered.get(row.so_detail)
			if not so_item:
				continue

			balance = flt(so_item.qty) - flt(so_item.delivered_qty) - flt(planned_elsewhere.get(row.so_detail))
			if flt(row.qty) > balance + QTY_TOLERANCE:
				frappe.throw(
					_(
						"Row {0} ({1}): planned {2} but only {3} is left on {4}. "
						"Ordered {5}, already delivered {6}, already on other trips {7}."
					).format(
						row.idx,
						row.item_code,
						flt(row.qty),
						balance,
						so_item.parent,
						flt(so_item.qty),
						flt(so_item.delivered_qty),
						flt(planned_elsewhere.get(row.so_detail)),
					)
				)

	def _validate_vehicle_capacity(self):
		if not self.get("vehicle"):
			return
		total = sum(flt(row.qty) for row in (self.get("custom_trip_items") or []))
		if not total:
			return
		capacity = flt(frappe.get_cached_value("Vehicle", self.vehicle, "custom_capacity"))
		if capacity and total > capacity:
			frappe.msgprint(
				_("Total planned qty {0} exceeds the rated capacity {1} of vehicle {2}.").format(
					total, capacity, self.vehicle
				),
				indicator="orange",
				title=_("Over Capacity"),
			)

	def _set_pod_flag(self):
		rows = [row for row in (self.get("custom_pod_documents") or []) if row.get("attachment")]
		self.custom_pod_received = 1 if rows else 0

		if self.status == "Completed" and _pod_required() and not self.custom_pod_received:
			frappe.throw(_("Attach at least one Proof of Delivery document before completing this trip."))

	def _validate_transport_type(self):
		if self.get("custom_trip_type") == "External Transport" and not self.get(
			"custom_external_transporter"
		):
			frappe.throw(_("Select the External Transporter for an external transport trip."))

	def _sync_header_summary(self):
		"""Keep the legacy header fields readable for old reports and list views."""
		rows = self.get("custom_trip_items") or []
		if not rows:
			return

		self.custom_planned_qty = sum(flt(row.qty) for row in rows)
		self.custom_delivered_qty = sum(flt(row.delivered_qty) for row in rows)

		orders = {row.sales_order for row in rows if row.sales_order}
		if len(orders) == 1:
			self.custom_sales_order = orders.pop()

		items = {row.item_code for row in rows if row.item_code}
		self.custom_item = items.pop() if len(items) == 1 else None

		if not self.get("custom_delivery_location"):
			self.custom_delivery_location = next(
				(row.delivery_location for row in rows if row.delivery_location), None
			)

	# ------------------------------------------------------------------
	# Status machine — replaces the standard stop-based one
	# ------------------------------------------------------------------

	def update_status(self):
		"""ERPNext derives status from `delivery_stops[].visited`. This app drives
		it from the cement lifecycle instead, so the standard derivation is
		replaced rather than fought with."""
		if self.docstatus == 0:
			status = "Draft"
		elif self.docstatus == 2:
			status = "Cancelled"
		else:
			status = self.status if self.status in LIFECYCLE else "Scheduled"

		if self.status != status:
			self.db_set("status", status)

	def update_delivery_notes(self, delete=False):
		"""Standard method msgprints even when there are no stops. Delivery Notes
		in this app are linked through `custom_delivery_trip`, not through stops."""
		if not [stop for stop in (self.delivery_stops or []) if stop.delivery_note]:
			return
		super().update_delivery_notes(delete=delete)

	# ------------------------------------------------------------------
	# Submit / cancel
	# ------------------------------------------------------------------

	def on_submit(self):
		super().on_submit()

		if self.get("vehicle"):
			_ensure_masters(self.vehicle)
			frappe.db.set_value(
				"Vehicle", self.vehicle, "custom_status", "On Trip", update_modified=False
			)

	def on_cancel(self):
		super().on_cancel()

		if self.get("vehicle"):
			frappe.db.set_value(
				"Vehicle", self.vehicle, "custom_status", "Available", update_modified=False
			)

	# ------------------------------------------------------------------
	# Stage side effects
	# ------------------------------------------------------------------

	def load_vehicle(self):
		"""Material Transfer: loading warehouse(s) -> vehicle warehouse."""
		rows = [row for row in (self.get("custom_trip_items") or []) if flt(row.qty)]
		if not rows:
			return

		if not self.get("vehicle"):
			frappe.throw(_("Set a Vehicle before loading."))

		vehicle_warehouse = frappe.db.get_value("Vehicle", self.vehicle, "custom_vehicle_warehouse")
		if not vehicle_warehouse:
			frappe.throw(
				_("Vehicle {0} has no vehicle warehouse. Re-save the Vehicle to create it.").format(self.vehicle)
			)

		if frappe.db.exists(
			"Stock Entry",
			{"custom_delivery_trip": self.name, "stock_entry_type": "Material Transfer", "docstatus": 1},
		):
			return

		missing = [
			row.idx for row in rows if not (row.source_warehouse or self.get("custom_loading_warehouse"))
		]
		if missing:
			frappe.throw(
				_("Set a Loading Warehouse on the trip, or on row(s) {0}.").format(
					", ".join(str(idx) for idx in missing)
				)
			)

		se = frappe.new_doc("Stock Entry")
		se.stock_entry_type = "Material Transfer"
		se.company = self.company
		se.custom_delivery_trip = self.name
		se.custom_vehicle = self.vehicle

		for row in rows:
			se.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": flt(row.qty),
					"uom": row.uom or row.stock_uom,
					"conversion_factor": flt(row.conversion_factor) or 1,
					"s_warehouse": row.source_warehouse or self.custom_loading_warehouse,
					"t_warehouse": vehicle_warehouse,
					"cost_center": self.get("custom_cost_center"),
				},
			)

		se.flags.ignore_permissions = True
		se.insert()
		se.submit()

		frappe.msgprint(
			_("Loaded {0} row(s) onto {1} via {2}").format(len(rows), self.vehicle, se.name),
			indicator="green",
			alert=True,
		)

	@staticmethod
	def _freight_weights(by_order):
		"""Weight each Sales Order's share of the trip freight by delivered value.

		Falls back to qty when no rates were captured on the trip rows, and the
		caller falls back to an even split when both come out at zero.
		"""
		weight = {}
		for sales_order, order_rows in by_order.items():
			weight[sales_order] = sum(
				(flt(row.delivered_qty) or flt(row.qty)) * flt(row.rate) for row in order_rows
			)

		total = sum(weight.values())
		if total:
			return weight, total

		for sales_order, order_rows in by_order.items():
			weight[sales_order] = sum(flt(row.delivered_qty) or flt(row.qty) for row in order_rows)

		return weight, sum(weight.values())

	@staticmethod
	def _absorb_freight_rounding(created, total_freight):
		"""Push any rounding remainder onto the last note so the split reconciles."""
		if not created or not total_freight:
			return

		booked = sum(
			flt(amount)
			for amount in frappe.get_all(
				"Delivery Note",
				filters={"name": ("in", created)},
				pluck="custom_transportation_amount",
			)
		)
		drift = flt(total_freight - booked, 2)
		if not drift:
			return

		last = created[-1]
		current = flt(frappe.db.get_value("Delivery Note", last, "custom_transportation_amount"))
		frappe.db.set_value(
			"Delivery Note",
			last,
			"custom_transportation_amount",
			flt(current + drift, 2),
			update_modified=False,
		)

	def create_delivery_notes(self):
		"""One Delivery Note per Sales Order on the trip, from the vehicle warehouse."""
		rows = [row for row in (self.get("custom_trip_items") or []) if row.sales_order]
		if not rows:
			return []

		if frappe.db.exists("Delivery Note", {"custom_delivery_trip": self.name, "docstatus": ("<", 2)}):
			return []

		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		vehicle_warehouse = (
			frappe.db.get_value("Vehicle", self.vehicle, "custom_vehicle_warehouse")
			if self.get("vehicle")
			else None
		)

		by_order = {}
		for row in rows:
			by_order.setdefault(row.sales_order, []).append(row)

		# The trip's freight belongs to the whole truckload. When one trip serves
		# several Sales Orders it has to be SPLIT across the resulting Delivery
		# Notes — stamping the full amount on each would bill the customer twice,
		# because make_consolidated_invoice sums custom_transportation_amount.
		total_freight = flt(self.get("custom_transportation_cost"))
		freight_item = self.get("custom_transportation_item")
		order_weight, weight_total = self._freight_weights(by_order)

		created = []

		for sales_order, order_rows in by_order.items():
			wanted = {row.so_detail: row for row in order_rows if row.so_detail}
			if not wanted:
				frappe.throw(
					_(
						"Rows against {0} have no Sales Order Item reference. Recreate the trip from the Sales Order."
					).format(sales_order)
				)

			dn = make_delivery_note(sales_order)
			dn.custom_delivery_trip = self.name
			dn.custom_vehicle = self.get("vehicle")
			dn.custom_driver_link = self.get("driver")
			share = (order_weight.get(sales_order, 0) / weight_total) if weight_total else 0
			dn.custom_transportation_amount = flt(total_freight * share, 2)
			dn.custom_transportation_item = freight_item

			kept = []
			for dn_row in dn.items:
				trip_row = wanted.get(dn_row.so_detail)
				if not trip_row:
					continue
				dn_row.qty = flt(trip_row.delivered_qty) or flt(trip_row.qty)
				dn_row.warehouse = vehicle_warehouse or dn_row.warehouse
				dn_row.cost_center = self.get("custom_cost_center") or dn_row.cost_center
				if dn_row.meta.has_field("custom_delivery_trip"):
					dn_row.custom_delivery_trip = self.name
				kept.append(dn_row)

			if not kept:
				frappe.throw(
					_("None of the trip rows are still pending on Sales Order {0}.").format(sales_order)
				)

			dn.items = kept
			for idx, dn_row in enumerate(dn.items, start=1):
				dn_row.idx = idx

			dn.flags.ignore_permissions = True
			dn.insert()
			dn.submit()
			created.append(dn.name)

			for dn_row in dn.items:
				trip_row = wanted.get(dn_row.so_detail)
				if not trip_row:
					continue
				frappe.db.set_value(
					"Delivery Trip Item",
					trip_row.name,
					{
						"delivered_qty": flt(dn_row.qty),
						"delivery_note": dn.name,
						"delivery_note_item": dn_row.name,
					},
					update_modified=False,
				)

		self._absorb_freight_rounding(created, total_freight)

		self.reload()
		self.db_set(
			"custom_delivered_qty",
			sum(flt(row.delivered_qty) for row in (self.get("custom_trip_items") or [])),
			update_modified=False,
		)

		if created:
			frappe.msgprint(
				_("Delivery Note(s) {0} created from trip {1}").format(", ".join(created), self.name),
				indicator="green",
				alert=True,
			)

		return created

	def close_trip(self):
		if self.get("vehicle"):
			frappe.db.set_value(
				"Vehicle",
				self.vehicle,
				{
					"custom_status": "Available",
					"last_odometer": flt(self.get("custom_ending_odometer"))
					or frappe.db.get_value("Vehicle", self.vehicle, "last_odometer"),
				},
				update_modified=False,
			)

		orders = {row.sales_order for row in (self.get("custom_trip_items") or []) if row.sales_order}
		if self.get("custom_sales_order"):
			orders.add(self.custom_sales_order)

		for sales_order in orders:
			close_sales_order_if_complete(sales_order)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pod_required():
	return bool(frappe.db.get_single_value("Thameen Fleet Settings", "require_pod_before_complete"))


def _ensure_masters(vehicle):
	warehouse = frappe.db.get_value("Vehicle", vehicle, "custom_vehicle_warehouse")
	if not warehouse:
		ensure_vehicle_masters(frappe.get_doc("Vehicle", vehicle))


def _find_so_detail(sales_order, item_code):
	if not (sales_order and item_code):
		return None
	return frappe.db.get_value(
		"Sales Order Item", {"parent": sales_order, "item_code": item_code}, "name"
	)


def _planned_qty_on_other_trips(so_details, exclude_trip=None):
	"""Qty already committed on other trips that have not yet raised a Delivery Note."""
	if not so_details:
		return {}

	trip = frappe.qb.DocType("Delivery Trip")
	item = frappe.qb.DocType("Delivery Trip Item")

	conditions = (
		item.so_detail.isin(so_details)
		& (item.parenttype == "Delivery Trip")
		& (trip.docstatus < 2)
		& trip.status.notin(CONSUMED_STATES)
	)
	if exclude_trip:
		conditions = conditions & (trip.name != exclude_trip)

	query = (
		frappe.qb.from_(item)
		.join(trip)
		.on(item.parent == trip.name)
		.select(item.so_detail, Sum(item.qty).as_("qty"))
		.where(conditions)
		.groupby(item.so_detail)
	)

	return {row.so_detail: flt(row.qty) for row in query.run(as_dict=True)}


def close_sales_order_if_complete(sales_order):
	"""Close the SO only when every trip is done and delivery is 100%."""
	if not frappe.db.get_single_value("Thameen Fleet Settings", "auto_close_sales_order"):
		return

	so = frappe.get_doc("Sales Order", sales_order)
	if so.docstatus != 1 or so.status in ("Closed", "Cancelled"):
		return

	trip = frappe.qb.DocType("Delivery Trip")
	item = frappe.qb.DocType("Delivery Trip Item")

	open_trips = (
		frappe.qb.from_(item)
		.join(trip)
		.on(item.parent == trip.name)
		.select(trip.name)
		.where(
			(item.sales_order == sales_order)
			& (trip.docstatus == 1)
			& trip.status.notin(TERMINAL_STATES)
		)
		.limit(1)
		.run()
	)
	if open_trips:
		return

	legacy_open = frappe.db.count(
		"Delivery Trip",
		{
			"custom_sales_order": sales_order,
			"docstatus": 1,
			"status": ("not in", TERMINAL_STATES),
		},
	)
	if legacy_open:
		return

	if flt(so.per_delivered) < 99.99:
		return

	so.flags.ignore_permissions = True
	so.update_status("To Bill")

	frappe.msgprint(
		_("Sales Order {0} fully delivered and closed.").format(sales_order),
		indicator="green",
		alert=True,
	)


# ---------------------------------------------------------------------------
# Whitelisted API used by the form buttons
# ---------------------------------------------------------------------------


@frappe.whitelist()
def set_trip_status(trip, status):
	"""Advance a submitted trip.

	`status` is a standard read-only field, so it is written with `db_set`.
	Saving the document instead would raise UpdateAfterSubmitError.
	"""
	doc = frappe.get_doc("Delivery Trip", trip)
	frappe.has_permission("Delivery Trip", "write", doc=doc, throw=True)

	if doc.docstatus != 1:
		frappe.throw(_("Submit the trip before changing its status."))

	current = doc.status if doc.status in LIFECYCLE else "Scheduled"
	if status != NEXT_STATUS.get(current):
		frappe.throw(
			_("{0} cannot move straight to {1}. The next step is {2}.").format(
				current, status, NEXT_STATUS.get(current) or _("none — the trip is finished")
			)
		)

	if status == "Loading":
		doc.load_vehicle()
	elif status == "Delivered":
		doc.create_delivery_notes()
	elif status == "Completed":
		if _pod_required() and not doc.get("custom_pod_received"):
			frappe.throw(_("Attach at least one Proof of Delivery document before completing this trip."))

	doc.db_set("status", status)

	if status == "Completed":
		doc.reload()
		doc.close_trip()

	return status
