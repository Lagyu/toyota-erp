"""Product catalogue: item groups, attributes, vehicle templates/variants, modules,
powertrains, purchased components, the service-parts catalogue, BOMs and prices.

All records are created through ERPNext documents (Item, Item Attribute, BOM, Item
Price); variants via ERPNext's create_variant; BOM costs roll up natively.
"""

from __future__ import annotations

import itertools

import frappe
from frappe.utils import flt

from automotive_seed.generator.context import SeedContext

AICHI = "Hinode Automotive Manufacturing Japan"
KYUSHU = "Hinode Powertrain Components"
NA = "Hinode Automotive Distribution North America"

GROUPS = {
	"Vehicles": None,
	"Powertrain Assemblies": None,
	"Vehicle Modules": None,
	"Body & Exterior": "Raw Material",
	"Interior": "Raw Material",
	"Electrical & Electronics": "Raw Material",
	"Chassis & Wheels": "Raw Material",
	"Powertrain Components": "Raw Material",
	"Fasteners & Hardware": "Raw Material",
	"Fluids & Paint": "Raw Material",
	"Castings & Forgings": "Raw Material",
	"Service Parts": None,
}
COLORS = [
	("W10 Snow White", "W10"),
	("S21 Silver Mist", "S21"),
	("G32 Graphite", "G32"),
	("K40 Midnight Black", "K40"),
	("R53 Crimson", "R53"),
	("B64 Ocean Blue", "B64"),
	("N75 Sand Beige", "N75"),
	("E86 Forest Green", "E86"),
]
ATTRIBUTES = {
	"Market": [("JP", "JP"), ("US", "US")],
	"Trim": [("Standard", "S"), ("Premium", "P"), ("Luxury", "L")],
	"Drivetrain": [("FWD", "F"), ("AWD", "A")],
	"Powertrain": [("Hybrid", "HY"), ("Petrol", "PT")],
	"Package": [("Base", "B"), ("Tech", "T")],
	"Body Style": [("Cargo", "C"), ("Crew", "W")],
}
# The powertrain assembly a variant takes: by Powertrain where the model offers the choice,
# then by Drivetrain (see powertrain_of). Optional equipment comes with the Tech package.
HYBRID_OR_PETROL = {
	"Hybrid": {"FWD": "P180-FWD", "AWD": "P180-AWD"},
	"Petrol": {"FWD": "G180-FWD", "AWD": "G180-AWD"},
}
TECH_PACKAGE = [("ADAS-KIT", 1), ("AUDIO-PREM", 1)]
MODELS = [
	{
		"code": "H1",
		"name": "H1 Hybrid Compact",
		"family": "Passenger Car",
		"line": "AICHI-FA1",
		"vin": "JX9H1HC0",
		"attrs": ["Market", "Trim", "Drivetrain", "Powertrain", "Package", "Body Color"],
		"powertrain": HYBRID_OR_PETROL,
		"price": 2_980_000,
		"battery": None,
	},
	{
		"code": "H2",
		"name": "H2 Electric Crossover",
		"family": "Crossover",
		"line": "AICHI-FA1",
		"vin": "JX9H2EC0",
		"attrs": ["Market", "Trim", "Drivetrain", "Package", "Body Color"],
		"powertrain": {"FWD": "E240-S", "AWD": "E240-D"},
		"price": 4_480_000,
		"battery": "HVBATT-H2",
	},
	{
		"code": "H3",
		"name": "H3 Light Commercial Van",
		"family": "Light Commercial",
		"line": "AICHI-FA2",
		"vin": "JX9H3LV0",
		"attrs": ["Market", "Body Style", "Drivetrain", "Powertrain", "Body Color"],
		"powertrain": HYBRID_OR_PETROL,
		"price": 3_280_000,
		"battery": None,
	},
]
# code, name, group, uom, tracking (None|batch|serial), unit cost (JPY), commodity, lead time days, inspect?
PURCHASED = [
	("PNL-{m}-ROOF", "{m} roof panel", "Body & Exterior", "Nos", None, 18_000, "stamping", 5, False),
	("PNL-{m}-FLOOR", "{m} floor pan", "Body & Exterior", "Nos", None, 26_000, "stamping", 5, False),
	("PNL-{m}-SIDE", "{m} body side panel", "Body & Exterior", "Nos", None, 21_000, "stamping", 5, False),
	(
		"PNL-{m}-CLOSURE",
		"{m} closure set (hood, doors, hatch)",
		"Body & Exterior",
		"Nos",
		None,
		48_000,
		"stamping",
		6,
		False,
	),
	("TIRE-{m}", "{m} tyre", "Chassis & Wheels", "Nos", "batch", 9_500, "tires", 4, False),
	("WHEEL-{m}", "{m} alloy wheel", "Chassis & Wheels", "Nos", "batch", 14_000, "tires", 4, True),
	("GLASS-{m}", "{m} glazing set", "Body & Exterior", "Nos", "batch", 42_000, "glass", 6, True),
	(
		"AIRBAG-{m}",
		"{m} airbag module set",
		"Electrical & Electronics",
		"Nos",
		"serial",
		36_000,
		"electronics",
		10,
		True,
	),
	(
		"HEADLAMP-{m}",
		"{m} headlamp pair",
		"Electrical & Electronics",
		"Nos",
		"batch",
		31_000,
		"lighting",
		7,
		False,
	),
	("CARPET-{m}", "{m} carpet set", "Interior", "Nos", None, 8_800, "interior", 5, False),
	("HEADLINER-{m}", "{m} headliner", "Interior", "Nos", None, 7_400, "interior", 5, False),
	("SEATFRAME-{m}", "{m} seat frame set", "Interior", "Nos", "batch", 38_000, "seating", 6, True),
	("TRIMCOVER-{m}", "{m} seat trim cover set", "Interior", "Nos", "batch", 22_000, "seating", 6, False),
	("DOORTRIM-{m}", "{m} door trim set", "Interior", "Nos", None, 16_000, "interior", 5, False),
	(
		"REGULATOR-{m}",
		"{m} window regulator set",
		"Electrical & Electronics",
		"Nos",
		"batch",
		12_500,
		"motors",
		7,
		False,
	),
	("IPFRAME-{m}", "{m} instrument panel frame", "Interior", "Nos", "batch", 19_000, "plastics", 6, False),
	(
		"CLUSTER-{m}",
		"{m} meter cluster",
		"Electrical & Electronics",
		"Nos",
		"serial",
		27_000,
		"electronics",
		9,
		True,
	),
	(
		"HARN-{m}",
		"{m} wiring harness kit",
		"Electrical & Electronics",
		"Nos",
		"batch",
		58_000,
		"harness",
		8,
		True,
	),
	(
		"BUMPER-{m}",
		"{m} bumper set (painted)",
		"Body & Exterior",
		"Nos",
		"batch",
		24_000,
		"plastics",
		6,
		False,
	),
]
COMMON = [
	(
		"BATT12V",
		"12V auxiliary battery",
		"Electrical & Electronics",
		"Nos",
		"batch",
		11_000,
		"battery",
		5,
		False,
	),
	(
		"ECU-BODY",
		"Body control ECU",
		"Electrical & Electronics",
		"Nos",
		"serial",
		24_000,
		"electronics",
		12,
		True,
	),
	("FLUID-COOLANT", "Long-life coolant", "Fluids & Paint", "Litre", None, 380, "chemicals", 4, False),
	("FLUID-BRAKE", "Brake fluid DOT4", "Fluids & Paint", "Litre", None, 640, "chemicals", 4, False),
	("FLUID-WASHER", "Washer fluid", "Fluids & Paint", "Litre", None, 120, "chemicals", 4, False),
	(
		"FLUID-OIL",
		"Engine / transaxle oil 0W-16",
		"Fluids & Paint",
		"Litre",
		None,
		520,
		"chemicals",
		4,
		False,
	),
	("PRIMER", "Electro-deposition primer", "Fluids & Paint", "Kg", "batch", 1_150, "paint", 6, False),
	("CLEARCOAT", "2K clear coat", "Fluids & Paint", "Kg", "batch", 1_480, "paint", 6, False),
	("WELD-WIRE", "Spot weld consumables", "Fluids & Paint", "Kg", None, 900, "chemicals", 5, False),
	("SEALER", "Body seam sealer", "Fluids & Paint", "Kg", "batch", 760, "chemicals", 5, False),
	("FOAM", "Seat polyurethane foam", "Interior", "Kg", "batch", 950, "seating", 5, True),
	(
		"CAST-BLOCK",
		"Engine block casting (aluminium)",
		"Castings & Forgings",
		"Nos",
		"batch",
		34_000,
		"castings",
		12,
		True,
	),
	(
		"CAST-HEAD",
		"Cylinder head casting",
		"Castings & Forgings",
		"Nos",
		"batch",
		22_000,
		"castings",
		12,
		True,
	),
	(
		"FORGE-GEAR",
		"Reduction gear forging",
		"Castings & Forgings",
		"Nos",
		"batch",
		9_800,
		"forgings",
		10,
		True,
	),
	(
		"CAST-HOUSING",
		"E-drive housing casting",
		"Castings & Forgings",
		"Nos",
		"batch",
		18_000,
		"castings",
		12,
		True,
	),
	("CRANKSHAFT", "Crankshaft", "Powertrain Components", "Nos", "batch", 16_000, "forgings", 10, True),
	(
		"PISTON-KIT",
		"Piston and rod kit",
		"Powertrain Components",
		"Nos",
		"batch",
		12_000,
		"machining",
		9,
		False,
	),
	("VALVETRAIN", "Valvetrain kit", "Powertrain Components", "Nos", "batch", 14_500, "machining", 9, False),
	("INJECTOR-KIT", "Injector kit", "Powertrain Components", "Nos", "batch", 9_600, "electronics", 9, False),
	(
		"TRANSAXLE-HEV",
		"Hybrid transaxle",
		"Powertrain Components",
		"Nos",
		"serial",
		168_000,
		"machining",
		14,
		True,
	),
	(
		"INVERTER-P180",
		"P180 power control unit",
		"Powertrain Components",
		"Nos",
		"serial",
		96_000,
		"electronics",
		14,
		True,
	),
	(
		"HVBATT-P180",
		"P180 hybrid battery pack",
		"Powertrain Components",
		"Nos",
		"serial",
		142_000,
		"battery",
		14,
		True,
	),
	(
		"REAR-EMOTOR",
		"Rear e-motor (AWD)",
		"Powertrain Components",
		"Nos",
		"serial",
		88_000,
		"motors",
		14,
		True,
	),
	("STATOR-240", "E240 stator", "Powertrain Components", "Nos", "batch", 46_000, "motors", 12, True),
	("ROTOR-240", "E240 rotor", "Powertrain Components", "Nos", "batch", 38_000, "motors", 12, True),
	(
		"INVERTER-E240",
		"E240 inverter",
		"Powertrain Components",
		"Nos",
		"serial",
		112_000,
		"electronics",
		14,
		True,
	),
	(
		"HVBATT-H2",
		"H2 traction battery pack",
		"Electrical & Electronics",
		"Nos",
		"serial",
		980_000,
		"battery",
		21,
		True,
	),
	(
		"TRANSAXLE-AT",
		"Automatic transaxle",
		"Powertrain Components",
		"Nos",
		"serial",
		132_000,
		"machining",
		14,
		True,
	),
	(
		"AWD-DRIVELINE",
		"Propeller shaft and rear differential (AWD)",
		"Powertrain Components",
		"Nos",
		"serial",
		64_000,
		"forgings",
		14,
		True,
	),
	(
		"FUEL-SYS-G180",
		"G180 fuel system kit",
		"Powertrain Components",
		"Nos",
		"batch",
		21_000,
		"machining",
		9,
		False,
	),
	(
		"ADAS-KIT",
		"Camera and radar sensor kit",
		"Electrical & Electronics",
		"Nos",
		"batch",
		86_000,
		"electronics",
		9,
		True,
	),
	(
		"AUDIO-PREM",
		"Premium audio system",
		"Electrical & Electronics",
		"Nos",
		"batch",
		42_000,
		"electronics",
		9,
		False,
	),
]
MODULE_PARTS = {  # modules built at Aichi from purchased parts; code template -> lines
	"BIW-{m}": [
		("PNL-{m}-ROOF", 1),
		("PNL-{m}-FLOOR", 1),
		("PNL-{m}-SIDE", 2),
		("PNL-{m}-CLOSURE", 1),
		("WELD-WIRE", 1.6),
		("SEALER", 1.1),
	],
	"SEAT-{m}-{t}": [("SEATFRAME-{m}", 1), ("TRIMCOVER-{m}", 1), ("FOAM", 3.4)],
	"DOOR-{m}-{t}": [("DOORTRIM-{m}", 1), ("REGULATOR-{m}", 1)],
	"IP-{m}-{k}": [("IPFRAME-{m}", 1), ("CLUSTER-{m}", 1)],
	"FAST-KIT-{m}": [],  # filled from the fastener catalogue
}
BULK_FAMILIES = [
	(
		"BOLT",
		"Hex flange bolt",
		"Fasteners & Hardware",
		"fasteners",
		["M5", "M6", "M8", "M10", "M12", "M14"],
		["12", "16", "20", "25", "30", "35", "40", "50", "60", "70", "80"],
		["8.8", "10.9"],
		18,
	),
	(
		"NUT",
		"Flange nut",
		"Fasteners & Hardware",
		"fasteners",
		["M5", "M6", "M8", "M10", "M12", "M14"],
		["STD", "LOCK"],
		["ZN", "ZNNI", "GEO"],
		9,
	),
	(
		"CLIP",
		"Trim clip",
		"Fasteners & Hardware",
		"plastics",
		[f"T{i:02d}" for i in range(1, 41)],
		["BK", "GY", "IV"],
		["A", "B"],
		4,
	),
	(
		"CONN",
		"Wire connector",
		"Electrical & Electronics",
		"harness",
		[f"{p}P" for p in (2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 32, 40)],
		["SEAL", "UNSEAL"],
		["090", "150", "250"],
		85,
	),
	(
		"HOSE",
		"Hose assembly",
		"Powertrain Components",
		"rubber",
		[f"D{d}" for d in (6, 8, 10, 12, 16, 19, 25, 32)],
		[f"L{l}" for l in (150, 250, 350, 500, 750, 1000)],
		["EPDM", "SIL"],
		640,
	),
	(
		"SEAL",
		"O-ring / seal",
		"Powertrain Components",
		"rubber",
		[f"ID{d}" for d in range(4, 64, 3)],
		["NBR", "FKM", "EPDM"],
		["70A", "90A"],
		45,
	),
	(
		"BRKT",
		"Bracket",
		"Body & Exterior",
		"stamping",
		[f"B{i:03d}" for i in range(1, 201)],
		["L", "R", "C"],
		["STL", "ALU"],
		320,
	),
	(
		"TRIM",
		"Interior trim panel (service)",
		"Service Parts",
		"interior",
		[f"{m}-{p:02d}" for m in ("H1", "H2", "H3") for p in range(1, 31)],
		["W10", "S21", "G32", "K40", "R53", "B64", "N75", "E86"],
		["R1", "R2", "R3"],
		2_400,
	),
	(
		"EPART",
		"Electronic module (service)",
		"Service Parts",
		"electronics",
		[f"{m}-{p:02d}" for m in ("H1", "H2", "H3") for p in range(1, 41)],
		["SW1", "SW2", "SW3", "SW4"],
		["A", "B"],
		5_200,
	),
	(
		"SUBH",
		"Sub-harness",
		"Electrical & Electronics",
		"harness",
		[f"{m}-{p:02d}" for m in ("H1", "H2", "H3") for p in range(1, 51)],
		["RHD", "LHD"],
		["V1", "V2"],
		1_800,
	),
	(
		"LABEL",
		"Label / emblem",
		"Service Parts",
		"plastics",
		[f"L{i:03d}" for i in range(1, 201)],
		["JP", "US", "EU"],
		["V1", "V2"],
		150,
	),
	(
		"GSKT",
		"Gasket",
		"Powertrain Components",
		"rubber",
		[f"K{i:03d}" for i in range(1, 101)],
		["MLS", "RUB", "PAPER"],
		["V1", "V2"],
		320,
	),
	(
		"BULB",
		"Lamp bulb / LED",
		"Electrical & Electronics",
		"lighting",
		["T10", "T16", "T20", "H11", "H8", "W5W", "P21W"],
		["12V", "24V"],
		["HAL", "LED"],
		260,
	),
	(
		"BEAR",
		"Bearing",
		"Powertrain Components",
		"machining",
		[f"60{i:02d}" for i in range(0, 40)],
		["2RS", "ZZ", "OPEN"],
		["C3", "CN"],
		980,
	),
	(
		"FUSE",
		"Fuse",
		"Electrical & Electronics",
		"harness",
		["MINI", "ATO", "MAXI", "JCASE"],
		["5A", "7.5A", "10A", "15A", "20A", "25A", "30A", "40A", "50A"],
		["STD"],
		35,
	),
	(
		"GROM",
		"Grommet",
		"Body & Exterior",
		"rubber",
		[f"G{i:02d}" for i in range(10, 70, 2)],
		["STD", "SEALED"],
		["BK"],
		22,
	),
	(
		"SCREW",
		"Tapping screw",
		"Fasteners & Hardware",
		"fasteners",
		["ST3.5", "ST4.2", "ST4.8", "ST5.5"],
		["10", "13", "16", "19", "22", "25"],
		["PH", "TX", "HEX"],
		6,
	),
	(
		"WASHER",
		"Washer",
		"Fasteners & Hardware",
		"fasteners",
		["M5", "M6", "M8", "M10", "M12", "M14", "M16"],
		["FLAT", "SPRING", "CONTACT"],
		["ZN", "SS"],
		3,
	),
	(
		"PIPE",
		"Brake / fuel pipe",
		"Chassis & Wheels",
		"steel",
		[f"D{d}" for d in (4.76, 6.35, 8, 10)],
		[f"L{l}" for l in range(300, 3300, 150)],
		["ZNAL", "PA12"],
		420,
	),
]


