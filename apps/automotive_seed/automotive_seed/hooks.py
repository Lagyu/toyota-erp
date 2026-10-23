app_name = "automotive_seed"
app_title = "Automotive Seed"
app_publisher = "Toyota ERP Clone contributors"
app_description = "Deterministic synthetic reference data (small / standard / large profiles) for non-production sites"
app_email = "erp-team@hinode.example"
app_license = "GPL-3.0-only"

required_apps = ['frappe', 'erpnext', 'automotive_core', 'automotive_manufacturing', 'automotive_supply_chain', 'automotive_portals']

# Every app prepares the test site the same way (Frappe runs the tested app's hook only).
before_tests = "automotive_core.tests.fixtures.before_tests"
