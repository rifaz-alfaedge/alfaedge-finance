"""GST-based supplier matching and new-supplier creation.

GSTIN is the unique identifier. An invoice with no GSTIN, or one that doesn't match
any existing Supplier, is not an error - it's routed to manual review as a "New"
supplier row.
"""

import frappe
from frappe.utils import getdate


def resolve_by_gst(gst_number: str | None) -> str | None:
	"""Return the name of an existing Supplier whose gstin matches, or None."""
	if not gst_number:
		return None
	return frappe.db.get_value("Supplier", {"gstin": gst_number}, "name")


def resolve_tax_withholding_category(category: str, company: str, reference_date=None) -> dict | None:
	"""Resolve a Tax Withholding Category to a {rate, account} pair the same way
	ERPNext's own automatic TDS would - the currently-effective rate (by
	reference_date) and this company's configured account. Returns None if
	nothing in it applies yet (no dated rate, no account for this company).
	"""
	if not category:
		return None

	reference_date = getdate(reference_date) if reference_date else getdate()
	category_doc = frappe.get_cached_doc("Tax Withholding Category", category)

	rate = None
	for row in category_doc.rates:
		if row.from_date and row.to_date and getdate(row.from_date) <= reference_date <= getdate(row.to_date):
			rate = row.tax_withholding_rate
			break
	if rate is None:
		return None

	account = None
	for row in category_doc.accounts:
		if row.company == company:
			account = row.account
			break
	if not account:
		return None

	return {"category": category, "rate": rate, "account": account}


def get_tds_from_supplier(supplier: str, company: str, reference_date=None) -> dict | None:
	"""If the matched Supplier has a Tax Withholding Category set, resolve it via
	resolve_tax_withholding_category(). Returns None if the supplier has none.
	"""
	category = frappe.db.get_value("Supplier", supplier, "tax_withholding_category")
	if not category:
		return None
	return resolve_tax_withholding_category(category, company, reference_date)


def sync_tax_withholding_category_to_supplier(doc):
	"""When a reviewer sets/changes the Tax Withholding Category on a Purchase
	Expense Center for an Existing Supplier, remember it on the Supplier itself so
	future invoices from them apply TDS the standard ERPNext way (apply_tds)
	without needing to pick it again here.

	Skipped for a Supplier flagged exclude_from_auto_tds - that flag means this
	app must never turn on ERPNext's automatic TDS for them (their invoices don't
	cross ERPNext's own threshold, so automatic TDS would silently not apply; TDS
	is instead deducted manually via the tds_rate/tds_account/tds_amount fields
	regardless of any Tax Withholding Category picked here for calculation).
	"""
	if not (doc.tds_category and doc.supplier_type == "Existing" and doc.existing_supplier):
		return

	supplier = frappe.db.get_value(
		"Supplier",
		doc.existing_supplier,
		["tax_withholding_category", "exclude_from_auto_tds"],
		as_dict=True,
	)
	if not supplier or supplier.exclude_from_auto_tds:
		return

	if supplier.tax_withholding_category != doc.tds_category:
		frappe.db.set_value("Supplier", doc.existing_supplier, "tax_withholding_category", doc.tds_category)


def supplier_uses_automatic_tds(supplier: str) -> bool:
	"""True if this Supplier is set up for ERPNext's own automatic TDS (a Tax
	Withholding Category is set and it isn't flagged to bypass automatic TDS) -
	the signal invoice_creation uses to decide whether to let ERPNext compute the
	withholding tax itself (apply_tds) instead of appending our own manual row.
	"""
	supplier_doc = frappe.db.get_value(
		"Supplier", supplier, ["tax_withholding_category", "exclude_from_auto_tds"], as_dict=True
	)
	return bool(supplier_doc and supplier_doc.tax_withholding_category and not supplier_doc.exclude_from_auto_tds)


def _safe_gstin(gst_number: str | None) -> str | None:
	"""Drop a GSTIN that fails checksum validation instead of letting Supplier
	creation crash on it. LLM extraction from a scanned/rendered PDF occasionally
	misreads a character (seen in practice: AABUC read for AABCU) - that's a data
	quality issue for the reviewer to fix on the Supplier later, not a reason to
	block invoice creation entirely.
	"""
	if not gst_number:
		return None
	try:
		from india_compliance.gst_india.utils import validate_gstin

		return validate_gstin(gst_number)
	except Exception:
		frappe.log_error(
			title="Purchase Invoice Automation: extracted GSTIN failed validation",
			message=f"GSTIN {gst_number!r} was dropped when creating the Supplier; please verify manually.",
		)
		return None


def create_supplier_and_address(expense_center) -> str:
	"""Create a Supplier (+ best-effort Address) from a New-supplier expense center row.

	Address creation is isolated in its own try/except: a failure there must not
	block Supplier or Purchase Invoice creation.
	"""
	supplier = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": expense_center.new_supplier,
			"supplier_group": frappe.db.get_single_value("Buying Settings", "supplier_group")
			or "All Supplier Groups",
			"supplier_type": "Company",
			"gstin": _safe_gstin(expense_center.supplier_gst),
		}
	)
	supplier.insert(ignore_permissions=True)

	try:
		if any([expense_center.address, expense_center.city, expense_center.state]):
			address = frappe.get_doc(
				{
					"doctype": "Address",
					"address_title": supplier.supplier_name,
					"address_type": "Billing",
					"address_line1": expense_center.address or supplier.supplier_name,
					"city": expense_center.city or "",
					"state": expense_center.state or "",
					"pincode": expense_center.postal_code or "",
					"country": "India",
					"links": [{"link_doctype": "Supplier", "link_name": supplier.name}],
				}
			)
			address.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title="Purchase Invoice Automation: Address creation failed",
			message=frappe.get_traceback(),
		)

	return supplier.name
