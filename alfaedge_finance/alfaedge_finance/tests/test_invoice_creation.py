import frappe
from frappe.tests.utils import FrappeTestCase

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.invoice_creation import (
	create_purchase_invoice_from_expense_center,
)

COMPANY = "Code Dynamic Solutions Private Limited"
TEST_ITEM = "PIA Test Item"
TEST_CGST_ACCOUNT = "Input Tax CGST - CDS"
TEST_SGST_ACCOUNT = "Input Tax SGST - CDS"
TEST_GSTIN = "32AABCU9603R1ZW"  # same state (32/Kerala) as the test Company, so CGST+SGST is valid
TEST_TDS_ACCOUNT = "TDS Payable - CDS"


class TestInvoiceCreation(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()

		if not frappe.db.exists("Item", TEST_ITEM):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": TEST_ITEM,
					"item_name": TEST_ITEM,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"gst_hsn_code": "998719",
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Supplier", {"gstin": TEST_GSTIN}):
			cls.supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "PIA Invoice Test Supplier",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
					"gstin": TEST_GSTIN,
				}
			).insert(ignore_permissions=True)
		else:
			cls.supplier = frappe.get_doc("Supplier", {"gstin": TEST_GSTIN})

		settings = frappe.get_single("Purchase Invoice Automation Settings")
		settings.company = COMPANY
		settings.default_uom = "Nos"
		settings.save(ignore_permissions=True)

		if not frappe.db.exists("TDS Category", "TEST-194J"):
			frappe.get_doc(
				{
					"doctype": "TDS Category",
					"category_name": "TEST-194J",
					"rate": 2,
					"account": TEST_TDS_ACCOUNT,
				}
			).insert(ignore_permissions=True)

	def _make_expense_center(self, with_tax=True):
		doc = frappe.get_doc(
			{
				"doctype": "Purchase Expense Center",
				"source": "Manual Upload",
				"status": "Extracted",
				"supplier_type": "Existing",
				"existing_supplier": self.supplier.name,
				"supplier_gst": TEST_GSTIN,
				"supplier_invoice_number": frappe.generate_hash(length=8),
				"extracted_grand_total": 118 if with_tax else 100,
			}
		)
		doc.append(
			"items",
			{
				"item_name": "Widget",
				"description": "Widget",
				"qty": 1,
				"rate": 100,
				"amount": 100,
				"uom": "Nos",
				"stock_qty": 1,
				"base_rate": 100,
				"base_amount": 100,
				"mapped_item": TEST_ITEM,
			},
		)
		if with_tax:
			doc.append(
				"taxes",
				{"tax_type": "CGST", "rate": 9, "amount": 9, "mapped_account": TEST_CGST_ACCOUNT},
			)
			doc.append(
				"taxes",
				{"tax_type": "SGST", "rate": 9, "amount": 9, "mapped_account": TEST_SGST_ACCOUNT},
			)
		doc.insert(ignore_permissions=True)
		return doc

	def test_blocks_when_item_row_unmapped(self):
		doc = self._make_expense_center(with_tax=False)
		doc.items[0].mapped_item = None
		doc.save(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			create_purchase_invoice_from_expense_center(doc.name)

	def test_blocks_when_tax_row_unmapped(self):
		doc = self._make_expense_center(with_tax=True)
		doc.taxes[0].mapped_account = None
		doc.save(ignore_permissions=True)

		with self.assertRaises(frappe.ValidationError):
			create_purchase_invoice_from_expense_center(doc.name)

	def test_creates_draft_invoice_with_taxes(self):
		doc = self._make_expense_center(with_tax=True)
		result = create_purchase_invoice_from_expense_center(doc.name)

		pi = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(pi.docstatus, 0)  # draft, never submitted
		self.assertEqual(len(pi.taxes), 2)
		self.assertEqual(pi.taxes[0].charge_type, "On Net Total")
		self.assertEqual(pi.taxes[0].account_head, TEST_CGST_ACCOUNT)
		self.assertIsNone(result["warning"])

		doc.reload()
		self.assertEqual(doc.invoice_status, "Invoice Created")
		self.assertEqual(doc.purchase_invoice, pi.name)
		self.assertEqual(pi.purchase_expense_center, doc.name)

	def test_source_pdf_is_attached_to_the_invoice_too(self):
		doc = self._make_expense_center(with_tax=False)
		# .txt, not .pdf: this test is about copying the File reference across
		# doctypes, not about PDF content validation.
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "test_invoice.txt",
				"attached_to_doctype": "Purchase Expense Center",
				"attached_to_name": doc.name,
				"content": b"test invoice placeholder",
				"is_private": 1,
			}
		).insert(ignore_permissions=True)
		doc.pdf_attachment = file_doc.file_url
		doc.save(ignore_permissions=True)

		result = create_purchase_invoice_from_expense_center(doc.name)

		attached = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Purchase Invoice", "attached_to_name": result["purchase_invoice"]},
		)
		self.assertEqual(len(attached), 1)

	def test_warns_on_grand_total_mismatch(self):
		doc = self._make_expense_center(with_tax=True)
		doc.extracted_grand_total = 500
		doc.save(ignore_permissions=True)

		result = create_purchase_invoice_from_expense_center(doc.name)
		self.assertIsNotNone(result["warning"])

	def test_blocks_recreating_invoice(self):
		doc = self._make_expense_center(with_tax=False)
		create_purchase_invoice_from_expense_center(doc.name)

		with self.assertRaises(frappe.ValidationError):
			create_purchase_invoice_from_expense_center(doc.name)

	def test_blocks_when_tds_applicable_but_unmapped(self):
		doc = self._make_expense_center(with_tax=False)
		doc.is_tds_applicable = 1
		doc.tds_rate = 2
		doc.save(ignore_permissions=True)  # no tds_account, no tds_amount

		with self.assertRaises(frappe.ValidationError):
			create_purchase_invoice_from_expense_center(doc.name)

	def test_creates_invoice_with_tds_deduction(self):
		doc = self._make_expense_center(with_tax=False)
		doc.is_tds_applicable = 1
		doc.tds_category = "TEST-194J"
		doc.tds_rate = 2
		doc.tds_account = TEST_TDS_ACCOUNT
		doc.tds_amount = 2  # 2% of the 100 taxable amount
		doc.extracted_grand_total = 100  # invoice's own printed total, before TDS
		doc.save(ignore_permissions=True)

		result = create_purchase_invoice_from_expense_center(doc.name)

		pi = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		tds_rows = [row for row in pi.taxes if row.account_head == TEST_TDS_ACCOUNT]
		self.assertEqual(len(tds_rows), 1)
		self.assertEqual(tds_rows[0].charge_type, "Actual")
		self.assertEqual(tds_rows[0].add_deduct_tax, "Deduct")
		self.assertEqual(tds_rows[0].tax_amount, 2)
		self.assertEqual(pi.grand_total, 98)  # 100 - 2% TDS
		# extracted_grand_total (100) is pre-TDS, so this must not warn despite
		# pi.grand_total (98) differing from it by more than the ₹1 tolerance.
		self.assertIsNone(result["warning"])
