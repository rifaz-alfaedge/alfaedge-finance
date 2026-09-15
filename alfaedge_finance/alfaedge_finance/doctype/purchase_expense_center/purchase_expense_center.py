import frappe
from frappe import _
from frappe.model.document import Document


class PurchaseExpenseCenter(Document):
	pass


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
	)
	return {"queued": True}