def _ensure_group(name: str, parent: str | None) -> None:
	if not frappe.db.exists("Item Group", name):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": name,
				"parent_item_group": parent or "All Item Groups",
				"is_group": 0,
			}
		).insert(ignore_permissions=True)


# Inspection plans (ERP-07.1): template -> parameters. A measured parameter has its acceptance
# range; a check (low None) passes with the value "OK". ERPNext accepts or rejects an
# inspection from its readings.
INSPECTION_TEMPLATES = {
	"Incoming - Metal Parts": [
		("Critical dimension deviation (mm)", -0.05, 0.05),
		("Hardness (HRC)", 28, 36),
		("Surface and burrs", None, None),
	],
	"Incoming - Electrical": [
		("Insulation resistance (MOhm)", 100, 1_000),
		("Contact resistance (Ohm)", 0, 0.5),
		("Functional test", None, None),
	],
	"Incoming - Trim and Polymer": [
		("Hardness (Shore A)", 60, 80),
		("Colour difference (dE)", 0, 1.0),
		("Appearance", None, None),
	],
	"Incoming - Fluids and Paint": [
		("Viscosity (mPa s)", 85, 115),
		("Solids content (%)", 42, 48),
		("Contamination", None, None),
	],
	"Final - Vehicle": [
		("Wheel alignment toe (mm)", -1.0, 1.0),
		("Headlamp aim (%)", -2.0, -0.5),
		("Brake force imbalance (%)", 0, 20),
		("Water leak test", None, None),
		("Road test", None, None),
	],
	"Final - Powertrain": [
		("Hot test power (kW)", 145, 175),
		("Oil pressure at idle (kPa)", 300, 450),
		("Leak rate (cc/min)", 0, 5),
		("NVH check", None, None),
	],
	"Pre-Delivery - Vehicle": [
		("Battery voltage (V)", 12.2, 12.9),
		("Paint and body", None, None),
		("Accessory fit", None, None),
	],
}
COMMODITY_TEMPLATE = {
	**dict.fromkeys(
		("stamping", "castings", "forgings", "machining", "fasteners", "steel", "tires"),
		"Incoming - Metal Parts",
	),
	**dict.fromkeys(("electronics", "lighting", "motors", "harness", "battery"), "Incoming - Electrical"),
	**dict.fromkeys(("glass", "interior", "seating", "plastics", "rubber"), "Incoming - Trim and Polymer"),
	**dict.fromkeys(("chemicals", "paint"), "Incoming - Fluids and Paint"),
}


