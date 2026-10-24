app_name = "automotive_portals"
app_title = "Automotive Portals"
app_publisher = "Toyota ERP Clone contributors"
app_description = "Supplier and dealer self-service portals with partner-scoped APIs"
app_email = "erp-team@hinode.example"
app_license = "GPL-3.0-only"

required_apps = ['frappe', 'erpnext', 'automotive_core', 'automotive_manufacturing', 'automotive_supply_chain']

# Every app prepares the test site the same way (Frappe runs the tested app's hook only).
before_tests = "automotive_core.tests.fixtures.before_tests"

# Website pages: Frappe's shell (sign-in, navbar, footer) at WCAG 2.1 AA (ERP-12.3).
web_include_css = ["hinode_web.bundle.css"]
web_include_js = ["hinode_web.bundle.js"]

# Partner users land on their portal after login; internal users keep the desk (a
# role-based mapping would also catch Administrator, who holds every role).
get_website_user_home_page = "automotive_portals.portal.home_page"
standard_portal_menu_items = [
	{"title": "Supplier Portal", "route": "/supplier-portal", "role": "Supplier Portal User"},
	{"title": "Dealer Portal", "route": "/dealer-portal", "role": "Dealer Portal User"},
]
