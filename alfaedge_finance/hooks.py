app_name = "alfaedge_finance"
app_title = "alfaEdge Finance"
app_publisher = "alfaEdge"
app_description = "Finance App"
app_email = "mail@alfaedge.org"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "alfaedge_finance",
# 		"logo": "/assets/alfaedge_finance/logo.png",
# 		"title": "alfaEdge Finance",
# 		"route": "/alfaedge_finance",
# 		"has_permission": "alfaedge_finance.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/alfaedge_finance/css/alfaedge_finance.css"
# app_include_js = "/assets/alfaedge_finance/js/alfaedge_finance.js"

# include js, css files in header of web template
# web_include_css = "/assets/alfaedge_finance/css/alfaedge_finance.css"
# web_include_js = "/assets/alfaedge_finance/js/alfaedge_finance.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "alfaedge_finance/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
doctype_list_js = {"Purchase Expense Center": "public/js/purchase_expense_center_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "alfaedge_finance/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "alfaedge_finance.utils.jinja_methods",
# 	"filters": "alfaedge_finance.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "alfaedge_finance.install.before_install"
# after_install = "alfaedge_finance.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "alfaedge_finance.uninstall.before_uninstall"
# after_uninstall = "alfaedge_finance.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "alfaedge_finance.utils.before_app_install"
# after_app_install = "alfaedge_finance.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "alfaedge_finance.utils.before_app_uninstall"
# after_app_uninstall = "alfaedge_finance.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "alfaedge_finance.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

doc_events = {
	"Purchase Invoice": {
		"on_submit": "alfaedge_finance.alfaedge_finance.purchase_invoice_automation.invoice_creation.sync_invoice_status",
		"on_cancel": "alfaedge_finance.alfaedge_finance.purchase_invoice_automation.invoice_creation.sync_invoice_status",
		"on_trash": "alfaedge_finance.alfaedge_finance.purchase_invoice_automation.invoice_creation.unlink_from_expense_center",
	},
}

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"alfaedge_finance.tasks.all"
# 	],
# 	"daily": [
# 		"alfaedge_finance.tasks.daily"
# 	],
# 	"hourly": [
# 		"alfaedge_finance.tasks.hourly"
# 	],
# 	"weekly": [
# 		"alfaedge_finance.tasks.weekly"
# 	],
# 	"monthly": [
# 		"alfaedge_finance.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "alfaedge_finance.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "alfaedge_finance.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "alfaedge_finance.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------
# A cancelled Purchase Invoice should be deletable even though a Purchase Expense
# Center still points to it (invoice_creation.unlink_from_expense_center clears that
# reference via on_trash) - the reverse direction (deleting a Purchase Expense Center
# while a Purchase Invoice still links to it) is intentionally NOT exempted here and
# stays blocked. Note this hook is doctype-wide: it also means deleting a Supplier/
# Item/Account still referenced by an unmapped Purchase Expense Center row would no
# longer be blocked on that account either - an acceptable trade-off since master data
# like that is meant to be disabled, not deleted (Frappe nudges towards that itself).

ignore_links_on_delete = ["Purchase Expense Center"]

# Request Events
# ----------------
# before_request = ["alfaedge_finance.utils.before_request"]
# after_request = ["alfaedge_finance.utils.after_request"]

# Job Events
# ----------
# before_job = ["alfaedge_finance.utils.before_job"]
# after_job = ["alfaedge_finance.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"alfaedge_finance.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Fixtures
# --------
# Custom Fields used by the purchase invoice automation feature: mapping a
# supplier's line-item text to an internal Item, linking a created Purchase
# Invoice back to the Purchase Expense Center it came from, and opting a
# Supplier out of ERPNext's automatic TDS.

fixtures = [
	{
		"doctype": "Custom Field",
		"filters": [
			[
				"name",
				"in",
				[
					"Purchase Invoice Item-mapped_item",
					"Purchase Invoice-purchase_expense_center",
					"Supplier-exclude_from_auto_tds",
				],
			]
		],
	},
]