# Kyushu routings (ERP-04.1): item -> (line, [(station, operation, minutes per unit)]). A work order
# gets one job card per operation for its whole quantity.
_P180_POWERTRAIN = (
	"KYU-P180",
	[("P20", "Hybrid transaxle fit", 8), ("P30", "Inverter fit", 6), ("P40", "Hot test", 10)],
)
# the petrol powertrain on the same line's stations: a conventional transaxle, no inverter
_G180_POWERTRAIN = (
	"KYU-P180",
	[("P20", "Transaxle fit", 7), ("P30", "Fuel system fit", 5), ("P40", "Hot test", 10)],
)
_E240_DRIVE = (
	"KYU-E240",
	[
		("E10", "Stator insert", 6),
		("E20", "Rotor and reduction gear", 7),
		("E30", "Inverter integration", 6),
		("E40", "End-of-line test", 8),
	],
)
ROUTINGS = {
	"BLOCK-MACH": ("KYU-MACH", [("M10", "Block machining", 6), ("M40", "CMM inspection", 2)]),
	"HEAD-MACH": ("KYU-MACH", [("M20", "Head machining", 5), ("M40", "CMM inspection", 2)]),
	"GEARSET-240": ("KYU-MACH", [("M30", "Gear cutting", 4), ("M40", "CMM inspection", 2)]),
	"HOUSING-240": ("KYU-MACH", [("M10", "Housing machining", 5), ("M40", "CMM inspection", 2)]),
	"ENG-P180": ("KYU-P180", [("P10", "Engine dress", 12)]),
	"P180-FWD": _P180_POWERTRAIN,
	"P180-AWD": _P180_POWERTRAIN,
	"G180-FWD": _G180_POWERTRAIN,
	"G180-AWD": _G180_POWERTRAIN,
	"E240-S": _E240_DRIVE,
	"E240-D": _E240_DRIVE,
}


