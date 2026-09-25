"""Shared intake entry point and the background extraction job.

Both the email webhook and the manual PDF upload path call intake_pdf() so the
extraction/mapping/duplicate-detection logic is written exactly once.
"""

import frappe
from frappe.utils import flt, getdate

from alfaedge_finance.alfaedge_finance.doctype.purchase_invoice_automation_settings.purchase_invoice_automation_settings import (
	get_settings,
)
from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.extraction import extract_invoice_data
from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
	get_tds_from_supplier,
	looks_like_gstin,
	resolve_by_gst,
	resolve_by_name,
	resolve_by_name_and_address,
)
from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.text_normalization import (
	normalize_for_matching,
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
		# The intake PDF, not whichever file a reviewer attached to the record later.
		file_filters = {"attached_to_doctype": "Purchase Expense Center", "attached_to_name": doc.name}
		if doc.pdf_attachment:
			file_filters["file_url"] = doc.pdf_attachment
		file_doc = frappe.get_doc("File", file_filters)
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
	settings = get_settings()

	gst_number = (parsed.get("gst_number") or "").strip().upper() or None
	if gst_number and not looks_like_gstin(gst_number):
		# Not a real GSTIN shape - an overseas supplier has none at all, and the
		# model occasionally mislabels some other identifier (EIN, VAT number)
		# as gst_number despite being told not to. Treat as absent rather than
		# storing a bogus value in supplier_gst.
		gst_number = None

	our_own_gstin = frappe.db.get_value("Company", settings["company"], "gstin")
	if gst_number and our_own_gstin and gst_number == our_own_gstin.strip().upper():
		# The invoice's "Bill To" section prints our own GSTIN (some invoices
		# do, for the buyer's records) and it got extracted as the supplier's
		# instead - seen in practice. Our own company can never be its own
		# supplier, so this is unambiguously a misextraction regardless of what
		# the extraction prompt says.
		gst_number = None

	doc.supplier_invoice_date = _parse_date(parsed.get("invoice_date"))
	doc.supplier_invoice_number = parsed.get("invoice_number") or ""
	doc.supplier_gst = gst_number or ""

	supplier_name = parsed.get("supplier_name")
	existing_supplier = (
		resolve_by_gst(gst_number)
		or resolve_by_name(supplier_name)
		or resolve_by_name_and_address(
			supplier_name,
			country=parsed.get("country"),
			state=parsed.get("state"),
			postal_code=parsed.get("postal_code"),
		)
	)
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
		doc.country = parsed.get("country") or "India"

	currency = (parsed.get("currency") or "").strip().upper()
	if not currency or not frappe.db.exists("Currency", currency):
		# Not extracted, or not a real currency code - fall back to the
		# Company's own default rather than leaving the field blank.
		currency = frappe.db.get_value("Company", settings["company"], "default_currency")
	doc.currency = currency

	doc.items = []
	for item in parsed.get("items") or []:
		qty = flt(item.get("qty"))
		rate = flt(item.get("rate"))
		amount = flt(item.get("amount")) or qty * rate

		if amount == 0:
			# Zero-amount lines show up as free samples, informational notes, or
			# extraction noise - they don't need a mapping or a place on the invoice.
			continue

		full_text = item.get("item_name") or ""
		truncated = full_text[:ITEM_NAME_MAX_LENGTH]
		mapped_item = frappe.db.get_value(
			"Purchase Item Mapping",
			{"supplier_item_description": normalize_for_matching(truncated)},
			"mapped_item",
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
				"rate": flt(tax.get("rate")),
				"amount": flt(tax.get("amount")),
				"mapped_account": mapped_account or None,
			},
		)

	doc.extracted_taxable_amount = flt(parsed.get("taxable_amount")) or None
	doc.extracted_grand_total = flt(parsed.get("grand_total")) or None

	tds_rate = parsed.get("tds_rate")
	tds_amount = parsed.get("tds_amount")
	if tds_rate or tds_amount:
		# The invoice itself states a TDS deduction - trust that over any generic
		# default configured on the Supplier master.
		doc.is_tds_applicable = 1
		doc.tds_rate = flt(tds_rate) or None
		doc.tds_amount = flt(tds_amount) or None
		doc.tds_account = settings["default_tds_account"]
	elif doc.supplier_type == "Existing" and doc.existing_supplier:
		# No TDS printed on the invoice - fall back to the Supplier's own Tax
		# Withholding Category, if one is configured (mirrors ERPNext's own
		# automatic TDS, for suppliers whose per-invoice amount never crosses the
		# threshold that would otherwise trigger it).
		tds = get_tds_from_supplier(doc.existing_supplier, settings["company"], doc.supplier_invoice_date)
		if tds:
			doc.is_tds_applicable = 1
			doc.tds_category = tds["category"]
			doc.tds_rate = tds["rate"]
			doc.tds_account = tds["account"]
			if doc.extracted_taxable_amount:
				doc.tds_amount = round(doc.extracted_taxable_amount * tds["rate"] / 100, 2)
	# else: leave TDS fields untouched - the reviewer picks a TDS Category by hand
	# when neither the invoice nor the Supplier master states a deduction.


def _parse_date(value):
	"""The model is told YYYY-MM-DD but occasionally returns "15/04/2026" or prose -
	leave the date for the reviewer rather than failing the whole extraction."""
	if not value:
		return None
	try:
		return getdate(value)
	except Exception:
		return None


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
	if not duplicate and doc.supplier_type == "Existing" and doc.existing_supplier:
		# Also an invoice keyed in by hand in ERPNext, without going through this app.
		duplicate = frappe.db.exists(
			"Purchase Invoice",
			{
				"supplier": doc.existing_supplier,
				"bill_no": doc.supplier_invoice_number,
				"docstatus": ["<", 2],
			},
		)
	doc.is_potential_duplicate = 1 if duplicate else 0
