### alfaEdge Finance

Finance app for alfaEdge (Code Dynamic Solutions Private Limited). Currently includes
a Bulk Payment CSV tool and Purchase Invoice Automation.

## Purchase Invoice Automation

Turns a supplier-emailed PDF invoice into a draft `Purchase Invoice` with minimal manual
work, while keeping a human in the loop for every review and submission decision.

### Flow

1. A supplier emails a PDF invoice to `invoices@finance.alfaedge.org`.
2. A Cloudflare Email Worker parses the MIME message and POSTs the PDF + email metadata
   to this app's webhook.
3. The webhook creates a `Purchase Expense Center` record (`status = Pending`) and
   enqueues a background job - it does not wait for extraction, so the Worker gets a
   fast response.
4. The background job calls Bifrost (a self-hosted OpenAI-compatible LLM gateway) to
   extract supplier, line items, and tax breakdown from the PDF, matches the supplier by
   GSTIN, and pre-fills any item/tax mappings already known from prior invoices.
   `status` becomes `Extracted` (or `Failed`, with a reason, if anything goes wrong).
5. A reviewer opens the record, maps any remaining line items to internal `Item`s and
   any remaining tax rows to ledger `Account`s (once per unique description/tax type -
   reused automatically on future invoices), then clicks **Create Invoice**.
6. A **Draft** `Purchase Invoice` is created. This app never submits an invoice - that
   remains a manual step for the reviewer.

