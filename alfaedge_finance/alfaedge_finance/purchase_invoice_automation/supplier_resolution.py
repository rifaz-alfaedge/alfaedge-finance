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


def get_tds_from_supplier(supplier: str, company: str, reference_date=None) -> dict | None:
	"""If the matched Supplier has a Tax Withholding Category set, resolve it to a
	{rate, account} pair the same way ERPNext's own automatic TDS would - the
	currently-effective rate (by reference_date) and this company's configured
	account. Returns None if the supplier has no category, or nothing in it applies
	yet (no dated rate, no account for this company).
	"""
	category = frappe.db.get_value("Supplier", supplier, "tax_withholding_category")
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
