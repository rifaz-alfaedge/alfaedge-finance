"""Inbound email webhook, called by the Cloudflare Email Worker on
invoices@finance.alfaedge.org.

Request contract (fixed on the Worker side - see project README):
    POST /api/method/alfaedge_finance.api.purchase_invoice_webhook.receive_invoice_email
    {
      "email_body": str, "sender_email": str, "subject": str, "webhook_secret": str,
      "attachments": [{"filename": str, "content_type": str, "content": "<base64>"}]
    }

Returns fast (just persists the shell record + PDF and enqueues extraction) since
Bifrost's PDF extraction can take 10-30+ seconds - too slow for a synchronous
webhook response.
"""

import base64
import json

import frappe

from alfaedge_finance.alfaedge_finance.doctype.purchase_invoice_automation_settings.purchase_invoice_automation_settings import (
	get_settings,
)
from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.tasks import intake_pdf


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive_invoice_email():
	data = frappe.local.form_dict

	settings = get_settings()
	if not settings.get("webhook_secret") or data.get("webhook_secret") != settings["webhook_secret"]:
		frappe.throw("Unauthorized: invalid webhook secret", frappe.PermissionError)

	attachments = data.get("attachments") or []
	if isinstance(attachments, str):
		attachments = json.loads(attachments)

	sender_email = data.get("sender_email") or ""
	subject = data.get("subject") or ""
	email_body = data.get("email_body") or ""

	created = []
	for attachment in attachments:
		filename = (attachment.get("filename") or "").lower()
		content_type = (attachment.get("content_type") or "").lower()
		if "pdf" not in content_type and not filename.endswith(".pdf"):
			continue

		pdf_bytes = base64.b64decode(attachment["content"])
		name = intake_pdf(
			pdf_bytes,
			attachment.get("filename") or "invoice.pdf",
			source="Email",
			sender_email=sender_email,
			subject=subject,
			email_body=email_body,
		)
		created.append(name)

	if not created:
		frappe.throw("No PDF attachment found in this email")

	frappe.response["message"] = {"success": True, "created": created}
