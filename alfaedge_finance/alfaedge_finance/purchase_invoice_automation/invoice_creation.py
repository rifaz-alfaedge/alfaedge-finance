"""Draft Purchase Invoice creation from a reviewed Purchase Expense Center.

The Purchase Invoice created here is always a Draft - this app never calls submit().
Human review and submission remain a manual step.
"""

import frappe
from frappe import _

from alfaedge_finance.alfaedge_finance.doctype.purchase_invoice_automation_settings.purchase_invoice_automation_settings import (
	get_settings,
)
from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
	create_supplier_and_address,
	resolve_by_gst,
)

GRAND_TOTAL_TOLERANCE = 1.0


def _validate_mappings(expense_center):
	missing_items = [row.idx for row in expense_center.items if not row.mapped_item]
	missing_taxes = [row.idx for row in expense_center.taxes if not row.mapped_account]

	errors = []
	if missing_items:
		errors.append(_("Item row(s) {0} are missing a Mapped Item.").format(", ".join(map(str, missing_items))))
	if missing_taxes:
		errors.append(
			_("Tax row(s) {0} are missing a Mapped Account.").format(", ".join(map(str, missing_taxes)))
		)
	if errors:
		frappe.throw("<br>".join(errors), title=_("Mapping Incomplete"))


def _resolve_supplier(expense_center):
	"""Re-check by GST (may have been created since extraction), else create it."""
	existing = resolve_by_gst(expense_center.supplier_gst)
	if existing:
		return existing
	if expense_center.supplier_type == "Existing" and expense_center.existing_supplier:
		return expense_center.existing_supplier
	return create_supplier_and_address(expense_center)


def create_purchase_invoice_from_expense_center(expense_center_name: str) -> dict:
	expense_center = frappe.get_doc("Purchase Expense Center", expense_center_name)

	if expense_center.invoice_status == "Invoice Created":
		frappe.throw(_("Invoice has already been created for this record."))

	_validate_mappings(expense_center)

	supplier = _resolve_supplier(expense_center)
	settings = get_settings()

	pi = frappe.new_doc("Purchase Invoice")
	pi.company = settings["company"]
	pi.supplier = supplier
	pi.bill_no = expense_center.supplier_invoice_number
	pi.bill_date = expense_center.supplier_invoice_date
	pi.set_posting_time = 1

	for row in expense_center.items:
		item_master_name = frappe.db.get_value("Item", row.mapped_item, "item_name") or row.mapped_item
		pi.append(
			"items",
			{
				"item_code": row.mapped_item,
				"item_name": item_master_name[:140],
				"description": row.description or row.item_name,
				"qty": row.qty,
				"rate": row.rate,
				"amount": row.amount,
				"uom": row.uom,
				"stock_qty": row.stock_qty or row.qty,
				"base_rate": row.base_rate or row.rate,
				"base_amount": row.base_amount or row.amount,
			},
		)

	for row in expense_center.taxes:
		# "Actual" (a fixed amount, ignoring rate) was the original design here - it would
		# have posted the exact amount printed on the invoice with no rounding risk. But
		# ERPNext forces tax.rate to None whenever charge_type is "Actual"
		# (erpnext.controllers.accounts_controller.validate_taxes_and_charges), which
		# leaves india_compliance unable to build a per-item tax-rate breakup and it
		# refuses to save GST-account tax rows in that state ("this would not compute item
		# taxes, and your further reporting will be affected"). So we use "On Net Total"
		# with the extracted rate instead, and let calculate_taxes_and_totals derive the
		# amount - the grand-total tolerance check below is the safety net that catches
		# any resulting drift from the invoice's printed total.
		pi.append(
			"taxes",
			{
				"charge_type": "On Net Total",
				"account_head": row.mapped_account,
				"rate": row.rate,
				"description": f"{row.tax_type} @ {row.rate}%",
			},
		)

	pi.insert(ignore_permissions=True)

	warning = None
	if expense_center.extracted_grand_total:
		diff = abs(pi.grand_total - expense_center.extracted_grand_total)
		if diff > GRAND_TOTAL_TOLERANCE:
			warning = _(
				"Calculated grand total ({0}) differs from the invoice's extracted grand total "
				"({1}) by more than {2} - please double-check items and taxes before submitting."
			).format(pi.grand_total, expense_center.extracted_grand_total, GRAND_TOTAL_TOLERANCE)

	expense_center.purchase_invoice = pi.name
	expense_center.invoice_status = "Invoice Created"
	expense_center.save(ignore_permissions=True)

	return {"purchase_invoice": pi.name, "warning": warning}
