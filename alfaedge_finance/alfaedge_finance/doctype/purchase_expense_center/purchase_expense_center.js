frappe.ui.form.on("Purchase Expense Center", {
	refresh(frm) {
		const cancelled = frm.doc.invoice_status === "Invoice Cancelled";
		if (frm.doc.purchase_invoice) {
			frm.add_custom_button(cancelled ? __("View Cancelled Invoice") : __("View Invoice"), () => {
				frappe.set_route("Form", "Purchase Invoice", frm.doc.purchase_invoice);
			});
		}
		if (frm.doc.status === "Extracted" && (!frm.doc.purchase_invoice || cancelled)) {
			frm.add_custom_button(__("Create Invoice"), () => {
				frappe.call({
					method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.create_purchase_invoice",
					args: { expense_center: frm.doc.name },
					freeze: true,
					freeze_message: __("Creating draft Purchase Invoice..."),
					callback(r) {
						if (!r.exc && r.message) {
							if (r.message.warning) {
								frappe.msgprint({ title: __("Check Before Submitting"), message: r.message.warning, indicator: "orange" });
							}
							frappe.show_alert({ message: __("Draft Purchase Invoice created"), indicator: "green" });
							frm.reload_doc();
						}
					},
				});
			}).addClass("btn-primary");
		}

		if (frm.doc.status === "Failed") {
			frm.add_custom_button(__("Retry Extraction"), () => {
				frappe.call({
					method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.retry_extraction",
					args: { expense_center: frm.doc.name },
					callback(r) {
						if (!r.exc) {
							frappe.show_alert({ message: __("Re-queued for extraction"), indicator: "blue" });
							frm.reload_doc();
						}
					},
				});
			});
		}

		if (frm.doc.is_potential_duplicate) {
			frm.dashboard.add_comment(
				__("This may be a duplicate of another Purchase Expense Center with the same supplier and invoice number."),
				"orange",
				true
			);
		}
	},

	existing_supplier(frm) {
		if (!frm.doc.existing_supplier) return;

		frappe.call({
			method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.get_supplier_tds",
			args: { supplier: frm.doc.existing_supplier, posting_date: frm.doc.supplier_invoice_date },
			callback(r) {
				const tds = r.message;
				if (!tds) return;

				frm.set_value("is_tds_applicable", 1);
				frm.set_value("tds_category", tds.category);
				frm.set_value("tds_rate", tds.rate);
				if (!frm.doc.tds_account) {
					frm.set_value("tds_account", tds.account);
				}
				if (frm.doc.extracted_taxable_amount) {
					frm.set_value(
						"tds_amount",
						flt((frm.doc.extracted_taxable_amount * tds.rate) / 100, precision("tds_amount", frm.doc))
					);
				}
				frappe.show_alert({
					message: __("Tax Withholding Category {0} found on this Supplier - TDS fields pre-filled.", [tds.category]),
					indicator: "blue",
				});
			},
		});
	},

	tds_category(frm) {
		if (!frm.doc.tds_category) return;

		frappe.call({
			method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.get_tax_withholding_category_rate",
			args: { category: frm.doc.tds_category, posting_date: frm.doc.supplier_invoice_date },
			callback(r) {
				const tds = r.message;
				if (!tds) {
					frappe.msgprint(__("This Tax Withholding Category has no rate covering this invoice's date, or no account configured for this company."));
					return;
				}

				frm.set_value("is_tds_applicable", 1);
				frm.set_value("tds_rate", tds.rate);
				if (!frm.doc.tds_account) {
					frm.set_value("tds_account", tds.account);
				}
				if (frm.doc.extracted_taxable_amount) {
					frm.set_value(
						"tds_amount",
						flt((frm.doc.extracted_taxable_amount * tds.rate) / 100, precision("tds_amount", frm.doc))
					);
				}
				offer_to_save_category_on_supplier(frm);
			},
		});
	},
});

// Picking a category here only affects this invoice. Saving it on the Supplier turns on
// ERPNext's automatic TDS for all their future invoices, so it's a separate, confirmed step.
function offer_to_save_category_on_supplier(frm) {
	const supplier = frm.doc.supplier_type === "Existing" && frm.doc.existing_supplier;
	const category = frm.doc.tds_category;
	if (!supplier || !category) return;

	frappe.call({
		method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.get_supplier_tds_default",
		args: { supplier },
		callback(r) {
			const current = r.message;
			if (!current || current.exclude_from_auto_tds || current.tax_withholding_category === category) return;

			const message = current.tax_withholding_category
				? __("Change {0}'s Tax Withholding Category from {1} to {2} for all future invoices?", [
						supplier.bold(),
						current.tax_withholding_category.bold(),
						category.bold(),
				  ])
				: __("Also save {0} on {1}, so ERPNext deducts TDS automatically on all their future invoices?", [
						category.bold(),
						supplier.bold(),
				  ]);
			frappe.confirm(message, () => {
				frappe.call({
					method: "alfaedge_finance.alfaedge_finance.doctype.purchase_expense_center.purchase_expense_center.set_supplier_tds_category",
					args: { supplier, category },
					callback(res) {
						if (res.message) {
							frappe.show_alert({ message: __("Saved on Supplier {0}", [supplier]), indicator: "green" });
						}
					},
				});
			});
		},
	});
}
