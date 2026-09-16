import frappe
from frappe.tests.utils import FrappeTestCase

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.tasks import _apply_extraction


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
