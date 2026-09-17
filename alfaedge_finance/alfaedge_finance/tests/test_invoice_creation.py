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

		if not frappe.db.exists("Tax Withholding Category", "TEST TWC 194J"):
			frappe.get_doc(
				{
					"doctype": "Tax Withholding Category",
					"name": "TEST TWC 194J",
					"category_name": "TEST TWC 194J",
					"rates": [
						{
							"from_date": "2020-01-01",
							"to_date": "2099-12-31",
							"tax_withholding_rate": 2,
							"single_threshold": 1,  # low on purpose: any test invoice amount should trigger TDS
						}
					],
					"accounts": [{"company": COMPANY, "account": TEST_TDS_ACCOUNT}],
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
				"supplier_invoice_date": "2026-01-15",
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
		self.assertEqual(doc.invoice_status, "Invoice Draft")
		self.assertEqual(doc.purchase_invoice, pi.name)
		self.assertEqual(pi.purchase_expense_center, doc.name)
		self.assertEqual(pi.posting_date, doc.supplier_invoice_date)

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

	def test_invoice_status_tracks_submit_and_cancel(self):
		doc = self._make_expense_center(with_tax=False)
		result = create_purchase_invoice_from_expense_center(doc.name)
		doc.reload()
		self.assertEqual(doc.invoice_status, "Invoice Draft")

		pi = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		pi.submit()
		doc.reload()
		self.assertEqual(doc.invoice_status, "Invoice Submitted")

		pi.cancel()
		doc.reload()
		self.assertEqual(doc.invoice_status, "Invoice Draft")

	def test_deleting_cancelled_invoice_unlinks_and_reopens_expense_center(self):
		doc = self._make_expense_center(with_tax=False)
		result = create_purchase_invoice_from_expense_center(doc.name)

		pi = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		pi.submit()
		pi.cancel()

		frappe.delete_doc("Purchase Invoice", pi.name, ignore_permissions=True)

		doc.reload()
		self.assertIsNone(doc.purchase_invoice)
		self.assertEqual(doc.invoice_status, "Pending Review")

		# Create Invoice is available again now that the link is cleared.
		result2 = create_purchase_invoice_from_expense_center(doc.name)
		self.assertTrue(frappe.db.exists("Purchase Invoice", result2["purchase_invoice"]))

	def test_deleting_expense_center_blocked_while_invoice_linked(self):
		doc = self._make_expense_center(with_tax=False)
		create_purchase_invoice_from_expense_center(doc.name)

		with self.assertRaises(frappe.LinkExistsError):
			frappe.delete_doc("Purchase Expense Center", doc.name, ignore_permissions=True)

	def test_blocks_when_tds_applicable_but_unmapped(self):
		doc = self._make_expense_center(with_tax=False)
		doc.is_tds_applicable = 1
		doc.tds_rate = 2
		doc.save(ignore_permissions=True)  # no tds_account, no tds_amount

		with self.assertRaises(frappe.ValidationError):
			create_purchase_invoice_from_expense_center(doc.name)

	def test_creates_invoice_with_manual_tds_deduction(self):
		# No Tax Withholding Category picked here - just tds_rate/account/amount
		# entered directly. (Picking a category on a non-excluded supplier
		# immediately syncs it to the Supplier - see
		# test_picking_tds_category_updates_the_supplier - which would make this
		# go native instead; that's covered separately.)
		doc = self._make_expense_center(with_tax=False)
		doc.is_tds_applicable = 1
		doc.tds_rate = 2
		doc.tds_account = TEST_TDS_ACCOUNT
		doc.tds_amount = 2  # 2% of the 100 taxable amount
		doc.extracted_grand_total = 100  # invoice's own printed total, before TDS
		doc.save(ignore_permissions=True)

		result = create_purchase_invoice_from_expense_center(doc.name)

		pi = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(pi.apply_tds, 0)
		tds_rows = [row for row in pi.taxes if row.account_head == TEST_TDS_ACCOUNT]
		self.assertEqual(len(tds_rows), 1)
		self.assertEqual(tds_rows[0].charge_type, "Actual")
		self.assertEqual(tds_rows[0].add_deduct_tax, "Deduct")
		self.assertEqual(tds_rows[0].tax_amount, 2)
		self.assertEqual(pi.grand_total, 98)  # 100 - 2% TDS
		# extracted_grand_total (100) is pre-TDS, so this must not warn despite
		# pi.grand_total (98) differing from it by more than the ₹1 tolerance.
		self.assertIsNone(result["warning"])

	def test_picking_tds_category_updates_the_supplier(self):
		gstin = "24AAQCA8719H1ZC"
		if not frappe.db.exists("Supplier", {"gstin": gstin}):
			supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "PIA Category Sync Supplier",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
					"gstin": gstin,
				}
			).insert(ignore_permissions=True)
		else:
			supplier = frappe.get_doc("Supplier", {"gstin": gstin})
		self.assertFalse(supplier.tax_withholding_category)

		doc = frappe.get_doc(
			{
				"doctype": "Purchase Expense Center",
				"source": "Manual Upload",
				"supplier_type": "Existing",
				"existing_supplier": supplier.name,
				"is_tds_applicable": 1,
				"tds_category": "TEST TWC 194J",
			}
		)
		doc.insert(ignore_permissions=True)

		self.assertEqual(
			frappe.db.get_value("Supplier", supplier.name, "tax_withholding_category"),
			"TEST TWC 194J",
		)

	def test_creates_invoice_with_native_apply_tds(self):
		gstin = "27AABCU9603R1ZN"
		if not frappe.db.exists("Supplier", {"gstin": gstin}):
			supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "PIA Native TDS Supplier",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
					"gstin": gstin,
					"tax_withholding_category": "TEST TWC 194J",
				}
			).insert(ignore_permissions=True)
		else:
			supplier = frappe.get_doc("Supplier", {"gstin": gstin})

		doc = frappe.get_doc(
			{
				"doctype": "Purchase Expense Center",
				"source": "Manual Upload",
				"status": "Extracted",
				"supplier_type": "Existing",
				"existing_supplier": supplier.name,
				"supplier_gst": gstin,
				"supplier_invoice_number": frappe.generate_hash(length=8),
				"supplier_invoice_date": "2026-01-15",
			}
		)
		doc.append(
			"items",
			{
				"item_name": "Widget",
				"description": "Widget",
				"qty": 1,
				"rate": 1000,
				"amount": 1000,
				"uom": "Nos",
				"stock_qty": 1,
				"base_rate": 1000,
				"base_amount": 1000,
				"mapped_item": TEST_ITEM,
			},
		)
		doc.insert(ignore_permissions=True)

		result = create_purchase_invoice_from_expense_center(doc.name)

		pi = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(pi.apply_tds, 1)
		self.assertEqual(pi.tax_withholding_category, "TEST TWC 194J")
		# ERPNext's own set_tax_withholding() computed and appended this row - we
		# didn't build it ourselves.
		tds_rows = [row for row in pi.taxes if row.account_head == TEST_TDS_ACCOUNT]
		self.assertEqual(len(tds_rows), 1)
		self.assertEqual(tds_rows[0].tax_amount, 20)  # 2% of 1000

	def test_excluded_supplier_never_uses_native_apply_tds(self):
		gstin = "29AACCO6253G1ZB"  # the real OVH GSTIN, seeded with exclude_from_auto_tds=1
		if not frappe.db.exists("Supplier", {"gstin": gstin}):
			self.skipTest("OVH supplier fixture not present on this site")

		doc = self._make_expense_center(with_tax=False)
		doc.supplier_gst = gstin
		doc.existing_supplier = frappe.db.get_value("Supplier", {"gstin": gstin}, "name")
		doc.is_tds_applicable = 1
		doc.tds_category = "TEST TWC 194J"
		doc.tds_rate = 2
		doc.tds_account = TEST_TDS_ACCOUNT
		doc.tds_amount = 2
		doc.save(ignore_permissions=True)

		result = create_purchase_invoice_from_expense_center(doc.name)

		pi = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(pi.apply_tds, 0)
		self.assertTrue(any(row.account_head == TEST_TDS_ACCOUNT for row in pi.taxes))

		self.assertFalse(
			frappe.db.get_value("Supplier", doc.existing_supplier, "tax_withholding_category")
		)


