"""Role names shared by the schema specs (persona catalogue, ERP-10).

ERPNext already ships Auditor, Quality Manager and Sales Manager; they are reused.
Everything else is created by automotive_core.setup.roles on install/migrate.
"""

SYSTEM = "System Manager"

# Internal personas
EXECUTIVE = "Automotive Executive"
CONTROLLER = "Group Controller"
PLANT_DIRECTOR = "Plant Director"
PLANNER = "Production Planner"
SUPERVISOR = "Production Supervisor"
OPERATOR = "Line Operator"
WAREHOUSE = "Warehouse Clerk"
QUALITY_INSPECTOR = "Quality Inspector"
QUALITY_MANAGER = "Quality Manager"
BUYER = "Buyer"
PROCUREMENT_MANAGER = "Procurement Manager"
AP_CLERK = "AP Clerk"
TREASURY = "Treasury Approver"
SALES_MANAGER = "Sales Manager"
DEALER_LIAISON = "Dealer Liaison"
IT_OPERATOR = "IT Operator"
AUDITOR = "Auditor"
ENGINEER = "Product Engineer"
ENGINEERING_MANAGER = "Engineering Manager"
LOGISTICS = "Logistics Coordinator"

# External personas (website users)
SUPPLIER_PORTAL = "Supplier Portal User"
DEALER_PORTAL = "Dealer Portal User"

INTERNAL = [
	EXECUTIVE,
	CONTROLLER,
	PLANT_DIRECTOR,
	PLANNER,
	SUPERVISOR,
	OPERATOR,
	WAREHOUSE,
	QUALITY_INSPECTOR,
	QUALITY_MANAGER,
	BUYER,
	PROCUREMENT_MANAGER,
	AP_CLERK,
	TREASURY,
	SALES_MANAGER,
	DEALER_LIAISON,
	IT_OPERATOR,
	AUDITOR,
	ENGINEER,
	ENGINEERING_MANAGER,
	LOGISTICS,
]
