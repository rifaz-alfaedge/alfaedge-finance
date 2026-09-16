# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Contributors listed in `pyproject.toml` (`authors`).

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

[Unreleased]: https://github.com/rifaz-alfaedge/alfaedge-finance/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rifaz-alfaedge/alfaedge-finance/releases/tag/v0.1.0
