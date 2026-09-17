### alfaEdge Finance

Finance app for alfaEdge (Code Dynamic Solutions Private Limited). Currently includes
a Bulk Payment CSV tool and Purchase Invoice Automation.

## Purchase Invoice Automation

Turns a supplier-emailed PDF invoice into a draft `Purchase Invoice` with minimal manual
work, while keeping a human in the loop for every review and submission decision.

### Flow

1. A supplier emails a PDF invoice to `purchase@codedynamic.org` (production) or
   `test@codedynamic.org` (dev/testing).
2. A Cloudflare Email Worker (`alfaedge-invoice-email-worker` / `-dev`) parses the MIME
   message and POSTs the PDF + email metadata to the matching site's webhook.
3. The webhook creates a `Purchase Expense Center` record (`status = Pending`) and
   enqueues a background job - it does not wait for extraction, so the Worker gets a
   fast response.
4. The background job calls Bifrost (a self-hosted OpenAI-compatible LLM gateway) to
   extract supplier, line items, and tax breakdown from the PDF, matches the supplier
   (by GSTIN first, falling back to an exact supplier-name match when there's no usable
   GSTIN - see [Supplier matching](#supplier-matching) below), and pre-fills any
   item/tax mappings already known from prior invoices. `status` becomes `Extracted`
   (or `Failed`, with a reason, if anything goes wrong).
5. A reviewer opens the record, maps any remaining line items to internal `Item`s and
   any remaining tax rows to ledger `Account`s (once per unique description/tax type -
   reused automatically on future invoices), then clicks **Create Invoice**.
6. A **Draft** `Purchase Invoice` is created, dated to the supplier's own invoice date
   (`posting_date` = `supplier_invoice_date`). This app never submits an invoice - that
   remains a manual step for the reviewer. `invoice_status` on the source record tracks
   the invoice's actual state (`Invoice Draft` / `Invoice Submitted`), kept in sync
   automatically whenever the Purchase Invoice is submitted or cancelled.

PDFs can also be ingested without email, directly from Desk: a **"New from PDF"** button
on the `Purchase Expense Center` list view accepts one or more PDFs at once (drag-and-
drop is not required - it's a plain file picker) and feeds the exact same pipeline.

### Supplier matching

1. **By GSTIN** (`resolve_by_gst`): exact match against `Supplier.gstin`. The extracted
   `gst_number` is first checked against the actual 15-character Indian GSTIN shape
   (`looks_like_gstin`) - an overseas supplier has none at all, and the LLM has been
   caught mislabeling some other identifier (an EIN, in one real case) as `gst_number`
   despite being told not to; either way, anything that isn't GSTIN-shaped is treated as
   absent rather than stored or matched against.
2. **By exact name** (`resolve_by_name`), when GSTIN-based matching finds nothing: an
   exact, case/whitespace-insensitive match against `Supplier.supplier_name`. This is
   what catches an already-known overseas supplier (no GSTIN to match on at all) or a
   domestic one whose GST number the LLM got wrong.
3. **By normalized name + address** (`resolve_by_name_and_address`), when even that
   finds nothing: the name is normalized further (punctuation stripped too, so
   `"Anthropic, PBC"` and `"Anthropic PBC"` are the same) and matched against every
   Supplier's own normalized name.
   - A unique normalized-name match is trusted on its own.
   - If more than one Supplier normalizes to the same name, only one whose linked
     Address also agrees (exact pincode, or matching country+state) is trusted.
   - If *no* Supplier's name matches even loosely, an exact pincode match is still
     tried, but only for a Supplier whose name at least partially overlaps
     (one normalized name contains the other) - a bare pincode match against a
     completely unrelated name is never trusted (could be a different company at the
     same address).
   - Country/state/pincode come from `address`/`city`/`state`/`postal_code`/`country`
     in the extraction.
4. **New supplier**: if none of the above match, the record is flagged `New` for manual
   review, with `address`/`city`/`state`/`postal_code`/`country` filled from extraction
   (`country` defaults to India if extraction doesn't return one). All three checks
   re-run at invoice-creation time too (`_resolve_supplier`), in case the real Supplier
   was created or corrected in the meantime.

None of these tiers are fuzzy string-similarity matching - each requires an exact match
on some normalized/structured field (name, pincode, country+state), deliberately, to
keep the false-positive risk of linking to the wrong Supplier low. A near-miss that
doesn't satisfy any tier is routed to manual review as `New` rather than guessed at.

### DocTypes

| DocType | Purpose |
|---|---|
| `Purchase Expense Center` | The staging/review record for one inbound invoice - extraction status, supplier match, line items (`Purchase Invoice Item`, reused from ERPNext), tax rows, and a link to the `Purchase Invoice` once created. |
| `Purchase Expense Center Tax` | Child table of tax rows (type, rate, amount, mapped ledger account) on a `Purchase Expense Center`. |
| `Purchase Item Mapping` | Global (not per-supplier) map of exact supplier line-item text → internal `Item`. Grows automatically as invoices are reviewed. |
| `Tax Account Mapping` | Global map of tax type (e.g. `CGST`) → ledger `Account`. |
| `Purchase Invoice Automation Settings` | Single. Company, default UOM, default TDS account, Bifrost endpoint/model/key, and the webhook shared secret. |

TDS uses ERPNext's own `Tax Withholding Category` doctype rather than a custom one - see
[TDS deduction](#tds-deduction) below. `Supplier` also gets a custom field,
`exclude_from_auto_tds` (Check), for suppliers that need TDS deducted manually instead
of through ERPNext's own automatic mechanism.

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

Either way, mapping is remembered from that point on: saving a `Purchase Expense
Center` with a `mapped_item`/`mapped_account` set on any row - whether it got there via
the bulk dialog, extraction prefill, or just typed in by hand - upserts it into
`Purchase Item Mapping`/`Tax Account Mapping` automatically (`on_update`, see
`purchase_invoice_automation/mapping_sync.py`). You never need to map the same
description or tax type twice, and it's permanent regardless of what later happens to
the `Purchase Expense Center` it was mapped on (deleting that record does not remove the
mapping - they're independent doctypes).

Matching is normalized (whitespace collapsed, case-insensitive) before it's used as the
lookup/storage key, so trivial LLM re-wording between two extractions of what is
otherwise the same line item (`"SYS-1 rental"` vs `"  SYS-1   Rental  "`) still matches
the existing mapping (`purchase_invoice_automation/text_normalization.py`). The item's
*displayed* text on the invoice keeps its original casing/spacing - only the mapping key
is normalized. This does not merge two descriptions that are genuinely different text
(e.g. a recurring hosting invoice that appends a different date range or IP address each
month) - those still need mapping once per distinct wording, since we deliberately don't
do fuzzy/similarity matching (a real risk of silently mapping to the wrong Item).

Manage `Purchase Item Mapping` and `Tax Account Mapping` directly, independent of any
`Purchase Expense Center`, from the **Alfaedge Finance** workspace in the Desk sidebar -
standard Frappe list views, so you can add, edit, or delete mapping rows freely.

### Linking back from the Purchase Invoice

The created `Purchase Invoice` gets a read-only `purchase_expense_center` field (a
custom field) pointing back to its source record, and the original PDF is attached to
the Purchase Invoice as well (referencing the same stored file, not a duplicate copy) -
so a reviewer looking at the invoice alone can trace it back to the email/upload and
the original document without going through the Purchase Expense Center first.

### TDS deduction

Two mechanisms exist side by side, and invoice creation picks between them per
supplier - there is no separate custom "TDS Category" list; everything keys off
ERPNext's own `Tax Withholding Category` doctype and the Supplier's own field for it.

**Native (the default, used for most suppliers):** if the resolved Supplier has a
`Tax Withholding Category` set and isn't flagged `Exclude from Automatic TDS`, invoice
creation sets `apply_tds = 1` and `tax_withholding_category` on the Purchase Invoice and
lets ERPNext compute and append the withholding-tax row itself
(`accounts_controller.set_tax_withholding()` - the same thing that happens if a user
checked "Apply Tax Withholding Amount" by hand), including its own threshold logic. We
append nothing ourselves in this case.

**Manual override (for suppliers like OVHtech R&D (India) Private Limited):** some
suppliers should have TDS deducted on every invoice, but their per-invoice amount never
crosses ERPNext's own configured threshold, so native `apply_tds` would silently deduct
nothing (confirmed in testing: a Tax Withholding Category with a real threshold applies
TDS only once the amount clears it). Flag such a Supplier `Exclude from Automatic TDS`
(a custom field) - this app then never writes to that Supplier's own
`tax_withholding_category` and never sets `apply_tds` on invoices for them, and instead
appends the withholding row manually using the `Purchase Expense Center`'s own TDS
section fields: `is_tds_applicable`, `tds_category` (Link → `Tax Withholding Category`,
used only for calculation here, not synced anywhere), `tds_rate`, `tds_account`,
`tds_amount`.

The manual TDS section fills in from, in priority order:
1. **The invoice itself**, if it explicitly states a TDS deduction (e.g. "Less: TDS @
   2%") - extraction fills `tds_rate`/`tds_amount`/`tds_account` (from Settings' Default
   TDS Account) directly; Bifrost is told not to invent a figure that isn't printed.
2. **The Supplier's Tax Withholding Category**, if the invoice doesn't state one -
   resolved the same way ERPNext's own automatic TDS would (currently-effective rate by
   date, this company's configured account), just applied proactively instead of
   waiting on a threshold. Also re-runs client-side whenever a reviewer changes
   `existing_supplier` by hand.
3. **Manual pick**: the reviewer selects a `Tax Withholding Category` directly - this
   auto-fills rate/account and computes `tds_amount` as `extracted_taxable_amount ×
   rate` (on the pre-GST taxable amount, not the GST-inclusive total).

Saving a `Purchase Expense Center` with `tds_category` set updates the matched
Supplier's own `tax_withholding_category` to the same value automatically (unless that
Supplier is excluded per above) - so once picked, that Supplier's future invoices go
through the native path with no further manual selection needed. Invoice creation
blocks if `is_tds_applicable` is checked (manual path) but the account or amount is
missing, same as item/tax mapping. The grand-total reconciliation check accounts for
either path - it reads back whatever ERPNext actually deducted for the native case
(which may be less than expected, or nothing, depending on its own threshold logic)
rather than trusting our own pre-computed estimate.

### Multi-currency

Every field before this assumed the Company's own currency (INR); overseas suppliers
(billed in USD, etc.) need the real thing. `Purchase Expense Center` has a `currency`
field, set from extraction (Bifrost is told to infer it from the invoice, never assume
INR) and falling back to the Company's default currency if extraction doesn't return a
real 3-letter code.

At invoice creation:
- **Currency**: the Supplier's own `default_currency` (if set) takes priority over the
  extracted one - ERPNext hard-requires all of a Supplier's accounting entries to be in
  one currency once that's set, so honouring it is what avoids the
  `"Accounting Entry for Supplier: X can only be made in currency: Y"` error.
- **Conversion rate**: fetched via ERPNext's own `erpnext.setup.utils.get_exchange_rate`
  (checks a manually-recorded `Currency Exchange` rate first, then auto-fetches from the
  configured external provider) for the invoice's posting date. Invoice creation blocks
  with a clear message if no rate can be found, rather than posting at a wrong or zero
  rate.
- Item rows no longer force `base_rate`/`base_amount` to mirror `rate`/`amount` - those
  are left for `calculate_taxes_and_totals()` to derive from `conversion_rate`, which
  only equals rate/amount 1:1 when the invoice currency is the company's own.

**This does not, by itself, set up multi-currency accounting** - ERPNext also requires
the Supplier's Payable account (`credit_to`) to itself be denominated in that currency
(a separate constraint from the two points above). That's a Chart of Accounts / Supplier
setup task, not something this app creates automatically: create a currency-specific
Payable account (e.g. "Creditors USD") under your Payables group, and add it to the
Supplier's own **Accounts** table (`Supplier → Accounting tab → Default Accounts`) for
your Company. Once that's done for a given Supplier, invoices from them in that currency
work with no further setup.

### Running the tests

```bash
bench --site erp.alfaedge.org set-config allow_tests true   # first time only
bench --site erp.alfaedge.org run-tests --app alfaedge_finance
```

Covers: Bifrost markdown-fence stripping, GST- and name-based supplier matching (match /
no match / no GSTIN / GSTIN-shaped garbage rejected), TDS (native `apply_tds` vs. manual
override, Supplier category sync), item/tax mapping normalization, and Purchase Invoice
creation (blocked on missing item/tax mapping, draft-only creation, tax rows, grand-total
tolerance warning, refusing to double-create an invoice, delete/unlink behavior).

### Changelog

See [CHANGELOG.md](CHANGELOG.md) for a version history. Every user-facing change
(feature, fix, or behavior change) gets an entry there - add one as part of the same
change, not as an afterthought.

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