def routing(item: str) -> tuple[str | None, list[dict]]:
	"""The item's Routing (created on first use) and the BOM operations it gives."""
	if item not in ROUTINGS:
		return None, []
	line, steps = ROUTINGS[item]
	operations = []
	for seq, (station, label, minutes) in enumerate(steps, start=1):
		workstation = f"{line}-{station}"
		operation = f"{station} {label}"
		if not frappe.db.exists("Operation", operation):
			frappe.get_doc(
				{
					"doctype": "Operation",
					"__newname": operation,
					"workstation": workstation,
					"description": f"{label} at {workstation}",
				}
			).insert(ignore_permissions=True)
		operations.append(
			{"operation": operation, "workstation": workstation, "time_in_mins": minutes, "sequence_id": seq}
		)
	name = f"Routing {item}"
	if not frappe.db.exists("Routing", name):
		frappe.get_doc({"doctype": "Routing", "routing_name": name, "operations": operations}).insert(
			ignore_permissions=True
		)
	return name, operations


def inspection_templates() -> None:
	for template, parameters in INSPECTION_TEMPLATES.items():
		for name, _low, _high in parameters:
			if not frappe.db.exists("Quality Inspection Parameter", name):
				frappe.get_doc({"doctype": "Quality Inspection Parameter", "parameter": name}).insert(
					ignore_permissions=True
				)
		if not frappe.db.exists("Quality Inspection Template", template):
			frappe.get_doc(
				{
					"doctype": "Quality Inspection Template",
					"quality_inspection_template_name": template,
					"item_quality_inspection_parameter": [
						{"specification": name, "numeric": 1, "min_value": low, "max_value": high}
						if low is not None
						else {"specification": name, "numeric": 0, "value": "OK"}
						for name, low, high in parameters
					],
				}
			).insert(ignore_permissions=True)


