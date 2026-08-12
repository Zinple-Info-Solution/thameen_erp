# Thameen ERP

Cement distribution and fleet management layer for **Frappe v15 + ERPNext v15 + Frappe HR v15**.

The product problem: a cement distributor sells a low-margin commodity and hauls
it on trucks it owns. Whether the business made money depends on whether *each
truck* made money, and that answer only exists if freight revenue and every
vehicle cost land on the same cost center in the general ledger. This app makes
that true by construction, and adds the operational documents the cement trade
needs and stock ERPNext does not have — trip sheets, PODs, supplier credit notes.

---

## Table of contents

1. [Design principle: extend, don't clone](#1-design-principle-extend-dont-clone)
2. [Install](#2-install)
3. [First-run setup](#3-first-run-setup)
4. [Thameen Fleet Settings, field by field](#4-thameen-fleet-settings-field-by-field)
5. [The operational flow](#5-the-operational-flow)
6. [Vehicle profitability: how the money lands](#6-vehicle-profitability-how-the-money-lands)
7. [Freight billing and the transportation charge item](#7-freight-billing-and-the-transportation-charge-item)
8. [Supplier credit note reconciliation](#8-supplier-credit-note-reconciliation)
9. [Doctypes and custom fields](#9-doctypes-and-custom-fields)
10. [Hooks, overrides and permissions](#10-hooks-overrides-and-permissions)
11. [Scheduled jobs and notifications](#11-scheduled-jobs-and-notifications)
12. [Reports and dashboard](#12-reports-and-dashboard)
13. [Roles](#13-roles)
14. [Upgrading an existing site](#14-upgrading-an-existing-site)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Design principle: extend, don't clone

The single most important decision in this codebase: it extends standard
doctypes rather than cloning them. Verified against `frappe/erpnext@version-15`
and `frappe/hrms@version-15`.

| Requirement | Already ships with | What this app does |
|---|---|---|
| Vehicle master | `Vehicle` (ERPNext, Setup) | Adds fleet, accounting and status fields |
| Driver master, licence + expiry | `Driver` (ERPNext, Setup) | Adds `custom_assigned_vehicle` only — no `is_driver` flag on Employee |
| Trip sheet | `Delivery Trip` (ERPNext, Stock, submittable) | Class override: SO-line item table, odometer, freight, POD table, cement lifecycle statuses |
| Fuel / service log | `Vehicle Log` + `Vehicle Service` (HRMS, HR) | Adds maintenance type, workshop, labour cost, next service due |
| Stock deduction on delivery | `Delivery Note` | Raised automatically from the trip, out of the vehicle warehouse |
| SO delivered qty / auto-close | `per_delivered` + `update_status` | Trip completion triggers closure at 100% delivered |
| Vehicle depreciation | `Asset` module | `Asset.custom_vehicle` link only; depreciation logic untouched |
| Supplier credit accounting | Return `Purchase Invoice` (`is_return=1`) | Credit note tracker optionally raises the debit note |
| Driver salary → vehicle cost center | `Salary Structure Assignment.payroll_cost_centers` | Synced when a driver is assigned; no Salary Slip override |

New doctypes are limited to the seven with no standard equivalent:
`Vehicle Document`, `Customer Requirement` (+ item), `Supplier Credit Note`
(+ item), `Delivery Trip Item`, `Trip POD Document`, `Thameen Fleet Settings`.

### Why Delivery Trip is a class override, not a doc_event

ERPNext's `DeliveryTrip.update_status()` derives status from
`delivery_stops[].visited` and runs on both `on_submit` and
`on_update_after_submit`. Controller methods run **before** app `doc_events`
hooks, so a hook can never stop it overwriting the cement lifecycle states —
"Loading", "Delivered" and "POD Pending" were all being reset to "Scheduled" on
every save. `override_doctype_class` is the only correct fix.

Two consequences worth internalising:

- `status` is written with `db_set` from `set_trip_status`, never through
  `doc.save()`. It is a standard read-only field, and a plain save on a submitted
  document raises `UpdateAfterSubmitError`.
- Frappe does **not** run `validate` on a post-submit save, only
  `before_update_after_submit`. Anything derived from the trip rows has to be
  recomputed in both, which is why `_recompute()` exists and is called from each.

---

## 2. Install

```bash
cd ~/frappe-bench
bench get-app https://github.com/<your-org>/thameen_erp.git
bench --site <site> install-app thameen_erp
bench --site <site> migrate
bench build --app thameen_erp
```

`erpnext` and `hrms` must be on the site first — they are declared in
`required_apps`, so install will refuse otherwise.

---

## 3. First-run setup

1. **Thameen Fleet Settings** — set the transportation charge item, the
   Transportation Revenue account and the Cement Sales Revenue account. See
   section 4 for what each one actually controls.
2. **Create Vehicles.** Each one auto-creates `CC-{plate}` under a `Fleet` cost
   center group, and a `{plate} - Vehicle` warehouse under a `Vehicles` group.
   This is what makes every later figure land in the right place, so do it
   before any transaction.
3. **Create Drivers** and link them to Employees and Vehicles. Enabling
   `custom_sync_payroll_cost_center` pushes the vehicle's cost center onto the
   driver's Salary Structure Assignment, so payroll posts driver cost to the
   truck with no Salary Slip override.
4. **Assign the roles:** Fleet Manager, Operation Manager, Sales Approver,
   Finance Approver, Credit Note Officer.
5. **Decide on auto-create trips** (default on). With it on, submitting an
   approved order plans one draft trip per delivery location. With it off, use
   **Sales Order → Create → Delivery Trips**.

---

## 4. Thameen Fleet Settings, field by field

`Thameen Fleet Settings` is a **Single** doctype — one row in `tabSingles`, no
list view. Nothing caches it. Every consumer reads it live with
`frappe.db.get_single_value` or `frappe.get_single`, so a change takes effect on
the next document created. **It never rewrites anything already posted.**

### Revenue Split

| Field | Read by | Effect |
|---|---|---|
| Transportation Charge Item | `delivery_trip._set_transportation_item`, `api._append_freight_lines` | The company-wide default freight item. Copied onto each trip at validate; a trip may override it. Validated as non-stock. |
| Transportation Revenue Account | `api._freight_income_account`, `vehicle_profitability.get_transport_income_accounts` | Fallback income account for freight rows, and part of the account set the profitability report uses to tell freight from goods. Validated as `root_type = Income`. |
| Cement Sales Revenue Account | `api.make_consolidated_invoice` | **Fallback only.** Used when a Delivery Note line has no `income_account` of its own — rare, since Item Defaults normally supply one. |
| Default Company | *nothing* | Reserved. `make_consolidated_invoice` falls back to the Delivery Note's company, then `frappe.defaults.get_user_default("Company")`. |

### Operations

| Field | Read by | Effect |
|---|---|---|
| Auto-create Delivery Trips on Sales Order submit | `overrides/sales_order.py` | Early-returns out of the submit hook when off |
| Auto-close Sales Order on full delivery | `overrides/delivery_trip.py` | Early-returns out of the closure routine when off |
| Require POD before completing a trip | `overrides/delivery_trip.py` | Blocks the `POD Pending → Completed` transition until a Trip POD Document is attached |
| Document Expiry Reminder (Days) | `tasks.notify_expiring_vehicle_documents` | Default reminder window, used when an individual Vehicle Document leaves `reminder_days` blank |
| Credit Note Overdue After (Days) | `tasks.flag_overdue_credit_notes` | Age cutoff for the overdue digest |

### Notifications

| Field | Read by | Effect |
|---|---|---|
| Notify Roles | `tasks._get_notify_users` | One role per line. Resolved to enabled users (Administrator excluded) who receive the daily digests. Falls back to Fleet Manager if blank. |

---

## 5. The operational flow

```
Customer Requirement ──workflow──▶ Sales Approver ──▶ Finance Approver ──▶ Approved
        │  (credit limit + outstanding shown on the form)
        ▼
   Sales Order  ◀── mapped from the approved requirement
        │   each item row carries its own Delivery Location
        │
        ▼
   Delivery Trips — planned automatically on submit, ONE PER LOCATION
        │
        │   SO-0001  ├─ OPC 43  200 → Site A ─┐
        │            ├─ OPC 53  120 → Site A ─┴─ Trip 1 (2 rows)
        │            └─ White    80 → Site B ──  Trip 2 (1 row)
        │
        │   Created in Draft with no vehicle — dispatch assigns the truck.
        ▼
   Delivery Trip (per truckload, multiple items, one site)
     Scheduled → Loading → In Transit → Delivered → POD Pending → Completed
        │           │                       │                        │
        │           │                       │                        └─ SO closed when
        │           │                       │                           100% delivered
        │           │                       └─ one Delivery Note per SO, from the
        │           │                          vehicle warehouse, matched on so_detail
        │           └─ one Stock Entry, all rows: loading → vehicle warehouse
        │
        ▼
   Month end: Sales Invoice → "Get Items From" → Consolidated Monthly Bill
     • cement lines keep their normal income account
     • one freight line per (vehicle, charge item), posted to Transportation
       Revenue against that vehicle's cost center
        │
        ▼
   Payment Entry — ordinary ERPNext. One invoice per customer per month means
   one receipt clears the month.
```

### What each lifecycle transition actually does

| Transition | Side effect |
|---|---|
| → Loading | One Material Transfer Stock Entry moves every trip row from its loading warehouse onto the vehicle warehouse ("truck stock") |
| → In Transit | Status only |
| → Delivered | One Delivery Note per Sales Order, from the vehicle warehouse, rows matched on `so_detail`. ERPNext then updates `per_delivered` on its own |
| → POD Pending | Status only |
| → Completed | Releases the vehicle to Available; closes each Sales Order once every trip against it is done and qty is fully delivered |

Trip rows may be **re-measured** after submit (`delivered_qty`) but never
**re-planned** — `_guard_planned_rows` throws if rows are added, removed, or have
their planned `qty` changed. Cancel and amend instead.

### Where revenue is and is not recognised

A common misreading. The Delivery Note posts **stock and COGS only** — no
receivable, no revenue. Revenue and the debit to Debtors appear only when the
consolidated Sales Invoice is submitted at month end. A trial balance taken
mid-month shows delivered inventory gone with no revenue against it; that is
correct and expected under this model.

---

## 6. Vehicle profitability: how the money lands

Every cost and every riyal of freight revenue is posted to the vehicle's cost
center, so the **Vehicle Profitability** report reads GL Entry directly and
agrees with the trial balance by construction rather than re-deriving figures
from transaction tables.

| Bucket | Sources | Mechanism |
|---|---|---|
| Transport revenue | Freight rows on the consolidated invoice | Cost center from `Vehicle.custom_cost_center`; account must be in the transport account set |
| Direct trip cost | Fuel, toll, loading/unloading, driver allowance | Matched on account **name** keywords, cost center from the vehicle |
| Periodic cost | Driver salary, depreciation, insurance, maintenance | Salary via payroll cost center; depreciation via `Asset.custom_vehicle`; maintenance via Vehicle Log |
| Goods revenue | Cement lines on the consolidated invoice | Reported separately — see below |

### The goods-revenue trap

Cement lines also carry the vehicle cost center: `create_delivery_notes` sets
`dn_row.cost_center` from the trip, and the consolidated invoice copies it
through. So **Income on a vehicle cost center is not all freight.**

`get_gl_totals` therefore splits Income into `revenue` (accounts in the transport
set) and `goods_revenue` (everything else), and only transport revenue feeds the
profit and margin columns. Goods revenue is shown as its own column so the split
is auditable rather than hidden.

The transport account set is built from the Settings default **plus** the Item
Default income account of every item actually used as a transportation charge —
so a dedicated "Long Haul Freight" item with its own revenue account is
classified correctly without any code change. If you would rather force all
freight to the single Settings account, drop the `Item Default` lookup in
`api._freight_income_account`; the report's set-based logic then collapses
harmlessly to one account.

### A caveat on cost classification

Direct-versus-periodic cost is decided by substring matching on the **account
name** (`fuel`, `toll`, `loading`, `unloading`, `freight`, `driver allowance`).
This is a pragmatic choice, not a robust one. If your chart of accounts names fuel
something else, that cost silently reclassifies as periodic. Total cost and profit
stay correct either way — only the split moves.

---

## 7. Freight billing and the transportation charge item

### Resolution order

1. `Delivery Trip.custom_transportation_item`, if set by dispatch or accounts.
2. Otherwise `Thameen Fleet Settings.transportation_item`.

Resolution happens at **trip validate**, not at invoice time, and the result is
stamped onto the Delivery Note. This matters: changing the default in Settings
next quarter will not silently re-code trips already delivered but not yet billed.

Stock items are rejected server-side (`_set_transportation_item` throws) and
filtered out of the client-side picker. If freight is entered with no item
resolvable from either source, the trip refuses to validate rather than quietly
dropping the charge at month end.

### Rate, not price list

The freight row is written as `qty = 1, rate = <summed freight>`. The item's own
Item Price is never consulted, so there is nothing to maintain there. What varies
per row is the **cost center**, which is the entire point.

### Apportioning across Delivery Notes

The trip's freight belongs to the whole truckload. When one trip serves several
Sales Orders it produces several Delivery Notes, and the freight is **split**
across them by delivered value (falling back to qty when trip rows carry no
rate). Any rounding remainder is pushed onto the last note so the apportioned
total reconciles to the trip exactly.

This split is mandatory, not cosmetic: `make_consolidated_invoice` **sums**
`custom_transportation_amount` across notes, so stamping the full trip freight on
each one would bill the customer once per Sales Order on the truck.

### Grouping on the invoice

Freight is accumulated per `(vehicle, charge item)` pair. A truck that ran under
two different charge items in one month produces two freight rows, each to its own
revenue account, both against that vehicle's cost center. Row count per vehicle
is therefore not guaranteed to be one.

---

## 8. Supplier credit note reconciliation

Cement suppliers commonly invoice at list price and issue a credit note for the
agreed discount later. Purchase Order and Purchase Invoice lines therefore carry
an **expected discount**; the invoice is paid at full value; and when the credit
note arrives a `Supplier Credit Note` reconciles it line by line.

| Condition | Status |
|---|---|
| received = expected | Fully Received |
| 0 < received < expected | Partially Received — stays open for a supplementary note |
| received > expected | Received Above Expected — **blocked until a variance approver signs off** |

Multiple credit notes against one invoice line accumulate; each new note reads
what previous submitted notes already covered (`get_previously_received`).

On submit, `make_debit_note()` optionally raises a return Purchase Invoice
(`is_return = 1`) for the **actual** credited amount. That sits as a credit
against the supplier, cleared in the next Payment Entry or by reconciliation.
`on_cancel` reverses the line-level figures.

---

## 9. Doctypes and custom fields

### New doctypes

| Doctype | Submittable | Purpose |
|---|---|---|
| `Thameen Fleet Settings` | — (Single) | Configuration |
| `Vehicle Document` | no | Registration, insurance, permits, with expiry tracking |
| `Customer Requirement` (+ Item) | yes | Pre-order demand capture with credit check and two-step approval |
| `Supplier Credit Note` (+ Item) | yes | Expected-versus-received discount reconciliation |
| `Delivery Trip Item` | child | The SO lines on a truckload |
| `Trip POD Document` | child | Proof-of-delivery attachments |

### Custom fields added to standard doctypes

All created in code by `install.py` via `create_custom_fields(..., update=True)`,
so `bench migrate` is idempotent and re-running repositions fields safely.

| Doctype | Fields |
|---|---|
| Vehicle | vehicle type, capacity + UOM, status, assigned driver, registration expiry, cost center, vehicle warehouse, asset link, auto-create-masters flag |
| Driver | assigned vehicle, sync payroll cost center |
| Vehicle Log | log type, workshop, labour cost, stock entry, total cost, next service due (date + odometer), cost center |
| Delivery Trip | sales order, delivery location, loading warehouse, trip type, external transporter, start/end odometer, transportation cost, **transportation item**, cost center, trip items table, POD table, POD received |
| Delivery Note | delivery trip, vehicle, driver, transportation amount, **transportation item** |
| Sales Order (+ Item) | customer requirement, delivery location |
| Sales Invoice (+ Item) | consolidated-run flag, billing from/to, delivery trip, vehicle, is-transportation-row |
| Purchase Order Item | expected discount amount, agreed net price |
| Purchase Invoice Item | expected discount, agreed net price, credit note status, credit note received, vehicle |
| Journal Entry Account / Expense Claim Detail | vehicle (drives cost center) |
| Asset | vehicle |
| Warehouse | is-vehicle-warehouse, linked vehicle |
| Stock Entry | delivery trip, vehicle |

---

## 10. Hooks, overrides and permissions

### Class override

`Delivery Trip` → `thameen_erp.overrides.delivery_trip.ThameenDeliveryTrip`.
Rationale in section 1.

### Document events

| Doctype | Events | Handler |
|---|---|---|
| Vehicle | validate, after_insert, on_update | `overrides/vehicle.py` — creates cost center and warehouse |
| Driver | on_update | `overrides/driver.py` — payroll cost center sync |
| Vehicle Log | validate, on_submit, on_cancel | `overrides/vehicle_log.py` |
| Sales Order | validate, on_submit | `overrides/sales_order.py` — plans trips per location |
| Delivery Note | validate, on_submit, on_cancel | `overrides/delivery_note.py` |
| Purchase Order | validate | expected-discount validation |
| Purchase Invoice | validate, on_submit | expected-discount validation, credit note flagging |
| Journal Entry, Expense Claim | validate | `fleet_expense.set_cost_center_from_vehicle` |
| Sales Invoice | validate | `overrides/sales_invoice.py` |

### Permissions

`Delivery Trip` has both a `permission_query_conditions` and a `has_permission`
hook in `permissions.py`. A user linked to a Driver record sees only their own
trips; everyone else is unrestricted. Both hooks are needed — the query condition
filters list views and reports, `has_permission` guards direct document access.

### Whitelisted API

| Method | Purpose |
|---|---|
| `api.get_billable_deliveries` | Unbilled Delivery Notes for a customer in a window |
| `api.make_consolidated_invoice` | Builds the monthly Sales Invoice |
| `api.get_available_vehicles` | Dispatch picker |
| `api.get_vehicle_stock` | Truck stock lookup |
| `api.check_stock_availability` | Pre-trip stock check |
| `customer_requirement.make_sales_order` | Maps an approved requirement |
| `supplier_credit_note.get_expected_lines` | Pulls invoice lines awaiting credit |

---

## 11. Scheduled jobs and notifications

| Job | Frequency | Purpose |
|---|---|---|
| `notify_expiring_vehicle_documents` | daily | Refreshes document status, digests everything expiring or expired |
| `notify_service_due` | daily | Digests vehicles due by date or odometer |
| `flag_overdue_credit_notes` | daily | Digests credit notes older than the configured threshold |
| `sync_vehicle_status` | hourly | Releases vehicles left "On Trip" with no open trip |

### Digest model

Each job sends **one** Notification Log per user containing an HTML table of
every due row — not one notification per document. A vehicle with four expiring
documents produces one line in one notification, not four notifications.

Because a Notification Log can only reference a single record, the digests leave
`document_type`/`document_name` empty and put a `get_link_to_form` link in each
table row instead; those are clickable both in the notification dropdown and in
the email.

Recipients are the enabled users holding any role in **Notify Roles**, resolved in
one query. Administrator is always excluded.

**Known limitation:** the digests re-fire daily for the same rows, so an unrenewed
document nags indefinitely. If that becomes noise, add a `last_notified_on` field
to Vehicle Document and skip rows notified within the last N days.

---

## 12. Reports and dashboard

| Report | Reads |
|---|---|
| Vehicle Profitability | GL Entry by vehicle cost center — see section 6 |
| Vehicle Wise Cost Analysis | GL Entry, cost breakdown per vehicle |
| Trip Cost Report | Delivery Trip, freight versus trip cost, contribution |
| Transportation Cost Analysis | Freight billed versus cost by month, vehicle or customer, with per-km and per-unit rates |
| Delivery Performance And POD Pending | On-time delivery and outstanding PODs |
| Supplier Credit Note Variance | Expected versus received discount |
| Customer Requirement Report | Pipeline with credit exposure |

Plus the **Fleet Dashboard** page (`/app/fleet-dashboard`): one server call,
hand-drawn SVG charts, CSS-variable colours so dark mode works.

Note that Trip Cost Report, Transportation Cost Analysis and the dashboard read
`Delivery Trip.custom_transportation_cost` — the *trip's* freight figure — while
Vehicle Profitability reads the GL. They answer slightly different questions: the
trip-level reports show freight as planned and charged operationally, the GL
report shows freight as invoiced and posted. They diverge for trips delivered but
not yet billed, and for any freight edited after invoicing.

---

## 13. Roles

Shipped as fixtures: **Fleet Manager**, **Operation Manager**, **Sales Approver**,
**Finance Approver**, **Credit Note Officer**.

Sales Approver and Finance Approver drive the Customer Requirement workflow.
Credit Note Officer owns Supplier Credit Note. Fleet Manager and Operation
Manager are the default notification recipients.

---

## 14. Upgrading an existing site

```bash
bench --site <site> backup --with-files
bench --site <site> migrate
bench build --app thameen_erp
```

Patches run in `post_model_sync` order:

| Patch | What it does |
|---|---|
| `install_customisations` | Re-runs the custom field and property setter install |
| `backfill_vehicle_cost_centers` | Creates missing `CC-{plate}` cost centers and vehicle warehouses |
| `backfill_trip_items` | Folds legacy single-item trips onto the `Delivery Trip Item` table. Idempotent, drops nothing — the legacy `custom_item` / `custom_planned_qty` fields survive as read-only summaries so existing reports keep working |
| `backfill_transportation_item` | Stamps the current default charge item onto historic trips and notes that carry freight. Skips itself if more than one item is already in use, so it cannot relabel history billed under an older item |

Correctness does not depend on the last one — `_append_freight_lines` falls back
to the Settings default when the field is blank.

**Back up first.** Every patch here is idempotent, but `migrate` on a production
cement site is not a thing to run hopefully.

---

## 15. Troubleshooting

**Trip status keeps resetting to Scheduled.** The class override is not loading.
Confirm `override_doctype_class` in `hooks.py`, then `bench --site <site>
clear-cache && bench restart`.

**"Vehicle X has no vehicle warehouse."** The Vehicle was created before this app
was installed, or `custom_auto_create_masters` was off. Re-save the Vehicle, or
run `backfill_vehicle_cost_centers`.

**Freight missing from the consolidated invoice.** Either the Delivery Note has no
`custom_vehicle` (freight is keyed on vehicle and is skipped without one), or no
charge item resolved. The invoice build raises an orange message naming the
vehicles it skipped rather than failing silently.

**Vehicle Profitability shows implausibly high revenue.** Check the Goods Revenue
column. If transport revenue looks inflated and goods revenue is near zero, your
freight item's income account is not in the transport account set — set the
account in Thameen Fleet Settings, or on the item's Item Default for that company.

**Freight double-counted on a customer's month.** Look for Delivery Notes from one
trip each carrying the full trip freight. That was the pre-apportioning
behaviour; notes created before this version are not retro-corrected. Fix the
affected notes' `custom_transportation_amount` before consolidating, or credit the
difference.

**Nothing appears in the daily notifications.** Notify Roles resolves to enabled
users only, and Administrator is always excluded. A site where Administrator is
the only Fleet Manager will send nothing.

**Trip refuses to submit: "cannot serve more than one delivery location."** By
design — one truckload goes to one site. Split the trip.

---

## Licence

MIT. See `license.txt`.
