"""GST-based, and as a fallback name/address-based, supplier matching, plus
new-supplier creation.

Three tiers, in order, before giving up and routing to manual review as a "New"
supplier row:
1. GSTIN (resolve_by_gst) - the primary identifier for domestic suppliers.
2. Exact supplier name (resolve_by_name) - case/whitespace-insensitive - for a known
   supplier with no usable GSTIN (overseas, or the LLM mislabelled some other ID as
   gst_number).
3. Normalized name + address (resolve_by_name_and_address) - name with punctuation
   stripped too (catches "Anthropic, PBC" vs "Anthropic PBC"), confirmed against the
   Supplier's own linked Address (country/state/city/pincode) when the loose name match
   isn't unique on its own. Deliberately requires an address signal to agree before
   trusting a non-exact name match - a name-only fuzzy match risks silently linking to
   the wrong Supplier.
"""

import re

import frappe
from frappe.utils import getdate

_GSTIN_SHAPE_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]{3}$")
_PUNCTUATION_RE = re.compile(r"[.,;:'\"()\[\]&/\\-]")
_WHITESPACE_RE = re.compile(r"\s+")


def looks_like_gstin(value: str | None) -> bool:
	"""Structural check only (2-digit state code + 10-char PAN + 3 trailing
	alphanumeric chars for entity code/checksum, 15 total) - not a checksum
	validation. Used to catch an LLM mislabeling some other identifier (EIN, VAT
	number) as gst_number, which resolve_by_gst would otherwise just fail to
	match silently.
	"""
	return bool(value and _GSTIN_SHAPE_RE.match(value.strip().upper()))


def resolve_by_gst(gst_number: str | None) -> str | None:
	"""Return the name of an existing Supplier whose gstin matches, or None."""
	if not gst_number:
		return None
	return frappe.db.get_value("Supplier", {"gstin": gst_number}, "name")


def resolve_by_name(supplier_name: str | None) -> str | None:
	"""Fallback match for suppliers with no usable GSTIN (overseas, or an
	extraction that mislabeled some other ID as gst_number) - an exact,
	case/whitespace-insensitive match on Supplier.supplier_name. Deliberately not
	fuzzy: a near-miss is routed to manual review as New rather than risking a
	link to the wrong Supplier.
	"""
	if not supplier_name or not supplier_name.strip():
		return None
	result = frappe.db.sql(
		"select name from tabSupplier where lower(supplier_name) = lower(%s) limit 1",
		(supplier_name.strip(),),
	)
	return result[0][0] if result else None


def _normalize_name(name: str) -> str:
	name = _PUNCTUATION_RE.sub(" ", name)
	return _WHITESPACE_RE.sub(" ", name.strip()).lower()


def _supplier_address(supplier: str) -> dict | None:
	address_name = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Supplier", "link_name": supplier, "parenttype": "Address"},
		"parent",
	)
	if not address_name:
		return None
	return frappe.db.get_value(
		"Address", address_name, ["country", "state", "city", "pincode"], as_dict=True
	)


def _address_confirms(address: dict, country=None, state=None, postal_code=None) -> bool:
	if postal_code and address.pincode and _normalize_name(address.pincode) == _normalize_name(postal_code):
		return True
	if (
		country
		and state
		and address.country
		and address.state
		and _normalize_name(address.country) == _normalize_name(country)
		and _normalize_name(address.state) == _normalize_name(state)
	):
		return True
	return False