PDFs can also be ingested without email, directly from Desk: a **"New from PDF"** button
on the `Purchase Expense Center` list view accepts one or more PDFs at once (drag-and-
drop is not required - it's a plain file picker) and feeds the exact same pipeline.

### DocTypes

| DocType | Purpose |
|---|---|
| `Purchase Expense Center` | The staging/review record for one inbound invoice - extraction status, supplier match, line items (`Purchase Invoice Item`, reused from ERPNext), tax rows, and a link to the `Purchase Invoice` once created. |
| `Purchase Expense Center Tax` | Child table of tax rows (type, rate, amount, mapped ledger account) on a `Purchase Expense Center`. |
| `Purchase Item Mapping` | Global (not per-supplier) map of exact supplier line-item text → internal `Item`. Grows automatically as invoices are reviewed. |
| `Tax Account Mapping` | Global map of tax type (e.g. `CGST`) → ledger `Account`. |
| `TDS Category` | Small master list of TDS sections (e.g. "194J - Technical Services (2%)") with a rate and a liability account. |
| `Purchase Invoice Automation Settings` | Single. Company, default UOM, default TDS account, Bifrost endpoint/model/key, and the webhook shared secret. |

`Purchase Invoice Item` also gets a custom field, `mapped_item` (Link → Item), via the
`custom_field` fixture in `alfaedge_finance/fixtures/`.

### Ingestion endpoints

**Email webhook** (called by the Cloudflare Worker; guest-allowed, secured by a shared
secret):

```
POST /api/method/alfaedge_finance.alfaedge_finance.api.purchase_invoice_webhook.receive_invoice_email
Content-Type: application/json

{
  "email_body": "string",
  "sender_email": "string",
  "subject": "string",
  "webhook_secret": "string",
  "attachments": [
    { "filename": "invoice.pdf", "content_type": "application/pdf", "content": "<base64>" }
  ]
}
```
Returns `{"success": true, "created": ["PEC-2026-00001", ...]}` - one record per PDF
attachment found. This request shape is fixed by the Worker; do not change field names
here without also updating the Worker.

**Manual upload** (authenticated Desk user, no secret required):

```
POST /api/method/alfaedge_finance.alfaedge_finance.api.purchase_invoice_upload.upload_purchase_invoices
Content-Type: application/json

{ "attachments": [ { "filename": "invoice.pdf", "content": "<base64>" } ] }
```
Returns a list of `{filename, name, status}` - one entry per file, `status` is `queued`
or `failed`.

### Configuration

Set these on **Purchase Invoice Automation Settings** (Desk: search "Purchase Invoice
Automation Settings"):

- `company` - the Company automated invoices are created against.
- `default_uom` - UOM used for extracted line items (default `Nos`).
- `bifrost_endpoint` / `bifrost_model` - Bifrost's `/v1/chat/completions` URL and model
  alias.
- `bifrost_virtual_key` - leave blank to fall back to `BIFROST_VIRTUAL_KEY` in
  `site_config.json`.
- `webhook_secret` - leave blank to fall back to `WEBHOOK_SECRET` in
  `site_config.json`. The Cloudflare Worker must be configured with the same value via
  `wrangler secret put WEBHOOK_SECRET`.

**Go-live checklist:** rotate the Bifrost virtual key and webhook secret before
production use if either was ever shared outside of encrypted config (e.g. pasted into
a chat, a script, or committed anywhere) - treat any such exposure as a compromised
credential, not a cosmetic concern.

### Bulk item/tax mapping

The `Purchase Expense Center` list view has a **"Bulk Map Items"** button: it lists
every distinct unmapped line-item description across all records with a per-item Item
picker, and applies all of them at once (a direct SQL update, since `mapped_item` does
not affect invoice totals) while also saving each mapping into `Purchase Item Mapping`
for future invoices. Tax account mapping has no bulk tool - in practice there are only
a handful of distinct tax types (CGST/SGST/IGST/Other), so mapping them individually
during the first few invoice reviews is simpler than building a second bulk dialog.

### TDS deduction

Most suppliers never cross the TDS threshold on a single invoice, so ERPNext's own
automatic Tax Withholding Category mechanism (unaffected by this app) rarely fires. A
few suppliers - e.g. OVHtech R&D (India) Private Limited - should always have TDS
deducted regardless, which previously meant manually adding a deduction-on-actual tax
row to every invoice from them. The `Purchase Expense Center` now has a **TDS** section
for this:

- `is_tds_applicable`, `tds_category` (Link → `TDS Category`), `tds_rate`, `tds_account`,
  `tds_amount`.
- **Auto-detected**: if the supplier's own invoice explicitly states a TDS deduction
  (e.g. "Less: TDS @ 2%"), extraction fills `is_tds_applicable`, `tds_rate`, `tds_amount`,
  and `tds_account` (from the Settings' Default TDS Account) automatically - Bifrost is
  told not to invent a TDS figure that isn't printed.
- **Manual fallback** (the common case for OVH-style suppliers, since their invoices
  don't print the TDS breakdown): the reviewer picks a `TDS Category` from the list -
  selecting one auto-fills the rate, account, and computes the amount as
  `extracted_taxable_amount × rate` (TDS is calculated on the pre-GST taxable amount, not
  the GST-inclusive total).
- At invoice creation, this becomes a `charge_type = "Actual"`, `category = "Total"`,
  `add_deduct_tax = "Deduct"` row on the Purchase Invoice - the same shape ERPNext's own
  automatic TDS uses - reducing the amount payable to the supplier while posting the
  withheld amount to the TDS liability account. Invoice creation blocks if
  `is_tds_applicable` is checked but the account or amount is missing, same as item/tax
  mapping. The grand-total reconciliation check adds the TDS amount back before comparing
  against the invoice's own printed total, since TDS isn't part of what the supplier
  billed - it's withheld at payment time.

### Running the tests

```bash
bench --site erp.alfaedge.org set-config allow_tests true   # first time only
bench --site erp.alfaedge.org run-tests --app alfaedge_finance
```

Covers: Bifrost markdown-fence stripping, GST-based supplier matching (match / no
match / no GSTIN), and Purchase Invoice creation (blocked on missing item/tax mapping,
draft-only creation, tax rows, grand-total tolerance warning, refusing to double-create
an invoice).

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app alfaedge_finance
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/alfaedge_finance
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