def _ensure_attribute(name: str, values: list[tuple[str, str]]) -> None:
	if frappe.db.exists("Item Attribute", name):
		doc = frappe.get_doc("Item Attribute", name)
		have = {v.attribute_value for v in doc.item_attribute_values}
		missing = [v for v in values if v[0] not in have]
		if missing:
			for value, abbr in missing:
				doc.append("item_attribute_values", {"attribute_value": value, "abbr": abbr})
			doc.save(ignore_permissions=True)
		return
	frappe.get_doc(
		{
			"doctype": "Item Attribute",
			"attribute_name": name,
			"item_attribute_values": [{"attribute_value": v, "abbr": a} for v, a in values],
		}
	).insert(ignore_permissions=True)


def _defaults(company_warehouses: dict[str, str]) -> list[dict]:
	return [{"company": company, "default_warehouse": wh} for company, wh in company_warehouses.items()]


def _item(
	ctx: SeedContext,
	code: str,
	name: str,
	group: str,
	uom: str = "Nos",
	tracking: str | None = None,
	cost: float = 0,
	warehouses: dict[str, str] | None = None,
	**extra,
) -> str:
	if frappe.db.exists("Item", code):
		return code
	values = {
		"doctype": "Item",
		"item_code": code,
		"item_name": name[:140],
		"item_group": group,
		"stock_uom": uom,
		"is_stock_item": 1,
		"valuation_rate": cost,
		"automotive_standard_cost": cost,
		"include_item_in_manufacturing": 1,
		"item_defaults": _defaults(warehouses or {}),
		**extra,
	}
	if tracking == "batch":
		values.update(
			{"has_batch_no": 1, "create_new_batch": 1, "batch_number_series": f"{code[:12]}-.YY.-.#####"}
		)
	elif tracking == "serial":
		values.update({"has_serial_no": 1, "serial_no_series": f"{code[:12]}-.#######"})
	frappe.get_doc(values).insert(ignore_permissions=True)
	ctx.count("Item")
	return code


def warehouses() -> dict[str, dict[str, str]]:
	plants = {
		p.name: p
		for p in frappe.get_all(
			"Plant",
			fields=[
				"name",
				"company",
				"raw_material_warehouse",
				"finished_goods_warehouse",
				"wip_warehouse",
				"receiving_warehouse",
			],
		)
	}
	lines = {
		ln.name: ln.line_side_warehouse
		for ln in frappe.get_all("Production Line", fields=["name", "line_side_warehouse"])
	}
	return {"plants": plants, "lines": lines}


def build(ctx: SeedContext) -> dict:
	"""Create the whole catalogue; returns the structure the simulation uses."""
	for name, parent in GROUPS.items():
		_ensure_group(name, parent)
	inspection_templates()
	colors = COLORS[: int(ctx.profile["catalog"]["colors"])]
	for attribute, values in ATTRIBUTES.items():
		_ensure_attribute(attribute, values)
	_ensure_attribute("Body Color", colors)
	wh = warehouses()
	aichi, kyushu = wh["plants"]["AICHI-VA"], wh["plants"]["KYUSHU-PT"]
	aichi_store = {AICHI: aichi.raw_material_warehouse}
	kyushu_store = {KYUSHU: kyushu.raw_material_warehouse}
	structure: dict = {
		"purchased": {},
		"modules": {},
		"powertrains": {},
		"variants": {},
		"boms": {},
		"colors": colors,
	}

	def purchased(code, name, group, uom, tracking, cost, commodity, lead, inspect, stores):
		_item(
			ctx,
			code,
			name,
			group,
			uom,
			tracking,
			cost,
			stores,
			is_purchase_item=1,
			lead_time_days=lead,
			inspection_required_before_purchase=int(inspect),
			min_order_qty=1,
			quality_inspection_template=COMMODITY_TEMPLATE.get(commodity) if inspect else None,
		)
		structure["purchased"][code] = {
			"commodity": commodity,
			"cost": cost,
			"lead": lead,
			"uom": uom,
			"tracking": tracking,
			"inspect": inspect,
			"plant": "KYUSHU-PT" if stores is kyushu_store else "AICHI-VA",
		}

	pt_codes = {
		"CAST-",
		"FORGE-",
		"CRANKSHAFT",
		"PISTON-",
		"VALVETRAIN",
		"INJECTOR-",
		"TRANSAXLE",
		"INVERTER-",
		"HVBATT-P",
		"REAR-EMOTOR",
		"AWD-DRIVELINE",
		"FUEL-SYS",
		"STATOR",
		"ROTOR",
		"FLUID-OIL",
	}
	for code, name, group, uom, tracking, cost, commodity, lead, inspect in COMMON:
		stores = kyushu_store if any(code.startswith(p) for p in pt_codes) else aichi_store
		purchased(code, name, group, uom, tracking, cost, commodity, lead, inspect, stores)
	for model in MODELS:
		for code, name, group, uom, tracking, cost, commodity, lead, inspect in PURCHASED:
			purchased(
				code.format(m=model["code"]),
				name.format(m=model["code"]),
				group,
				uom,
				tracking,
				cost * (1.25 if model["code"] == "H2" else 1.0),
				commodity,
				lead,
				inspect,
				aichi_store,
			)
		for color, abbr in colors:
			purchased(
				f"PAINT-{abbr}",
				f"Top coat {color}",
				"Fluids & Paint",
				"Kg",
				"batch",
				1_850,
				"paint",
				6,
				False,
				aichi_store,
			)
	_bulk_catalogue(ctx, structure, aichi_store, kyushu_store)
	_powertrains(ctx, structure, kyushu)
	_modules(ctx, structure, aichi, wh)
	_vehicles(ctx, structure, aichi, wh, colors)
	frappe.db.commit()
	return structure


