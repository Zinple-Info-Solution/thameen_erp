"""Packing trip rows into truck-sized loads.

Used by the load check on the Delivery Trip form and by the split action it
offers. Kept separate from both so the arithmetic can be tested on its own.

    Site A   OPC-43  500 bags,  truck holds 200
      → load 1  OPC-43 200
        load 2  OPC-43 200
        load 3  OPC-43 100

Rows are cut, not just distributed: a single 500-bag line becomes three rows
rather than being rejected as indivisible. Cement is bulk, so that is the right
default. For an item where half a line is meaningless, add a Check field
`custom_indivisible` to Item and the row moves whole or not at all.
"""

import frappe
from frappe import _
from frappe.utils import flt

QTY_TOLERANCE = 0.001


def stock_qty(row):
	"""Row qty in stock UOM — the unit capacity is rated in."""
	return flt(row.get("qty")) * (flt(row.get("conversion_factor")) or 1)


def _indivisible(item_code):
	if not item_code or not frappe.get_meta("Item").has_field("custom_indivisible"):
		return False
	return bool(frappe.get_cached_value("Item", item_code, "custom_indivisible"))


def split_by_capacity(rows, capacity, first_capacity=None, one_item_per_load=False):
	"""Pack rows into loads of at most `capacity` (stock UOM).

	`first_capacity` lets the first load be smaller than the rest — that is the
	"this truck already has 180 on it, only 120 will fit, the rest goes on fresh
	trucks" case. Pass None to use `capacity` throughout.

	`one_item_per_load` keeps items apart: a bulk tanker cannot carry OPC-43 and
	OPC-53 in the same compartment, so each item is packed on its own and the
	loads are concatenated. The first load (the one that keeps the current
	truck) is always the first item's first load.

	Returns a list of row-lists. A capacity of 0, or a total that already fits,
	returns a single load, so callers can use this unconditionally.
	"""
	rows = [row for row in rows if stock_qty(row) > QTY_TOLERANCE]
	if not rows:
		return []

	if one_item_per_load:
		groups = {}
		for row in rows:
			groups.setdefault(row.get("item_code"), []).append(row)
		if len(groups) > 1:
			loads = []
			for index, group in enumerate(groups.values()):
				head = first_capacity if index == 0 else None
				loads.extend(split_by_capacity(group, capacity, first_capacity=head))
			return loads

	if capacity <= QTY_TOLERANCE:
		return [rows]

	head = capacity if first_capacity is None else max(flt(first_capacity), 0.0)
	total = sum(stock_qty(row) for row in rows)

	if total <= head + QTY_TOLERANCE:
		return [rows]

	loads = []
	current = []
	remaining = head

	# Heaviest first: keeps the load count near-minimal and stops a big line
	# being stranded behind a pile of small ones.
	for row in sorted(rows, key=stock_qty, reverse=True):
		qty_left = stock_qty(row)
		factor = flt(row.get("conversion_factor")) or 1

		if _indivisible(row.get("item_code")):
			if qty_left > capacity + QTY_TOLERANCE:
				frappe.throw(
					_("{0} needs {1} but is marked indivisible and the truck holds {2}.").format(
						row.get("item_code"), qty_left, capacity
					)
				)
			if qty_left > remaining + QTY_TOLERANCE:
				loads.append(current)
				current, remaining = [], capacity
			current.append(row)
			remaining -= qty_left
			continue

		while qty_left > QTY_TOLERANCE:
			if remaining <= QTY_TOLERANCE:
				loads.append(current)
				current, remaining = [], capacity

			take = min(qty_left, remaining)
			piece = frappe._dict(row.as_dict() if hasattr(row, "as_dict") else dict(row))
			piece.pop("name", None)
			piece.pop("parent", None)
			piece.pop("idx", None)
			piece.qty = take / factor
			piece.amount = flt(piece.qty) * flt(piece.get("rate"))
			current.append(piece)

			qty_left -= take
			remaining -= take

	if current:
		loads.append(current)

	return [load for load in loads if load]


def describe_loads(loads):
	"""Plain summary for the confirmation dialog."""
	return [
		{
			"load": index + 1,
			"total_qty": sum(stock_qty(row) for row in load),
			"items": [
				{
					"item_code": row.get("item_code"),
					"item_name": row.get("item_name"),
					"qty": flt(row.get("qty")),
					"uom": row.get("uom"),
				}
				for row in load
			],
		}
		for index, load in enumerate(loads)
	]


def _piece(row, stock_take):
	factor = flt(row.get("conversion_factor")) or 1
	piece = frappe._dict(row.as_dict() if hasattr(row, "as_dict") else dict(row))
	for key in ("name", "parent", "idx"):
		piece.pop(key, None)
	piece.qty = stock_take / factor
	piece.amount = flt(piece.qty) * flt(piece.get("rate"))
	return piece


def allocate_to_plan(rows, plan):
	"""Cut the trip's rows into the loads the dispatcher typed.

	`plan` = [{"items": [{"item_code", "qty"}]}, ...] with qty in STOCK UOM.
	Rows of each item are consumed first-come-first-served, so Sales Order
	line references survive the cut. Every unit must be placed: the plan's
	per-item totals must equal the trip's, or this throws with the figures.
	"""
	rows = [row for row in rows if stock_qty(row) > QTY_TOLERANCE]
	pool = {}
	for row in rows:
		pool.setdefault(row.get("item_code"), []).append([row, stock_qty(row)])

	wanted = {}
	for load in plan:
		for item in load.get("items") or []:
			wanted[item["item_code"]] = flt(wanted.get(item["item_code"])) + flt(item["qty"])
	have = {code: sum(left for _, left in entries) for code, entries in pool.items()}
	for code in set(wanted) | set(have):
		if abs(flt(wanted.get(code)) - flt(have.get(code))) > QTY_TOLERANCE:
			frappe.throw(
				_("{0}: the plan places {1} but the trip carries {2}. Every unit must be on exactly one trip.").format(
					code, flt(wanted.get(code), 3), flt(have.get(code), 3)
				),
				title=_("Plan does not add up"),
			)

	loads = []
	for load in plan:
		chunk = []
		for item in load.get("items") or []:
			need = flt(item["qty"])
			for entry in pool.get(item["item_code"], []):
				if need <= QTY_TOLERANCE:
					break
				row, left = entry
				if left <= QTY_TOLERANCE:
					continue
				take = min(left, need)
				chunk.append(_piece(row, take))
				entry[1] = left - take
				need -= take
		if chunk:
			loads.append(chunk)
	return loads
