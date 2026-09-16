"""Shared intake entry point and the background extraction job.

Both the email webhook and the manual PDF upload path call intake_pdf() so the
extraction/mapping/duplicate-detection logic is written exactly once.
"""

import frappe

from alfaedge_finance.alfaedge_finance.doctype.purchase_invoice_automation_settings.purchase_invoice_automation_settings import (
	get_settings,
)
from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.extraction import extract_invoice_data
from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
	get_tds_from_supplier,
	resolve_by_gst,
)

ITEM_NAME_MAX_LENGTH = 140


def intake_pdf(
	pdf_bytes: bytes,
	filename: str,
	source: str,
	sender_email: str | None = None,
	subject: str | None = None,
	email_body: str | None = None,
) -> str:
	"""Create the Purchase Expense Center shell record and enqueue extraction.

	Returns immediately (fast) so the webhook/HTTP caller doesn't wait on the LLM.
	"""
	doc = frappe.get_doc(
		{
			"doctype": "Purchase Expense Center",
			"source": source,
			"status": "Pending",
			"sender_email": sender_email,
			"email_subject": subject,
			"raw_email_body": email_body,
		}
	)
	doc.insert(ignore_permissions=True)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"attached_to_doctype": "Purchase Expense Center",
			"attached_to_name": doc.name,
			"content": pdf_bytes,
			"is_private": 1,
		}
	)
	file_doc.insert(ignore_permissions=True)

	doc.pdf_attachment = file_doc.file_url
	doc.save(ignore_permissions=True)

	frappe.enqueue(
		process_expense_center,
		queue="long",
		timeout=180,
		expense_center=doc.name,
		# Without this, a fast worker can start the job before this request's
		# transaction commits, so the job's frappe.get_doc() finds nothing yet
		# (seen in practice with multi-file uploads in the same request).
		enqueue_after_commit=True,
	)
	return doc.name


def process_expense_center(expense_center: str):
	doc = frappe.get_doc("Purchase Expense Center", expense_center)
	doc.status = "Processing"
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	try:
		file_doc = frappe.get_doc("File", {"attached_to_doctype": "Purchase Expense Center", "attached_to_name": doc.name})
		pdf_bytes = file_doc.get_content()
		parsed = extract_invoice_data(pdf_bytes)
		_apply_extraction(doc, parsed)
		_flag_duplicates(doc)
		doc.status = "Extracted"
		doc.failure_reason = None
	except Exception:
		frappe.db.rollback()
		doc = frappe.get_doc("Purchase Expense Center", expense_center)
		doc.status = "Failed"
		doc.failure_reason = frappe.get_traceback()
		frappe.log_error(
			title="Purchase Invoice Automation: extraction failed",
			message=doc.failure_reason,
		)

	doc.save(ignore_permissions=True)
	frappe.db.commit()


def _apply_extraction(doc, parsed: dict):
	gst_number = (parsed.get("gst_number") or "").strip().upper() or None
	doc.supplier_invoice_date = parsed.get("invoice_date") or None
	doc.supplier_invoice_number = parsed.get("invoice_number") or ""
	doc.supplier_gst = gst_number or ""

	existing_supplier = resolve_by_gst(gst_number)
	if existing_supplier:
		doc.supplier_type = "Existing"
		doc.existing_supplier = existing_supplier
	else:
		doc.supplier_type = "New"
		doc.new_supplier = parsed.get("supplier_name") or ""
		doc.address = parsed.get("address") or ""
		doc.city = parsed.get("city") or ""
		doc.state = parsed.get("state") or ""
		doc.postal_code = parsed.get("postal_code") or ""

	settings = get_settings()
	doc.items = []
	for item in parsed.get("items") or []:
		qty = float(item.get("qty") or 0)
		rate = float(item.get("rate") or 0)
		amount = float(item.get("amount") or (qty * rate))

		if amount == 0:
			# Zero-amount lines show up as free samples, informational notes, or
			# extraction noise - they don't need a mapping or a place on the invoice.
			continue

		full_text = item.get("item_name") or ""
		truncated = full_text[:ITEM_NAME_MAX_LENGTH]
		mapped_item = frappe.db.get_value(
			"Purchase Item Mapping", {"supplier_item_description": truncated}, "mapped_item"
		)

		doc.append(
			"items",
			{
				"item_name": truncated,
				"description": full_text,
				"qty": qty,
				"rate": rate,
				"amount": amount,
				"uom": settings["default_uom"],
				"stock_qty": qty,
				"base_rate": rate,
				"base_amount": amount,
				"mapped_item": mapped_item or None,
			},
		)

	doc.taxes = []
	for tax in parsed.get("taxes") or []:
		tax_type = tax.get("tax_type") or "Other"
		mapped_account = frappe.db.get_value("Tax Account Mapping", {"tax_type": tax_type}, "mapped_account")
		doc.append(
			"taxes",
			{
				"tax_type": tax_type,
				"rate": float(tax.get("rate") or 0),
				"amount": float(tax.get("amount") or 0),
				"mapped_account": mapped_account or None,
			},
		)

	doc.extracted_taxable_amount = float(parsed.get("taxable_amount") or 0) or None
	doc.extracted_grand_total = float(parsed.get("grand_total") or 0) or None

	tds_rate = parsed.get("tds_rate")
	tds_amount = parsed.get("tds_amount")
	if tds_rate or tds_amount:
		# The invoice itself states a TDS deduction - trust that over any generic
		# default configured on the Supplier master.
		doc.is_tds_applicable = 1
		doc.tds_rate = float(tds_rate or 0) or None
		doc.tds_amount = float(tds_amount or 0) or None
		doc.tds_account = settings["default_tds_account"]
	elif doc.supplier_type == "Existing" and doc.existing_supplier:
		# No TDS printed on the invoice - fall back to the Supplier's own Tax
		# Withholding Category, if one is configured (mirrors ERPNext's own
		# automatic TDS, for suppliers whose per-invoice amount never crosses the
		# threshold that would otherwise trigger it).
		tds = get_tds_from_supplier(doc.existing_supplier, settings["company"], doc.supplier_invoice_date)
		if tds:
			doc.is_tds_applicable = 1
			doc.tds_rate = tds["rate"]
			doc.tds_account = tds["account"]
			if doc.extracted_taxable_amount:
				doc.tds_amount = round(doc.extracted_taxable_amount * tds["rate"] / 100, 2)
	# else: leave TDS fields untouched - the reviewer picks a TDS Category by hand
	# when neither the invoice nor the Supplier master states a deduction.


def _flag_duplicates(doc):
	"""Same supplier + same supplier_invoice_number arriving twice: flag, don't block."""
	if not doc.supplier_invoice_number:
		return

	supplier_filter = {}
	if doc.supplier_type == "Existing" and doc.existing_supplier:
		supplier_filter["existing_supplier"] = doc.existing_supplier
	elif doc.new_supplier:
		supplier_filter["new_supplier"] = doc.new_supplier
	else:
		return

	duplicate = frappe.db.get_value(
		"Purchase Expense Center",
		{
			"name": ["!=", doc.name],
			"supplier_invoice_number": doc.supplier_invoice_number,
			**supplier_filter,
		},
		"name",
	)
	doc.is_potential_duplicate = 1 if duplicate else 0