def _bulk_catalogue(ctx, structure, aichi_store, kyushu_store) -> None:
	target = int(ctx.profile["catalog"]["bulk_component_items"])
	made = 0
	fasteners: list[str] = []

	def combinations():
		"""Every family combination; further passes are later engineering revisions of the
		same parts (service parts are superseded over a model's life)."""
		revision = 0
		while True:
			for family, label, group, commodity, a, b, c, cost in BULK_FAMILIES:
				for x, y, z in itertools.product(a, b, c):
					yield revision, family, label, group, commodity, x, y, z, cost
			revision += 1

	for revision, family, label, group, commodity, x, y, z, cost in combinations():
		if made >= target:
			break
		suffix = f"-R{revision}" if revision else ""
		code = (f"{family}-{x}-{y}-{z}".replace(".", "") + suffix)[:60]
		name = f"{label} {x} {y} {z}" + (f" rev {revision}" if revision else "")
		stores = kyushu_store if group == "Powertrain Components" else aichi_store
		_item(
			ctx,
			code,
			name,
			group,
			"Nos",
			None,
			cost,
			stores,
			is_purchase_item=1,
			lead_time_days=7,
			min_order_qty=100,
		)
		structure["purchased"][code] = {
			"commodity": commodity,
			"cost": cost,
			"lead": 7,
			"uom": "Nos",
			"tracking": None,
			"inspect": False,
			"plant": "KYUSHU-PT" if stores is kyushu_store else "AICHI-VA",
			"catalogue": True,
		}
		if family in ("BOLT", "NUT", "SCREW", "WASHER", "CLIP") and not revision:
			fasteners.append(code)
		made += 1
		if made % 500 == 0:
			frappe.db.commit()
	structure["fasteners"] = fasteners


def _bom(
	ctx, item: str, company: str, lines: list[tuple[str, float]], operations_line: str | None = None
) -> str:
	existing = frappe.db.get_value(
		"BOM", {"item": item, "docstatus": 1, "is_active": 1, "is_default": 1}, "name"
	)
	if existing:
		return existing
	route, operations = routing(item)
	bom = frappe.get_doc(
		{
			"doctype": "BOM",
			"item": item,
			"company": company,
			"quantity": 1,
			"is_default": 1,
			"is_active": 1,
			"rm_cost_as_per": "Valuation Rate",
			"automotive_effective_from": ctx.start.isoformat(),
			"with_operations": int(bool(operations)),
			"routing": route,
			"operations": operations,
			"items": [
				{
					"item_code": code,
					"qty": flt(qty, 6),
					"automotive_net_qty": flt(qty, 6),
					"automotive_critical": int(bool(frappe.get_cached_value("Item", code, "has_serial_no"))),
				}
				for code, qty in lines
			],
		}
	)
	bom.insert(ignore_permissions=True)
	bom.submit()
	ctx.count("BOM")
	cost = flt(bom.total_cost)
	frappe.db.set_value(
		"Item", item, {"automotive_standard_cost": cost, "valuation_rate": cost}, update_modified=False
	)
	return bom.name


def _powertrains(ctx, structure, kyushu) -> None:
	fg = {
		KYUSHU: kyushu.finished_goods_warehouse,
		AICHI: frappe.db.get_value("Plant", "AICHI-VA", "raw_material_warehouse"),
	}
	line_side = {
		ln: frappe.db.get_value("Production Line", ln, "line_side_warehouse")
		for ln in ("KYU-MACH", "KYU-P180", "KYU-E240")
	}
	machined = {
		"BLOCK-MACH": ("Machined engine block", "CAST-BLOCK"),
		"HEAD-MACH": ("Machined cylinder head", "CAST-HEAD"),
		"GEARSET-240": ("E240 reduction gear set", "FORGE-GEAR"),
		"HOUSING-240": ("E240 housing (machined)", "CAST-HOUSING"),
	}
	for code, (name, raw) in machined.items():
		_item(ctx, code, name, "Vehicle Modules", "Nos", "batch", 0, {KYUSHU: line_side["KYU-P180"]})
		structure["modules"][code] = {
			"plant": "KYUSHU-PT",
			"line": "KYU-MACH",
			"bom": _bom(ctx, code, KYUSHU, [(raw, 1)]),
		}
	_item(
		ctx,
		"ENG-P180",
		"P180 engine assembly",
		"Vehicle Modules",
		"Nos",
		"serial",
		0,
		{KYUSHU: line_side["KYU-P180"]},
		serial_no_series="ENGP180-.#######",
	)
	structure["modules"]["ENG-P180"] = {
		"plant": "KYUSHU-PT",
		"line": "KYU-P180",
		"bom": _bom(
			ctx,
			"ENG-P180",
			KYUSHU,
			[
				("BLOCK-MACH", 1),
				("HEAD-MACH", 1),
				("CRANKSHAFT", 1),
				("PISTON-KIT", 1),
				("VALVETRAIN", 1),
				("INJECTOR-KIT", 1),
				("FLUID-OIL", 4.2),
			],
		),
	}
	common = [("TRANSAXLE-HEV", 1), ("INVERTER-P180", 1), ("HVBATT-P180", 1), ("FLUID-OIL", 3.5)]
	for code, name, extra in (
		("P180-FWD", "P180 hybrid powertrain FWD", []),
		("P180-AWD", "P180 hybrid powertrain AWD", [("REAR-EMOTOR", 1)]),
	):
		_item(
			ctx,
			code,
			name,
			"Powertrain Assemblies",
			"Nos",
			"serial",
			0,
			fg,
			serial_no_series=f"{code.replace('-', '')}-.######",
			quality_inspection_template="Final - Powertrain",
		)
		structure["powertrains"][code] = {
			"line": "KYU-P180",
			"engine": True,
			"bom": _bom(ctx, code, KYUSHU, [("ENG-P180", 1), *common, *extra]),
		}
	# the petrol powertrain: the same engine with a conventional automatic transaxle
	petrol = [("TRANSAXLE-AT", 1), ("FUEL-SYS-G180", 1), ("FLUID-OIL", 3.5)]
	for code, name, extra in (
		("G180-FWD", "G180 petrol powertrain FWD", []),
		("G180-AWD", "G180 petrol powertrain AWD", [("AWD-DRIVELINE", 1)]),
	):
		_item(
			ctx,
			code,
			name,
			"Powertrain Assemblies",
			"Nos",
			"serial",
			0,
			fg,
			serial_no_series=f"{code.replace('-', '')}-.######",
			quality_inspection_template="Final - Powertrain",
		)
		structure["powertrains"][code] = {
			"line": "KYU-P180",
			"engine": True,
			"bom": _bom(ctx, code, KYUSHU, [("ENG-P180", 1), *petrol, *extra]),
		}
	for code, name, motors in (
		("E240-S", "E240 electric drive single motor", 1),
		("E240-D", "E240 electric drive dual motor", 2),
	):
		_item(
			ctx,
			code,
			name,
			"Powertrain Assemblies",
			"Nos",
			"serial",
			0,
			fg,
			serial_no_series=f"{code.replace('-', '')}-.######",
			quality_inspection_template="Final - Powertrain",
		)
		structure["powertrains"][code] = {
			"line": "KYU-E240",
			"bom": _bom(
				ctx,
				code,
				KYUSHU,
				[
					("STATOR-240", motors),
					("ROTOR-240", motors),
					("GEARSET-240", motors),
					("HOUSING-240", 1),
					("INVERTER-E240", motors),
				],
			),
		}


