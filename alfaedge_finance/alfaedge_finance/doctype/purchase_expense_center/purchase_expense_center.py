import frappe
from frappe import _
from frappe.model.document import Document


class PurchaseExpenseCenter(Document):
	def on_update(self):
		from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.mapping_sync import (
			sync_mappings,
		)

		sync_mappings(self)


@frappe.whitelist()
def get_supplier_tds(supplier, posting_date=None):
	"""Used by the form JS when a reviewer picks/changes the Existing Supplier by
	hand, so a Tax Withholding Category configured on that Supplier gets reflected
	the same way it would from automatic extraction.
	"""
	from alfaedge_finance.alfaedge_finance.doctype.purchase_invoice_automation_settings.purchase_invoice_automation_settings import (
		get_settings,
	)
	from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
		get_tds_from_supplier,
	)

	frappe.has_permission("Purchase Expense Center", "read", throw=True)
	settings = get_settings()
	return get_tds_from_supplier(supplier, settings["company"], posting_date)


@frappe.whitelist()
def get_tax_withholding_category_rate(category, posting_date=None):
	"""Used by the form JS when a reviewer picks a Tax Withholding Category by
	hand (the manual-fallback path), to pre-fill rate/account the same way as the
	automatic detection paths.
	"""
	from alfaedge_finance.alfaedge_finance.doctype.purchase_invoice_automation_settings.purchase_invoice_automation_settings import (
		get_settings,
	)
	from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
		resolve_tax_withholding_category,
	)

	frappe.has_permission("Purchase Expense Center", "read", throw=True)
	settings = get_settings()
	return resolve_tax_withholding_category(category, settings["company"], posting_date)


@frappe.whitelist()
def get_supplier_tds_default(supplier):
	"""The Supplier's own Tax Withholding Category, and whether it may be changed from
	here - the form asks before offering to save a newly picked category on it."""
	frappe.has_permission("Purchase Expense Center", "read", throw=True)
	return frappe.db.get_value(
		"Supplier", supplier, ["tax_withholding_category", "exclude_from_auto_tds"], as_dict=True
	)


@frappe.whitelist()
def set_supplier_tds_category(supplier, category):
	"""Save a Tax Withholding Category on the Supplier, after the reviewer confirmed it -
	ERPNext's automatic TDS then applies to every future invoice from them."""
	from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
		set_tax_withholding_category_on_supplier,
	)

	return set_tax_withholding_category_on_supplier(supplier, category)


@frappe.whitelist()
def create_purchase_invoice(expense_center):
	from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.invoice_creation import (
		create_purchase_invoice_from_expense_center,
	)

	frappe.get_doc("Purchase Expense Center", expense_center).check_permission("write")
	frappe.has_permission("Purchase Invoice", "create", throw=True)
	return create_purchase_invoice_from_expense_center(expense_center)


@frappe.whitelist()
def retry_extraction(expense_center):
	from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.tasks import (
		process_expense_center,
	)

	doc = frappe.get_doc("Purchase Expense Center", expense_center)
	doc.check_permission("write")
	if doc.status not in ("Failed", "Pending"):
		frappe.throw(_("Only Pending or Failed records can be retried."))
	doc.status = "Pending"
	doc.failure_reason = None
	doc.save(ignore_permissions=True)
	frappe.enqueue(
		process_expense_center,
		queue="long",
		timeout=180,
		expense_center=doc.name,
		enqueue_after_commit=True,
	)
	return {"queued": True}
