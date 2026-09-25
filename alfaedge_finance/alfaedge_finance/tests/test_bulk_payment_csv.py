import io

import frappe
import openpyxl
from frappe.tests.utils import FrappeTestCase

from alfaedge_finance.alfaedge_finance.page.bulk_payment_csv.bulk_payment_csv import download_xlsx, get_rows

COMPANY = "Code Dynamic Solutions Private Limited"
EXPENSE_ACCOUNT = "Miscellaneous Expenses - CDS"
TEST_ITEM = "BPC Test Item"


class TestBulkPaymentCSV(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Item", TEST_ITEM):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": TEST_ITEM,
					"item_group": "All Item Groups",
					"stock_uom": "Nos",
					"is_stock_item": 0,
					"gst_hsn_code": "998719",
				}
			).insert(ignore_permissions=True)
		self.supplier = (
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "BPC Supplier " + frappe.generate_hash(length=6),
					"supplier_group": "All Supplier Groups",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def tearDown(self):
		frappe.db.rollback()

	def _invoice(self, amount, due_date):
		pi = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": COMPANY,
				"supplier": self.supplier,
				"posting_date": "2026-01-10",
				"set_posting_time": 1,
				"due_date": due_date,
				"items": [{"item_code": TEST_ITEM, "qty": 1, "rate": amount, "expense_account": EXPENSE_ACCOUNT}],
			}
		).insert(ignore_permissions=True)
		pi.submit()
		return pi

	def _advance(self, amount):
		bank = frappe.db.get_value(
			"Bank Account", {"company": COMPANY, "is_default": 1, "is_company_account": 1}, "account"
		)
		pe = frappe.get_doc(
			{
				"doctype": "Payment Entry",
				"payment_type": "Pay",
				"company": COMPANY,
				"posting_date": "2026-01-05",
				"party_type": "Supplier",
				"party": self.supplier,
				"paid_from": bank,
				"paid_to": frappe.get_cached_value("Company", COMPANY, "default_payable_account"),
				"paid_amount": amount,
				"received_amount": amount,
				"reference_no": "BPC-ADV",
				"reference_date": "2026-01-05",
			}
		).insert(ignore_permissions=True)
		pe.submit()

	def _row(self, transaction_date):
		rows = get_rows(
			{"company": COMPANY, "txn_type": "Purchase Invoices", "transaction_date": transaction_date}
		)
		return next((r for r in rows if r["transaction_amount"] and self.supplier in r["party_label"]), None)

	def test_invoice_due_on_the_payment_day_is_included(self):
		self._invoice(1000, "2026-01-20")
		self.assertEqual(self._row("2026-01-20")["transaction_amount"], 1000)
		self.assertIsNone(self._row("2026-01-19"))

	def test_open_advance_is_deducted(self):
		self._invoice(1000, "2026-01-20")
		self._advance(300)
		row = self._row("2026-01-20")
		self.assertEqual(row["transaction_amount"], 700)
		self.assertEqual(row["invoices_due"], 1000)
		self.assertEqual(row["advance_deducted"], 300)

	def test_supplier_fully_covered_by_advance_is_left_out(self):
		self._invoice(1000, "2026-01-20")
		self._advance(1500)
		row = self._row("2026-01-20")
		self.assertFalse(row["matched"])
		self.assertIn("Covered by open advance", row["reason"])

	def test_excel_keeps_account_numbers_as_text(self):
		download_xlsx(
			[
				{
					"debit_account_no": "924020012345678",
					"transaction_amount": "14320.229999999998",
					"beneficiary_name": "A",
					"beneficiary_account_no": "000123456789012",
					"beneficiary_ifsc": "HDFC0000001",
					"transaction_date": "20/01/2026",
					"payment_mode": "N",
					"customer_ref_no": "AA2001012",
					"beneficiary_nickname": "a",
				}
			],
			"Purchase Invoices",
		)
		self.assertEqual(frappe.response["type"], "binary")
		sheet = openpyxl.load_workbook(io.BytesIO(frappe.response["filecontent"])).active
		self.assertEqual(sheet["A1"].value, "Debit Account Number (Mandatory)")
		self.assertEqual(sheet["A2"].value, "924020012345678")
		self.assertEqual(sheet["D2"].value, "000123456789012")  # leading zeros kept
		self.assertEqual(sheet["B2"].value, 14320.23)