def _modules(ctx, structure, aichi, wh) -> None:
	fasteners = structure.get("fasteners") or []
	rng = ctx.rng("fastener-kits")
	for model in MODELS:
		m = model["code"]
		line = model["line"]
		line_side = wh["lines"][line]
		body_line_side = wh["lines"]["AICHI-BODY"]
		trims = ["Cargo", "Crew"] if m == "H3" else ["Standard", "Premium", "Luxury"]
		specs = [
			(
				f"BIW-{m}",
				f"{m} body in white",
				body_line_side,
				"AICHI-BODY",
				[(c.format(m=m), q) for c, q in MODULE_PARTS["BIW-{m}"]],
			)
		]
		for t in trims:
			specs.append(
				(
					f"SEAT-{m}-{t[:2].upper()}",
					f"{m} seat set {t}",
					line_side,
					line,
					[
						(
							c.format(m=m),
							q * (0.6 if t == "Cargo" and c == "FOAM" else 1),
						)  # cargo: front row only
						for c, q in MODULE_PARTS["SEAT-{m}-{t}"]
					],
				)
			)
			specs.append(
				(
					f"DOOR-{m}-{t[:2].upper()}",
					f"{m} door module set {t}",
					line_side,
					line,
					[(c.format(m=m), q) for c, q in MODULE_PARTS["DOOR-{m}-{t}"]],
				)
			)
		for hand in ("RHD", "LHD"):
			specs.append(
				(
					f"IP-{m}-{hand}",
					f"{m} instrument panel {hand}",
					line_side,
					line,
					[(c.format(m=m), q) for c, q in MODULE_PARTS["IP-{m}-{k}"]],
				)
			)
		kit = sorted(rng.sample(fasteners, min(12, len(fasteners)))) if fasteners else []
		specs.append(
			(
				f"FAST-KIT-{m}",
				f"{m} fastener kit",
				line_side,
				line,
				[(f, rng.choice((4, 6, 8, 12))) for f in kit],
			)
		)
		for code, name, store, build_line, lines in specs:
			if not lines:
				continue
			_item(ctx, code, name, "Vehicle Modules", "Nos", "batch", 0, {AICHI: store})
			structure["modules"][code] = {
				"plant": "AICHI-VA",
				"line": build_line,
				"bom": _bom(ctx, code, AICHI, lines),
			}


def _variant_lines(model, sel, colors) -> list[tuple[str, float]]:
	m = model["code"]
	trim = sel.get("Trim") or sel.get("Body Style")
	hand = "RHD" if sel["Market"] == "JP" else "LHD"
	color = next(abbr for name, abbr in colors if name == sel["Body Color"])
	lines = [
		(f"BIW-{m}", 1),
		(f"PAINT-{color}", 2.4),
		("PRIMER", 1.8),
		("CLEARCOAT", 1.2),
		(f"SEAT-{m}-{trim[:2].upper()}", 1),
		(f"DOOR-{m}-{trim[:2].upper()}", 1),
		(f"IP-{m}-{hand}", 1),
		(f"HARN-{m}", 1),
		(powertrain_of(model, sel), 1),
		(f"TIRE-{m}", 4),
		(f"WHEEL-{m}", 4),
		(f"GLASS-{m}", 1),
		("BATT12V", 1),
		(f"AIRBAG-{m}", 1),
		("ECU-BODY", 1),
		(f"HEADLAMP-{m}", 1),
		(f"BUMPER-{m}", 1),
		(f"CARPET-{m}", 1),
		(f"HEADLINER-{m}", 1),
		("FLUID-COOLANT", 6),
		("FLUID-BRAKE", 1),
		("FLUID-WASHER", 2.5),
		(f"FAST-KIT-{m}", 1),
	]
	if model["battery"]:
		lines.append((model["battery"], 1))
	if sel.get("Package") == "Tech":
		lines += TECH_PACKAGE
	return lines


def powertrain_of(model: dict, sel: dict) -> str:
	"""The powertrain assembly of a configuration: by its Powertrain where the model offers
	the choice, then by its Drivetrain."""
	choices = model["powertrain"]
	if "Powertrain" in sel:
		choices = choices[sel["Powertrain"]]
	return choices[sel["Drivetrain"]]