class TestMultiCurrencyInvoice(FrappeTestCase):
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

		if not frappe.db.exists(
			"Currency Exchange", {"from_currency": "USD", "to_currency": "INR", "date": "2026-01-15"}
		):
			frappe.get_doc(
				{
					"doctype": "Currency Exchange",
					"date": "2026-01-15",
					"from_currency": "USD",
					"to_currency": "INR",
					"exchange_rate": 90,
					"for_buying": 1,
				}
			).insert(ignore_permissions=True)

		# ERPNext also requires the Payable account itself to be denominated in
		# the invoice's currency (a separate constraint from Supplier default
		# currency/conversion_rate) - real multi-currency AP setup, not something
		# this app should silently create in the Chart of Accounts, but a test
		# needs one to exercise the full path.
		if not frappe.db.exists("Account", "Creditors USD - CDS"):
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "Creditors USD",
					"parent_account": "Accounts Payable - CDS",
					"account_currency": "USD",
					"account_type": "Payable",
					"company": COMPANY,
				}
			).insert(ignore_permissions=True)

		if not frappe.db.exists("Supplier", {"supplier_name": "PIA USD Supplier"}):
			cls.usd_supplier = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "PIA USD Supplier",
					"supplier_group": "All Supplier Groups",
					"supplier_type": "Company",
					"default_currency": "USD",
					"accounts": [{"company": COMPANY, "account": "Creditors USD - CDS"}],
				}
			).insert(ignore_permissions=True)
		else:
			cls.usd_supplier = frappe.get_doc("Supplier", {"supplier_name": "PIA USD Supplier"})

	def _make_usd_expense_center(self, currency, rate=100):
		doc = frappe.get_doc(
			{
				"doctype": "Purchase Expense Center",
				"source": "Manual Upload",
				"status": "Extracted",
				"supplier_type": "Existing",
				"existing_supplier": self.usd_supplier.name,
				"currency": currency,
				"supplier_invoice_number": frappe.generate_hash(length=8),
				"supplier_invoice_date": "2026-01-15",
			}
		)
		doc.append(
			"items",
			{
				"item_name": "Widget",
				"description": "Widget",
				"qty": 1,
				"rate": rate,
				"amount": rate,
				"uom": "Nos",
				"stock_qty": 1,
				"base_rate": rate,
				"base_amount": rate,
				"mapped_item": TEST_ITEM,
			},
		)
		doc.insert(ignore_permissions=True)
		return doc

	def test_creates_invoice_in_suppliers_currency_with_conversion_rate(self):
		doc = self._make_usd_expense_center("USD", rate=100)
		result = create_purchase_invoice_from_expense_center(doc.name)

		pi = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(pi.currency, "USD")
		self.assertEqual(pi.conversion_rate, 90)
		self.assertEqual(pi.items[0].amount, 100)
		self.assertEqual(pi.items[0].base_amount, 9000)  # 100 * 90, computed by core - not by us

	def test_supplier_default_currency_overrides_extracted_currency(self):
		# Extraction says INR, but the Supplier is pinned to USD - Supplier wins,
		# which is exactly what avoids ERPNext's "can only be made in currency: X" error.
		doc = self._make_usd_expense_center("INR", rate=50)
		result = create_purchase_invoice_from_expense_center(doc.name)

		pi = frappe.get_doc("Purchase Invoice", result["purchase_invoice"])
		self.assertEqual(pi.currency, "USD")
