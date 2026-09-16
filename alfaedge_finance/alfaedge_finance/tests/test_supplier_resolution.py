import frappe
from frappe.tests.utils import FrappeTestCase

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.supplier_resolution import (
	get_tds_from_supplier,
	resolve_by_gst,
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
