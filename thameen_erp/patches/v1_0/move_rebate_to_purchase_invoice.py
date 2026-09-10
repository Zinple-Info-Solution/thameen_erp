"""Move the rebate feature from Sales Invoice to Purchase Invoice.

The rebate was originally built on the sales side. A rebate is something the
*supplier* grants us, so the fields and the whole calculation now live on
Purchase Invoice / Purchase Invoice Item.

This patch:
  1. Renames Rebate.customer -> Rebate.supplier and repoints Rebate.invoice
     at Purchase Invoice.
  2. Drops the Sales Invoice / Sales Invoice Item rebate custom fields (their
     definitions are already gone from the exported customizations, but the
     underlying columns and any stale Custom Field rows are cleaned up here so
     the change is not order-dependent).
  3. Creates the Purchase Invoice rebate fields by re-running install.

Any Rebate document created against a Sales Invoice cannot be repointed
automatically, so those are logged for manual review rather than deleted.
"""

import frappe

SALES_REBATE_FIELDS = {
	"Sales Invoice": ["custom_add_rebate"],
	"Sales Invoice Item": ["custom_rebate_percentage", "custom_rebate_amount"],
}


def execute():
	_migrate_rebate_doctype()
	_flag_orphan_rebates()
	_drop_sales_rebate_fields()
	_install_purchase_rebate_fields()


def _migrate_rebate_doctype():
	"""Carry customer values over to the new supplier column, then drop the old one."""
	if not frappe.db.exists("DocType", "Rebate"):
		return

	has_customer = frappe.db.has_column("Rebate", "customer")
	has_supplier = frappe.db.has_column("Rebate", "supplier")

	if has_customer and has_supplier:
		# Model sync already added `supplier`. Keep the old value visible so the
		# rows are identifiable during review, then retire the dead column.
		frappe.db.sql(
			"""UPDATE `tabRebate`
			   SET `supplier` = `customer`
			   WHERE (`supplier` IS NULL OR `supplier` = '')
			     AND `customer` IS NOT NULL AND `customer` != ''"""
		)
		frappe.db.sql("ALTER TABLE `tabRebate` DROP COLUMN `customer`")
	elif has_customer and not has_supplier:
		# Model sync has not run yet — rename in place.
		frappe.db.sql("ALTER TABLE `tabRebate` CHANGE `customer` `supplier` varchar(140)")

	frappe.db.delete("Custom Field", {"dt": "Rebate", "fieldname": "customer"})
	frappe.clear_cache(doctype="Rebate")


def _flag_orphan_rebates():
	"""Log Rebate rows still pointing at a Sales Invoice — they need manual review."""
	if not frappe.db.exists("DocType", "Rebate"):
		return

	names = [
		r.name
		for r in frappe.get_all("Rebate", fields=["name", "invoice"])
		if r.invoice and frappe.db.exists("Sales Invoice", r.invoice)
	]
	if not names:
		return

	frappe.log_error(
		"These Rebate documents were created against a Sales Invoice and now point "
		"at a Purchase Invoice link that will not resolve. Re-create them from the "
		"corresponding Purchase Invoice and cancel/delete these:\n\n" + "\n".join(names),
		"Thameen ERP: Rebates left on Sales Invoices",
	)


def _drop_sales_rebate_fields():
	for doctype, fieldnames in SALES_REBATE_FIELDS.items():
		if not frappe.db.exists("DocType", doctype):
			continue

		for fieldname in fieldnames:
			frappe.db.delete("Custom Field", {"dt": doctype, "fieldname": fieldname})

			table = f"tab{doctype}"
			try:
				if frappe.db.has_column(doctype, fieldname):
					frappe.db.sql(f"ALTER TABLE `{table}` DROP COLUMN `{fieldname}`")
			except Exception:
				# A missing column is the desired end state; anything else is
				# reported so the site is not left half-migrated silently.
				frappe.log_error(
					frappe.get_traceback(),
					f"Thameen ERP: could not drop {doctype}.{fieldname}",
				)

		frappe.clear_cache(doctype=doctype)

	frappe.db.commit()


def _install_purchase_rebate_fields():
	from thameen_erp.install import install_customisations

	install_customisations()
	frappe.clear_cache(doctype="Purchase Invoice")
	frappe.clear_cache(doctype="Purchase Invoice Item")
	frappe.db.commit()
