import frappe
from frappe import _
from frappe.model.document import Document


class PurchaseExpenseCenter(Document):
	def on_update(self):
		from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.mapping_sync import (
			sync_mappings,
		)
		from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
			sync_tax_withholding_category_to_supplier,
		)

		sync_mappings(self)
		sync_tax_withholding_category_to_supplier(self)


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

	settings = get_settings()
	return resolve_tax_withholding_category(category, settings["company"], posting_date)


@frappe.whitelist()
def create_purchase_invoice(expense_center):
	from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.invoice_creation import (
		create_purchase_invoice_from_expense_center,
	)

	return create_purchase_invoice_from_expense_center(expense_center)


@frappe.whitelist()
def retry_extraction(expense_center):
	from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.tasks import (
		process_expense_center,
	)

	doc = frappe.get_doc("Purchase Expense Center", expense_center)
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
