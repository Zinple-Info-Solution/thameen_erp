import frappe
from thameen_erp.install import create_roles, create_workflow, install_customisations


def execute():
	install_customisations()
	create_roles()
	create_workflow()
	frappe.db.commit()
