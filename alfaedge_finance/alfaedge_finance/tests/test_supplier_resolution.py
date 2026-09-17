import frappe
from frappe.tests.utils import FrappeTestCase

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
	get_tds_from_supplier,
	looks_like_gstin,
	resolve_by_gst,
	resolve_by_name,
	resolve_by_name_and_address,
)

TEST_GSTIN = "29AABCF8078M1C8"
COMPANY = "Code Dynamic Solutions Private Limited"
TEST_TDS_ACCOUNT = "TDS Payable - CDS"


class TestSupplierResolution(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Supplier", {"gstin": TEST_GSTIN}):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "Test PIA Supplier",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
					"gstin": TEST_GSTIN,
				}
			).insert(ignore_permissions=True)

	def test_matches_existing_supplier_by_gstin(self):
		self.assertEqual(resolve_by_gst(TEST_GSTIN), "Test PIA Supplier")

	def test_no_match_for_unknown_gstin(self):
		self.assertIsNone(resolve_by_gst("27ZZZZZ9999Z1Z1"))

	def test_no_gstin_is_not_an_error(self):
		self.assertIsNone(resolve_by_gst(None))
		self.assertIsNone(resolve_by_gst(""))


class TestResolveByName(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Supplier", {"supplier_name": "Anthropic Test Co"}):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "Anthropic Test Co",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
				}
			).insert(ignore_permissions=True)

	def test_exact_name_match(self):
		self.assertEqual(resolve_by_name("Anthropic Test Co"), "Anthropic Test Co")

	def test_name_match_ignores_case_and_surrounding_whitespace(self):
		self.assertEqual(resolve_by_name("  anthropic TEST co  "), "Anthropic Test Co")

	def test_no_match_for_unrelated_name(self):
		self.assertIsNone(resolve_by_name("Some Unrelated Supplier Inc"))

	def test_no_name_is_not_an_error(self):
		self.assertIsNone(resolve_by_name(None))
		self.assertIsNone(resolve_by_name(""))
		self.assertIsNone(resolve_by_name("   "))


class TestResolveByNameAndAddress(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		if not frappe.db.exists("Supplier", {"supplier_name": "Anthropic, PBC"}):
			cls.anthropic = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "Anthropic, PBC",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
				}
			).insert(ignore_permissions=True)
		else:
			cls.anthropic = frappe.get_doc("Supplier", {"supplier_name": "Anthropic, PBC"})

		if not frappe.db.exists(
			"Address", {"address_title": "Anthropic, PBC", "pincode": "94104"}
		):
			frappe.get_doc(
				{
					"doctype": "Address",
					"address_title": "Anthropic, PBC",
					"address_type": "Billing",
					"address_line1": "548 Market Street PMB 90375",
					"city": "San Francisco",
					"state": "California",
					"pincode": "94104",
					"country": "United States",
					"links": [{"link_doctype": "Supplier", "link_name": cls.anthropic.name}],
				}
			).insert(ignore_permissions=True)

		# A second, unrelated Supplier so pincode-only matching can't be trusted blindly.
		if not frappe.db.exists("Supplier", {"supplier_name": "PIA Unrelated Co"}):
			unrelated = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "PIA Unrelated Co",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
				}
			).insert(ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "Address",
					"address_title": "PIA Unrelated Co",
					"address_type": "Billing",
					"address_line1": "Somewhere else",
					"city": "San Francisco",
					"state": "California",
					"pincode": "94104",
					"country": "United States",
					"links": [{"link_doctype": "Supplier", "link_name": unrelated.name}],
				}
			).insert(ignore_permissions=True)

	def test_matches_on_normalized_name_alone_when_unique(self):
		# Punctuation-only difference from the stored "Anthropic, PBC".
		self.assertEqual(resolve_by_name_and_address("Anthropic PBC"), self.anthropic.name)

	def test_no_match_when_name_and_address_both_disagree(self):
		self.assertIsNone(
			resolve_by_name_and_address("Totally Unrelated Name", postal_code="00000")
		)

	def test_address_only_match_requires_loose_name_overlap(self):
		# Same pincode as Anthropic's address, but a name that shares no overlap
		# with any Supplier at all - must not match.
		self.assertIsNone(resolve_by_name_and_address("Zzz Totally Different Inc", postal_code="94104"))

	def test_loose_name_overlap_plus_pincode_match_succeeds(self):
		# "Anthropic" alone doesn't equal "Anthropic, PBC" after normalization,
		# but it's contained within it, and the pincode confirms it.
		self.assertEqual(
			resolve_by_name_and_address("Anthropic", postal_code="94104"), self.anthropic.name
		)

	def test_no_name_is_not_an_error(self):
		self.assertIsNone(resolve_by_name_and_address(None))
		self.assertIsNone(resolve_by_name_and_address(""))


class TestLooksLikeGstin(FrappeTestCase):
	def test_valid_shape(self):
		self.assertTrue(looks_like_gstin("29AABCF8078M1C8"))

	def test_rejects_overseas_style_identifier(self):
		# The actual garbage an LLM produced for an overseas supplier in practice.
		self.assertFalse(looks_like_gstin("9924USA29003OSI"))

	def test_rejects_none_and_blank(self):
		self.assertFalse(looks_like_gstin(None))
		self.assertFalse(looks_like_gstin(""))


class TestTdsFromSupplier(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		if not frappe.db.exists("Tax Withholding Category", "TEST TWC 194J"):
			frappe.get_doc(
				{
					"doctype": "Tax Withholding Category",
					"name": "TEST TWC 194J",
					"category_name": "TEST TWC 194J",
					"rates": [{"from_date": "2020-01-01", "to_date": "2099-12-31", "tax_withholding_rate": 2}],
					"accounts": [{"company": COMPANY, "account": TEST_TDS_ACCOUNT}],
				}
			).insert(ignore_permissions=True)

		cls.supplier_with_twc = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "PIA Supplier With TWC",
				"supplier_group": "All Supplier Groups",
				"supplier_type": "Company",
				"tax_withholding_category": "TEST TWC 194J",
			}
		).insert(ignore_permissions=True)

		cls.supplier_without_twc = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "PIA Supplier Without TWC",
				"supplier_group": "All Supplier Groups",
				"supplier_type": "Company",
			}
		).insert(ignore_permissions=True)

	def test_returns_rate_and_account_for_supplier_with_category(self):
		tds = get_tds_from_supplier(self.supplier_with_twc.name, COMPANY, "2026-06-01")
		self.assertEqual(tds["rate"], 2)
		self.assertEqual(tds["account"], TEST_TDS_ACCOUNT)
		self.assertEqual(tds["category"], "TEST TWC 194J")

	def test_none_for_supplier_without_category(self):
		self.assertIsNone(get_tds_from_supplier(self.supplier_without_twc.name, COMPANY, "2026-06-01"))

	def test_none_when_no_rate_covers_the_date(self):
		self.assertIsNone(get_tds_from_supplier(self.supplier_with_twc.name, COMPANY, "2010-01-01"))

	def test_none_when_no_account_for_company(self):
		self.assertIsNone(
			get_tds_from_supplier(self.supplier_with_twc.name, "Some Other Company", "2026-06-01")
		)