def resolve_by_name_and_address(
	supplier_name: str | None,
	country: str | None = None,
	state: str | None = None,
	city: str | None = None,
	postal_code: str | None = None,
) -> str | None:
	"""Broader fallback once exact name matching (resolve_by_name) has already
	failed. Two paths, both requiring some agreement between name and address so
	neither signal alone can produce a false match:

	1. Normalized name match (punctuation/case/whitespace stripped, so
	   "Anthropic, PBC" and "Anthropic PBC" are the same) - trusted on its own
	   when it identifies exactly one Supplier. If it matches more than one
	   (ambiguous), only trusted for the one whose linked Address also agrees on
	   pincode, or on country+state.
	2. If no Supplier's name normalizes to a match at all, fall back to address
	   alone: an exact pincode match, but only for a Supplier whose name at least
	   loosely overlaps (one normalized name contains the other) - a bare pincode
	   match with a completely unrelated name is not trusted (could be a
	   different company in the same building).
	"""
	if not supplier_name or not supplier_name.strip():
		return None

	target = _normalize_name(supplier_name)
	if not target:
		return None

	suppliers = frappe.get_all("Supplier", fields=["name", "supplier_name"])
	exact_name_candidates = [s.name for s in suppliers if _normalize_name(s.supplier_name) == target]

	if len(exact_name_candidates) == 1:
		return exact_name_candidates[0]

	if exact_name_candidates:
		# Ambiguous on name alone - only trust one that also agrees on address.
		for supplier in exact_name_candidates:
			address = _supplier_address(supplier)
			if address and _address_confirms(address, country, state, postal_code):
				return supplier
		return None

	if not postal_code:
		return None

	loose_candidates = [
		s.name
		for s in suppliers
		if (n := _normalize_name(s.supplier_name)) and (n in target or target in n)
	]
	for supplier in loose_candidates:
		address = _supplier_address(supplier)
		if address and address.pincode and _normalize_name(address.pincode) == _normalize_name(postal_code):
			return supplier
	return None


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


def find_payable_account_for_currency(company: str, currency: str) -> str | None:
	"""An existing, non-group Payable account under this company already
	denominated in the given currency - only if exactly one such account exists.
	Never creates a new account: which ledger account to use is a Chart-of-
	Accounts decision left to a human. This only reuses one that's already
	there, the same way Anthropic's pre-existing "Purchase Import" USD account
	already made that Supplier work without any code changes.
	"""
	accounts = frappe.get_all(
		"Account",
		filters={
			"company": company,
			"account_type": "Payable",
			"account_currency": currency,
			"is_group": 0,
		},
		pluck="name",
	)
	return accounts[0] if len(accounts) == 1 else None


def create_supplier_and_address(expense_center, company: str) -> str:
	"""Create a Supplier (+ best-effort Address) from a New-supplier expense center row.

	Address creation is isolated in its own try/except: a failure there must not
	block Supplier or Purchase Invoice creation.
	"""
	company_currency = frappe.db.get_value("Company", company, "default_currency")
	supplier_currency = (
		expense_center.currency if expense_center.currency and expense_center.currency != company_currency else None
	)

	supplier = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": expense_center.new_supplier,
			"supplier_group": frappe.db.get_single_value("Buying Settings", "supplier_group")
			or "All Supplier Groups",
			"supplier_type": "Company",
			"gstin": _safe_gstin(expense_center.supplier_gst),
			"default_currency": supplier_currency,
		}
	)
	supplier.insert(ignore_permissions=True)

	if supplier_currency:
		payable_account = find_payable_account_for_currency(company, supplier_currency)
		if payable_account:
			supplier.append("accounts", {"company": company, "account": payable_account})
			supplier.save(ignore_permissions=True)
		else:
			# Not a blocker for Supplier creation itself, but invoice creation
			# will still fail on the Payable-account-currency mismatch until a
			# {currency} Payable account is created and added to this Supplier's
			# Accounting > Default Accounts for this Company - a one-time setup
			# per new currency, not per supplier.
			frappe.log_error(
				title="Purchase Invoice Automation: no matching Payable account for new currency",
				message=(
					f"Supplier {supplier.name!r} was created with default_currency="
					f"{supplier_currency!r}, but no single unambiguous {company!r} Payable "
					f"account in that currency exists yet. Create one and add it to this "
					f"Supplier's Default Accounts before creating an invoice for them."
				),
			)

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
					"country": expense_center.country
					if expense_center.country and frappe.db.exists("Country", expense_center.country)
					else "India",
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