def _vehicles(ctx, structure, aichi, wh, colors) -> None:
	from erpnext.controllers.item_variant import create_variant

	fg = {
		AICHI: aichi.finished_goods_warehouse,
		NA: frappe.db.get_value("Plant", "NA-PDC", "finished_goods_warehouse"),
	}
	for model in MODELS:
		m = model["code"]
		if not frappe.db.exists("Item", m):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": m,
					"item_name": model["name"],
					"item_group": "Vehicles",
					# purchase item too: the North American distributor buys vehicles from Aichi
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"has_variants": 1,
					"has_serial_no": 1,
					"is_purchase_item": 1,
					"quality_inspection_template": "Final - Vehicle",
					"attributes": [{"attribute": a} for a in model["attrs"]],
					"item_defaults": _defaults(fg),
				}
			).insert(ignore_permissions=True)
			ctx.count("Item")
		if not frappe.db.exists("Vehicle Model", m):
			frappe.get_doc(
				{
					"doctype": "Vehicle Model",
					"model_code": m,
					"model_name": model["name"],
					"product_family": model["family"],
					"template_item": m,
					"default_plant": "AICHI-VA",
					"production_line": model["line"],
					"vin_prefix": model["vin"],
					"warranty_months": 36,
					"start_of_production": "2024-04-01",
					"option_groups": [
						{"attribute": a, "required": 1, "display_order": i}
						for i, a in enumerate(model["attrs"])
					],
				}
			).insert(ignore_permissions=True)
			frappe.db.set_value("Item", m, "automotive_vehicle_model", m)
		_option_rules(model, colors)
		values = [[v for v, _ in (ATTRIBUTES.get(a) or colors)] for a in model["attrs"]]
		for combo in itertools.product(*values):
			sel = dict(zip(model["attrs"], combo, strict=True))
			if not _allowed(model, sel):
				continue
			from automotive_manufacturing.product_engineering.configuration import find_variant

			code = find_variant(m, sel)
			if not code:
				variant = create_variant(m, sel)
				variant.automotive_vehicle_model = m
				variant.quality_inspection_template = "Final - Vehicle"
				variant.flags.ignore_permissions = True
				variant.insert()
				code = variant.name
				ctx.count("Item")
			bom = _bom(ctx, code, AICHI, _variant_lines(model, sel, colors))
			structure["variants"][code] = {"model": m, "selections": sel, "bom": bom, "line": model["line"]}
		frappe.db.commit()


def _allowed(model, sel) -> bool:
	"""Mirror of the option rules: only valid configurations become variants."""
	if model["code"] == "H1" and sel["Trim"] == "Standard" and sel["Drivetrain"] == "AWD":
		return False
	if model["code"] == "H2" and sel["Drivetrain"] == "AWD" and sel["Trim"] == "Standard":
		return False
	if model["code"] == "H3" and sel["Body Color"] not in {c for c, _ in COLORS[:6]}:
		return False
	if sel.get("Powertrain") == "Petrol" and sel["Market"] == "US":
		return False
	if sel.get("Package") == "Tech" and sel.get("Trim") == "Standard":
		return False
	return True


def _option_rules(model, colors) -> None:
	m = model["code"]
	rules = []
	if m in ("H1", "H2"):
		rules.append(
			{
				"rule_code": f"{m}-AWD-TRIM",
				"rule_type": "Excludes",
				"if_attribute": "Drivetrain",
				"if_values": "AWD",
				"then_attribute": "Trim",
				"then_values": "Standard",
				"message": "AWD is offered on Premium and Luxury trims",
			}
		)
	if "Powertrain" in model["attrs"]:
		rules.append(
			{
				"rule_code": f"{m}-US-HYBRID",
				"rule_type": "Excludes",
				"if_attribute": "Market",
				"if_values": "US",
				"then_attribute": "Powertrain",
				"then_values": "Petrol",
				"message": "North American models are offered as hybrids",
			}
		)
	if "Package" in model["attrs"]:
		rules.append(
			{
				"rule_code": f"{m}-TECH-TRIM",
				"rule_type": "Excludes",
				"if_attribute": "Package",
				"if_values": "Tech",
				"then_attribute": "Trim",
				"then_values": "Standard",
				"message": "The Tech package comes with the Premium and Luxury trims",
			}
		)
	if m == "H3":
		rules.append(
			{
				"rule_code": "H3-FLEET-COLORS",
				"rule_type": "Allowed Values",
				"then_attribute": "Body Color",
				"then_values": "\n".join(c for c, _ in colors[:6]),
				"message": "Commercial vans come in fleet colours only",
			}
		)
	for rule in rules:
		if not frappe.db.exists("Vehicle Option Rule", rule["rule_code"]):
			frappe.get_doc(
				{"doctype": "Vehicle Option Rule", "vehicle_model": m, "is_active": 1, **rule}
			).insert(ignore_permissions=True)


def price_lists(ctx, structure) -> None:
	"""Dealer list prices (JPY/USD) and the cost-plus transfer price for exported vehicles."""
	usd_rate = 1 / 150.0
	for code, info in structure["variants"].items():
		model = next(m for m in MODELS if m["code"] == info["model"])
		sel = info["selections"]
		price = model["price"]
		price += {"Premium": 280_000, "Luxury": 560_000, "Crew": 190_000}.get(
			sel.get("Trim") or sel.get("Body Style"), 0
		)
		price += 220_000 if sel["Drivetrain"] == "AWD" else 0
		price -= 260_000 if sel.get("Powertrain") == "Petrol" else 0
		price += 180_000 if sel.get("Package") == "Tech" else 0
		price += 55_000 if sel["Body Color"].startswith(("R53", "B64", "E86")) else 0
		for price_list, rate in (
			("Dealer Price JP", price),
			("Dealer Price US", round(price * usd_rate * 1.12, -1)),
		):
			if not frappe.db.exists("Item Price", {"item_code": code, "price_list": price_list}):
				frappe.get_doc(
					{
						"doctype": "Item Price",
						"item_code": code,
						"price_list": price_list,
						"selling": 1,
						"price_list_rate": rate,
					}
				).insert(ignore_permissions=True)
				ctx.count("Item Price")
	frappe.db.commit()
