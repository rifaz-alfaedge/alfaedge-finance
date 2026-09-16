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
	supplier_uses_automatic_tds,
)

GRAND_TOTAL_TOLERANCE = 1.0


def _validate_mappings(expense_center, use_native_tds):
	missing_items = [row.idx for row in expense_center.items if not row.mapped_item]
	missing_taxes = [row.idx for row in expense_center.taxes if not row.mapped_account]

	errors = []
	if missing_items:
		errors.append(_("Item row(s) {0} are missing a Mapped Item.").format(", ".join(map(str, missing_items))))
	if missing_taxes:
		errors.append(
			_("Tax row(s) {0} are missing a Mapped Account.").format(", ".join(map(str, missing_taxes)))
		)
	# Native apply_tds sources its own account from the Supplier's Tax Withholding
	# Category and computes its own amount - only the manual-override path needs
	# these filled in by us.
	if expense_center.is_tds_applicable and not use_native_tds and not (
		expense_center.tds_account and expense_center.tds_amount
	):
		errors.append(_("TDS is marked applicable but is missing a TDS Account or TDS Amount."))
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

	if expense_center.purchase_invoice:
		frappe.throw(_("Invoice has already been created for this record."))

	supplier = _resolve_supplier(expense_center)
	settings = get_settings()
	# True for suppliers set up for ERPNext's own automatic TDS (a Tax Withholding
	# Category on the Supplier, and not flagged to bypass it) - false for suppliers
	# like OVH whose invoices never cross ERPNext's own threshold, where TDS is
	# deducted manually via the tds_rate/tds_account/tds_amount fields instead.
	use_native_tds = supplier_uses_automatic_tds(supplier)

	_validate_mappings(expense_center, use_native_tds)

	pi = frappe.new_doc("Purchase Invoice")
	pi.company = settings["company"]
	pi.supplier = supplier
	pi.bill_no = expense_center.supplier_invoice_number
	pi.bill_date = expense_center.supplier_invoice_date
	if expense_center.supplier_invoice_date:
		pi.posting_date = expense_center.supplier_invoice_date
	pi.set_posting_time = 1
	pi.purchase_expense_center = expense_center.name

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

	if use_native_tds:
		# Let ERPNext compute and append its own withholding-tax row (same
		# accounts_controller.set_tax_withholding() mechanism as if a user had
		# checked "Apply Tax Withholding Amount" by hand) - it sources the account
		# from the Tax Withholding Category itself and applies its own threshold
		# logic (which may decide not to deduct anything this time), so we don't
		# append anything ourselves here. Independent of is_tds_applicable, which
		# is our own signal for the manual-override path below.
		pi.apply_tds = 1
		pi.tax_withholding_category = frappe.db.get_value("Supplier", supplier, "tax_withholding_category")
	elif expense_center.is_tds_applicable:
		# Manual override path (e.g. OVH): this supplier's invoices never cross
		# ERPNext's own threshold, so native apply_tds would silently deduct
		# nothing. Append the same row shape
		# (tax_withholding_category.get_tax_row_for_tds) ourselves, using the
		# rate/account/amount already reviewed on the Purchase Expense Center - a
		# fixed amount deducted from the amount payable, not a GST account, so
		# india_compliance's per-item GST breakup requirement (see the comment
		# above) doesn't apply to this row.
		pi.append(
			"taxes",
			{
				"charge_type": "Actual",
				"category": "Total",
				"add_deduct_tax": "Deduct",
				"account_head": expense_center.tds_account,
				"tax_amount": expense_center.tds_amount,
				"description": f"TDS ({expense_center.tds_category or ''}) @ {expense_center.tds_rate}%".strip(),
			},
		)

	pi.insert(ignore_permissions=True)

	if expense_center.pdf_attachment:
		try:
			frappe.get_doc(
				{
					"doctype": "File",
					"file_url": expense_center.pdf_attachment,
					"file_name": expense_center.pdf_attachment.rsplit("/", 1)[-1],
					"attached_to_doctype": "Purchase Invoice",
					"attached_to_name": pi.name,
					"is_private": 1,
				}
			).insert(ignore_permissions=True)
		except Exception:
			frappe.log_error(
				title="Purchase Invoice Automation: could not attach source PDF to invoice",
				message=frappe.get_traceback(),
			)

	warning = None
	if expense_center.extracted_grand_total:
		# extracted_grand_total is the invoice's own printed total, before any TDS
		# deduction (TDS is withheld at payment time, it isn't part of what the
		# supplier billed) - add back what TDS subtracted so the comparison is
		# apples-to-apples.
		if use_native_tds:
			# Trust what ERPNext actually deducted (threshold-dependent - could be
			# less than our own rough estimate, or nothing at all) over our
			# pre-computed tds_amount.
			tds_deducted = sum(
				row.tax_amount or 0 for row in pi.taxes if row.category == "Total" and row.add_deduct_tax == "Deduct"
			)
		else:
			tds_deducted = expense_center.tds_amount or 0 if expense_center.is_tds_applicable else 0
		comparable_total = pi.grand_total + tds_deducted
		diff = abs(comparable_total - expense_center.extracted_grand_total)
		if diff > GRAND_TOTAL_TOLERANCE:
			warning = _(
				"Calculated grand total ({0}) differs from the invoice's extracted grand total "
				"({1}) by more than {2} - please double-check items and taxes before submitting."
			).format(comparable_total, expense_center.extracted_grand_total, GRAND_TOTAL_TOLERANCE)

	expense_center.purchase_invoice = pi.name
	expense_center.invoice_status = "Invoice Draft"
	expense_center.save(ignore_permissions=True)

	return {"purchase_invoice": pi.name, "warning": warning}


def sync_invoice_status(purchase_invoice_doc, method=None):
	"""Keep Purchase Expense Center.invoice_status in step with its Purchase
	Invoice's docstatus - called via doc_events on Purchase Invoice on_submit/on_cancel.
	"""
	expense_center = purchase_invoice_doc.get("purchase_expense_center")
	if not expense_center or not frappe.db.exists("Purchase Expense Center", expense_center):
		return

	new_status = "Invoice Submitted" if purchase_invoice_doc.docstatus == 1 else "Invoice Draft"
	frappe.db.set_value("Purchase Expense Center", expense_center, "invoice_status", new_status)


def unlink_from_expense_center(purchase_invoice_doc, method=None):
	"""Break the back-reference when a Purchase Invoice is deleted, so the source
	Purchase Expense Center goes back to "Pending Review" and can create a new
	invoice - called via doc_events on Purchase Invoice on_trash.

	Deleting the Purchase Invoice itself is allowed even while this link exists
	(see hooks.py's ignore_links_on_delete) - it's deleting the Purchase Expense
	Center while a Purchase Invoice still links to it that stays blocked.
	"""
	expense_center = purchase_invoice_doc.get("purchase_expense_center")
	if not expense_center or not frappe.db.exists("Purchase Expense Center", expense_center):
		return

	frappe.db.set_value(
		"Purchase Expense Center",
		expense_center,
		{"purchase_invoice": None, "invoice_status": "Pending Review"},
	)
