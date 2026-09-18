import frappe
from frappe.tests.utils import FrappeTestCase

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.tasks import _apply_extraction


class TestApplyExtractionCurrency(FrappeTestCase):
	def test_uses_extracted_currency_when_valid(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		_apply_extraction(doc, {"currency": "USD", "items": [], "taxes": []})
		self.assertEqual(doc.currency, "USD")

	def test_falls_back_to_company_currency_when_not_extracted(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		_apply_extraction(doc, {"currency": None, "items": [], "taxes": []})
		self.assertEqual(doc.currency, "INR")  # Code Dynamic Solutions Private Limited's default

	def test_falls_back_to_company_currency_for_invalid_code(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		_apply_extraction(doc, {"currency": "NOT_A_CURRENCY", "items": [], "taxes": []})
		self.assertEqual(doc.currency, "INR")


class TestApplyExtractionRejectsOwnGstin(FrappeTestCase):
	"""Regression test for a real extraction bug: an invoice's "Bill To" section
	printed our own company's GSTIN (some overseas suppliers capture it for
	their own records), and it got extracted as the supplier's GST number
	instead - our own company can never be its own supplier, so this must
	always be dropped regardless of what the LLM returns.
	"""

	def test_drops_gst_number_matching_our_own_company(self):
		our_gstin = frappe.db.get_value(
			"Company",
			frappe.get_single("Purchase Invoice Automation Settings").company,
			"gstin",
		)
		if not our_gstin:
			self.skipTest("Test Company has no GSTIN configured on this site")

		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		_apply_extraction(
			doc,
			{
				"supplier_name": "PIA Bill-To Confusion Supplier",
				"gst_number": our_gstin,
				"items": [],
				"taxes": [],
			},
		)

		self.assertEqual(doc.supplier_gst, "")
		self.assertEqual(doc.supplier_type, "New")  # nothing to match on with the GST dropped

	def test_drops_gst_number_matching_our_own_company_case_insensitively(self):
		our_gstin = frappe.db.get_value(
			"Company",
			frappe.get_single("Purchase Invoice Automation Settings").company,
			"gstin",
		)
		if not our_gstin:
			self.skipTest("Test Company has no GSTIN configured on this site")

		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		_apply_extraction(
			doc,
			{"supplier_name": "PIA Bill-To Confusion Supplier 2", "gst_number": our_gstin.lower(), "items": [], "taxes": []},
		)

		self.assertEqual(doc.supplier_gst, "")


class TestApplyExtraction(FrappeTestCase):
	def test_skips_zero_amount_line_items(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		parsed = {
			"items": [
				{"item_name": "Free Sample", "qty": 1, "rate": 0, "amount": 0},
				{"item_name": "Real Item", "qty": 2, "rate": 50, "amount": 100},
			],
			"taxes": [],
		}
		_apply_extraction(doc, parsed)

		self.assertEqual(len(doc.items), 1)
		self.assertEqual(doc.items[0].item_name, "Real Item")

	def test_skips_line_item_with_zero_qty_and_rate(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		parsed = {"items": [{"item_name": "Note", "qty": 0, "rate": 0}], "taxes": []}
		_apply_extraction(doc, parsed)

		self.assertEqual(len(doc.items), 0)


class TestApplyExtractionSupplierMatching(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Supplier", {"supplier_name": "PIA Overseas Test Co"}):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "PIA Overseas Test Co",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
					"country": "United States",
				}
			).insert(ignore_permissions=True)

	def test_falls_back_to_name_match_when_gst_number_is_not_a_real_gstin(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		parsed = {
			"supplier_name": "PIA Overseas Test Co",
			"gst_number": "9924USA29003OSI",  # the actual garbage seen in practice
			"items": [],
			"taxes": [],
		}
		_apply_extraction(doc, parsed)

		self.assertEqual(doc.supplier_type, "Existing")
		self.assertEqual(doc.existing_supplier, "PIA Overseas Test Co")
		self.assertEqual(doc.supplier_gst, "")  # garbage dropped, not stored

	def test_falls_back_to_name_match_when_gst_number_is_absent(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		parsed = {"supplier_name": "PIA Overseas Test Co", "gst_number": None, "items": [], "taxes": []}
		_apply_extraction(doc, parsed)

		self.assertEqual(doc.supplier_type, "Existing")
		self.assertEqual(doc.existing_supplier, "PIA Overseas Test Co")

	def test_new_supplier_captures_country(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		parsed = {
			"supplier_name": "PIA Brand New Overseas Co",
			"gst_number": None,
			"country": "United States",
			"items": [],
			"taxes": [],
		}
		_apply_extraction(doc, parsed)

		self.assertEqual(doc.supplier_type, "New")
		self.assertEqual(doc.country, "United States")


class TestApplyExtractionTds(FrappeTestCase):
	COMPANY = "Code Dynamic Solutions Private Limited"
	TDS_ACCOUNT = "TDS Payable - CDS"
	GSTIN = "24AABCT1234M1ZH"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		if not frappe.db.exists("Tax Withholding Category", "TEST TWC Extraction"):
			frappe.get_doc(
				{
					"doctype": "Tax Withholding Category",
					"name": "TEST TWC Extraction",
					"category_name": "TEST TWC Extraction",
					"rates": [{"from_date": "2020-01-01", "to_date": "2099-12-31", "tax_withholding_rate": 10}],
					"accounts": [{"company": cls.COMPANY, "account": cls.TDS_ACCOUNT}],
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Supplier", {"gstin": cls.GSTIN}):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "PIA Extraction TWC Supplier",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
					"gstin": cls.GSTIN,
					"tax_withholding_category": "TEST TWC Extraction",
				}
			).insert(ignore_permissions=True)

	def test_flags_tds_from_existing_suppliers_withholding_category(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		parsed = {
			"gst_number": self.GSTIN,
			"invoice_date": "2026-06-01",
			"items": [{"item_name": "Widget", "qty": 1, "rate": 1000, "amount": 1000}],
			"taxes": [],
			"taxable_amount": 1000,
		}
		_apply_extraction(doc, parsed)

		self.assertEqual(doc.supplier_type, "Existing")
		self.assertEqual(doc.is_tds_applicable, 1)
		self.assertEqual(doc.tds_rate, 10)
		self.assertEqual(doc.tds_account, self.TDS_ACCOUNT)
		self.assertEqual(doc.tds_amount, 100)  # 10% of the 1000 taxable amount

	def test_invoice_stated_tds_takes_precedence_over_supplier_category(self):
		doc = frappe.get_doc({"doctype": "Purchase Expense Center", "source": "Manual Upload"})
		parsed = {
			"gst_number": self.GSTIN,
			"invoice_date": "2026-06-01",
			"items": [{"item_name": "Widget", "qty": 1, "rate": 1000, "amount": 1000}],
			"taxes": [],
			"taxable_amount": 1000,
			"tds_rate": 2,
			"tds_amount": 20,
		}
		_apply_extraction(doc, parsed)

		# 2%/20 as printed on the invoice, not the Supplier's own 10% default.
		self.assertEqual(doc.tds_rate, 2)
		self.assertEqual(doc.tds_amount, 20)
