# Thameen ERP

Cement distribution and fleet management layer for **Frappe v15 + ERPNext v15 + Frappe HR v15**.

## What this app does *not* rebuild

The single most important design decision: this app extends standard doctypes
rather than cloning them. Verified against `frappe/erpnext@version-15` and
`frappe/hrms@version-15`:

| Requirement | Already ships with | What we do |
|---|---|---|
| Vehicle master | `Vehicle` (ERPNext, Setup module) | Add fleet, accounting and status fields |
| Driver master, licence no. + expiry | `Driver` (ERPNext, Setup module) | Add `custom_assigned_vehicle` only — no `is_driver` flag on Employee |
| Trip sheet | `Delivery Trip` (ERPNext, Stock, submittable) | Add SO link, odometer, freight, POD table, extra statuses |
| Fuel / service log | `Vehicle Log` + `Vehicle Service` (HRMS, HR module) | Add maintenance type, workshop, labour cost, next service due |
| Stock deduction on delivery | `Delivery Note` | Raised automatically from the trip, from the vehicle warehouse |
| SO delivered qty / auto-close | `per_delivered` + `update_status` | Trip completion triggers closure when 100% delivered |
| Vehicle depreciation | `Asset` module | `Asset.custom_vehicle` link only; depreciation logic untouched |
| Supplier credit accounting | Return `Purchase Invoice` (`is_return=1`) | Credit note tracker optionally raises the debit note |
| Driver salary → vehicle cost center | `Salary Structure Assignment.payroll_cost_centers` | Synced when a driver is assigned, no Salary Slip override |

New doctypes are limited to the six that have no standard equivalent:
`Vehicle Document`, `Customer Requirement` (+ item), `Supplier Credit Note`
(+ item), `Trip POD Document`, `Thameen Fleet Settings`.

## Install

```bash
cd ~/frappe-bench
bench get-app https://github.com/<your-org>/thameen_erp.git
bench --site <site> install-app thameen_erp
bench --site <site> migrate
bench build --app thameen_erp
```

Requires `erpnext` and `hrms` on the site first.

## First-run setup

1. **Thameen Fleet Settings** — set the transportation charge item,
   Transportation Revenue account and Cement Sales Revenue account.
2. Create Vehicles. Each one auto-creates `CC-{plate}` under a `Fleet` cost
   center group, and a `{plate} - Vehicle` warehouse under a `Vehicles` group.
3. Create Drivers and link them to Employees and Vehicles.
4. Assign the roles: Fleet Manager, Operation Manager, Sales Approver,
   Finance Approver, Credit Note Officer.

## The flow

```
Customer Requirement ──workflow──▶ Sales Approver ──▶ Finance Approver ──▶ Approved
        │  (credit limit + outstanding shown on the form)
        ▼
   Sales Order  ◀── mapped from the approved requirement
        │
        ▼
   Delivery Trip (per truckload)
     Scheduled → Loading → In Transit → Delivered → POD Pending → Completed
        │           │                       │                        │
        │           │                       │                        └─ SO closed when
        │           │                       │                           100% delivered
        │           │                       └─ Delivery Note from vehicle warehouse
        │           └─ Stock Entry: loading warehouse → vehicle warehouse
        │
        ▼
   Month end: Sales Invoice → "Get Items From" → Consolidated Monthly Bill
     • cement lines keep their normal income account
     • one freight line per vehicle, posted to Transportation Revenue
       against that vehicle's cost center
```

## Vehicle profitability

Every cost and every riyal of freight revenue lands on the vehicle's cost
center, so the **Vehicle Profitability** report reads the GL directly and
agrees with the trial balance by construction:

- Direct trip costs: fuel, toll, loading/unloading (matched on account name)
- Periodic costs: driver salary (via payroll cost center), depreciation,
  insurance, maintenance
- Revenue: transportation revenue rows on the consolidated invoice

## Supplier credit note reconciliation

Purchase Order / Purchase Invoice lines carry an **expected discount**. The
invoice is paid at full value; when the supplier's credit note arrives, a
`Supplier Credit Note` reconciles it line by line:

| Condition | Status |
|---|---|
| received = expected | Fully Received |
| 0 < received < expected | Partially Received (stays open for a supplementary note) |
| received > expected | Received Above Expected — **blocked until a variance approver signs off** |

Multiple credit notes against one invoice line accumulate; each new note reads
what previous submitted notes already covered.

## Scheduled jobs

| Job | Frequency | Purpose |
|---|---|---|
| `notify_expiring_vehicle_documents` | daily | Refresh document status, alert on expiry window |
| `notify_service_due` | daily | Alert when service is due by date or odometer |
| `flag_overdue_credit_notes` | daily | Chase credit notes older than the configured threshold |
| `sync_vehicle_status` | hourly | Release vehicles left "On Trip" with no open trip |

## Reports

Vehicle Profitability · Vehicle Wise Cost Analysis · Trip Cost Report ·
Transportation Cost Analysis · Delivery Performance And POD Pending ·
Supplier Credit Note Variance · Customer Requirement Report

## Workspaces

- **Fleet Management** — nested under HR (`parent_page: HR`)
- **Sales & Delivery**
- **Purchase & Credit Notes**

Plus the **Fleet Dashboard** page (`/app/fleet-dashboard`): one server call,
hand-drawn SVG charts, CSS-variable colours so dark mode works.
