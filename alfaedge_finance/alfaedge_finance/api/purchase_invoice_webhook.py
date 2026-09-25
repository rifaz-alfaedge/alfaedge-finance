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
import hmac
import json

import frappe
from frappe.rate_limiter import rate_limit

from alfaedge_finance.alfaedge_finance.doctype.purchase_invoice_automation_settings.purchase_invoice_automation_settings import (
	get_settings,
)
from alfaedge_finance.alfaedge_finance.purchase_invoice_automation.tasks import intake_pdf

# Guards against a leaked secret or a Worker stuck resending: each PDF becomes a stored
# file and one paid extraction call.
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 20
REQUESTS_PER_HOUR = 60


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=REQUESTS_PER_HOUR, seconds=60 * 60)
def receive_invoice_email():
	data = frappe.local.form_dict

	settings = get_settings()
	expected, given = settings.get("webhook_secret") or "", data.get("webhook_secret") or ""
	# Constant-time comparison, so the secret can't be recovered by timing the responses.
	if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
		frappe.throw("Unauthorized: invalid webhook secret", frappe.PermissionError)

	attachments = data.get("attachments") or []
	if isinstance(attachments, str):
		attachments = json.loads(attachments)
	if len(attachments) > MAX_ATTACHMENTS:
		frappe.throw(f"Too many attachments ({len(attachments)}); at most {MAX_ATTACHMENTS} per email")

	sender_email = data.get("sender_email") or ""
	subject = data.get("subject") or ""
	email_body = data.get("email_body") or ""

	created = []
	for attachment in attachments:
		filename = (attachment.get("filename") or "").lower()
		content_type = (attachment.get("content_type") or "").lower()
		if "pdf" not in content_type and not filename.endswith(".pdf"):
			continue

		content = attachment.get("content") or ""
		# base64 is 4 characters per 3 bytes - check before decoding.
		if len(content) * 3 // 4 > MAX_ATTACHMENT_BYTES:
			frappe.log_error(
				title="Purchase Invoice webhook: attachment too large",
				message=f"{attachment.get('filename')!r} from {data.get('sender_email')!r} was skipped.",
			)
			continue
		pdf_bytes = base64.b64decode(content)
		if b"%PDF" not in pdf_bytes[:1024]:
			# Named .pdf but not a PDF - don't store it or send it for extraction.
			continue
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
