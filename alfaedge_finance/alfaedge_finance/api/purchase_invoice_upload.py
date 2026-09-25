"""Manual PDF ingestion for Desk users - the non-email path for creating
Purchase Expense Center records (single-file "New from PDF" or multi-file bulk
upload). Runs as an authenticated Desk user, so no webhook secret is required.

Request shape mirrors the email webhook's attachments[] entries for consistency:
    POST /api/method/alfaedge_finance.api.purchase_invoice_upload.upload_purchase_invoices
    { "attachments": [{"filename": str, "content": "<base64>"}] }
"""

import base64
import json

import frappe

from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.tasks import intake_pdf


@frappe.whitelist()
def upload_purchase_invoices(attachments):
	frappe.has_permission("Purchase Expense Center", "create", throw=True)
	if isinstance(attachments, str):
		attachments = json.loads(attachments)

	results = []
	for attachment in attachments:
		filename = attachment.get("filename") or "invoice.pdf"
		try:
			pdf_bytes = base64.b64decode(attachment["content"])
			name = intake_pdf(pdf_bytes, filename, source="Manual Upload")
			results.append({"filename": filename, "name": name, "status": "queued"})
		except Exception:
			frappe.log_error(
				title="Purchase Invoice Automation: manual upload failed",
				message=frappe.get_traceback(),
			)
			results.append({"filename": filename, "name": None, "status": "failed"})

	return results
