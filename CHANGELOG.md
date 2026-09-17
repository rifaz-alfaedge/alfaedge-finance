# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Multi-currency support.** `Purchase Expense Center` gets a `currency` field, set
  from extraction (falling back to the Company's default currency). Invoice creation
  sets `Purchase Invoice.currency` (the Supplier's own `default_currency` takes
  priority when set - fixes `"Accounting Entry for Supplier: X can only be made in
  currency: Y"`) and fetches a real `conversion_rate` via ERPNext's own exchange-rate
  utility, blocking with a clear message if none can be found rather than posting at a
  wrong/zero rate. Item rows no longer force `base_rate`/`base_amount` to mirror
  `rate`/`amount`, letting ERPNext derive them correctly from the conversion rate.
  Confirmed live end-to-end for a USD Anthropic invoice. Still requires the Supplier's
  Payable account to itself be set up in that currency (a Chart of Accounts / Supplier
  setup task, not something this app creates automatically).
- Supplier matching now falls back to an exact, case/whitespace-insensitive match on
  `Supplier.supplier_name` when GSTIN-based matching finds nothing - covers overseas
  suppliers (no GSTIN at all) and cases where the LLM mislabels some other identifier
  as `gst_number`. A structural GSTIN-shape check (`looks_like_gstin`) now rejects
  anything that isn't actually a 15-character Indian GSTIN before it's stored or
  matched against (confirmed live: an overseas invoice produced `9924USA29003OSI` as
  `gst_number`, which isn't a GSTIN). New-supplier creation also now captures and uses
  the extracted `country` instead of always defaulting the Address to India.
- A further supplier-matching fallback, `resolve_by_name_and_address`, for when even
  exact name matching finds nothing: normalizes the name further (punctuation
  stripped too), and if that's ambiguous or empty, confirms/searches using the
  Supplier's linked Address (country, state, pincode). Each tier still requires an
  exact match on some normalized field - not fuzzy string similarity - to keep the
  false-positive risk of linking to the wrong Supplier low.
- **Alfaedge Finance workspace** in the Desk sidebar, with shortcuts and link cards to
  `Purchase Expense Center`, `Purchase Item Mapping`, `Tax Account Mapping`, and
  `Purchase Invoice Automation Settings` - so mappings can be viewed and managed
  directly, without going through a `Purchase Expense Center`.
- Item/tax mapping matching is now normalized (whitespace collapsed, lowercased)
  before being used as the lookup/storage key, so trivial LLM re-wording between two
  extractions of the same line item no longer looks unmapped. A one-time patch
  (`patches/v0_2/normalize_purchase_item_mapping_keys.py`) normalizes existing
  `Purchase Item Mapping` rows and merges any that become identical after
  normalization. Genuinely different wording (e.g. a date range or IP address that
  changes every month) still needs mapping once per distinct wording - this is
  whitespace/case normalization only, not fuzzy matching.
- TDS now uses ERPNext's own `Tax Withholding Category` doctype instead of a custom
  one, and invoice creation picks between two mechanisms per supplier:
  - **Native**: if the Supplier has a Tax Withholding Category and isn't flagged
    `Exclude from Automatic TDS` (new Supplier custom field), the Purchase Invoice gets
    `apply_tds = 1` and ERPNext computes and appends the withholding row itself,
    threshold logic included.
  - **Manual override**: for suppliers flagged excluded (e.g. OVHtech R&D, whose
    per-invoice amount never crosses ERPNext's own threshold, so native `apply_tds`
    would silently deduct nothing), TDS is detected from the invoice itself, the
    Supplier's category, or a manual pick, and appended as our own withholding row
    using the `Purchase Expense Center`'s TDS section fields - unchanged from before,
    just now gated on this flag instead of always running.
  - Picking a Tax Withholding Category on a non-excluded supplier's record now updates
    that Supplier's own field to match, so future invoices from them go native without
    needing to pick it again.
  - The grand-total reconciliation check now reads back what ERPNext actually deducted
    for the native path, rather than trusting a pre-computed estimate.

### Fixed
- A cancelled Purchase Invoice could not be deleted because its source Purchase
  Expense Center still linked to it, and vice versa - a mutual link deadlock. Deleting
  the Purchase Invoice now always succeeds and clears the link, resetting the source
  record's `invoice_status` back to `Pending Review` so "Create Invoice" reappears.
  Deleting a Purchase Expense Center while a Purchase Invoice still links to it remains
  blocked, as intended.

## [0.2.0] - 2026-09-16

### Added
- Contributors listed in `pyproject.toml` (`authors`).

### Changed
- Draft Purchase Invoice `posting_date` is now set to the supplier's own invoice date
  (previously left at today's date).
- `Purchase Expense Center.invoice_status` now tracks the actual Purchase Invoice state
  (`Invoice Draft` / `Invoice Submitted`) instead of a one-shot `Invoice Created`, kept
  in sync automatically when the invoice is submitted or cancelled.

## [0.1.0] - 2026-09-16

### Added
- **Purchase Invoice Automation**: turns a supplier-emailed or manually-uploaded PDF
  invoice into a draft Purchase Invoice.
  - `Purchase Expense Center`, `Purchase Expense Center Tax`, `Purchase Item Mapping`,
    `Tax Account Mapping`, `TDS Category`, and `Purchase Invoice Automation Settings`
    DocTypes.
  - Email webhook (`api.purchase_invoice_webhook`) and manual/bulk Desk PDF upload
    (`api.purchase_invoice_upload`), sharing one background ingestion pipeline.
  - Bifrost (LLM gateway) extraction of supplier, GST, line items, and tax breakdown
    from the PDF, running as a background job.
  - GST-based supplier matching, with automatic new-Supplier + Address creation as a
    fallback, and graceful handling of a GSTIN that fails checksum validation.
  - Global, self-learning item and tax account mapping - mapping a row by hand (or via
    the "Bulk Map Items" tool) is remembered for every future invoice.
  - TDS deduction handling: auto-detected when the invoice itself states one, or
    applied via a reviewer-picked `TDS Category`, posted the same way ERPNext's own
    automatic Tax Withholding does.
  - Draft-only Purchase Invoice creation (never auto-submitted), linked back to its
    source `Purchase Expense Center` via a custom field, with the original PDF
    attached to the invoice as well.
  - Duplicate detection, a retry-on-failure button, and a grand-total reconciliation
    warning against the invoice's own printed total.
  - 19 automated tests covering extraction parsing, GST matching, mapping sync, and
    invoice creation.

### Fixed
- Race condition where `frappe.enqueue()` could start a background job before its
  triggering record's insert had committed, on a fast worker (`enqueue_after_commit`).

## [0.0.1] - 2026-09-15

### Added
- Initial app scaffold and the pre-existing Bulk Payment CSV tool.

[Unreleased]: https://github.com/rifaz-alfaedge/alfaedge-finance/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rifaz-alfaedge/alfaedge-finance/releases/tag/v0.2.0
[0.1.0]: https://github.com/rifaz-alfaedge/alfaedge-finance/releases/tag/v0.1.0
