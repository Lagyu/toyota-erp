"""Synthetic, fictional names for people and organisations (requirements identity
boundary: no real employees, suppliers or dealers). Names are combinations of common
given/family names and generic industrial words; any resemblance to a real entity is
coincidental and no real company names are used."""

from __future__ import annotations

import random

JP_FAMILY = ["Sato", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Ito", "Yamamoto", "Nakamura", "Kobayashi", "Kato",
	"Yoshida", "Yamada", "Sasaki", "Yamaguchi", "Matsumoto", "Inoue", "Kimura", "Hayashi", "Shimizu", "Yamazaki",
	"Mori", "Abe", "Ikeda", "Hashimoto", "Ishikawa", "Ogawa", "Okada", "Hasegawa", "Fujita", "Goto", "Kondo",
	"Murakami", "Endo", "Aoki", "Sakamoto", "Fujii", "Nishimura", "Fukuda", "Ota", "Miura"]
JP_GIVEN = ["Haruto", "Sota", "Yuto", "Riku", "Ren", "Hinata", "Minato", "Kaito", "Daiki", "Kenta", "Shota", "Takumi",
	"Yui", "Aoi", "Sakura", "Hina", "Mio", "Rin", "Yuna", "Mei", "Akari", "Miyu", "Nanami", "Haruka", "Emi", "Kaori",
	"Naoki", "Tomoko", "Hiroshi", "Keiko"]
US_FAMILY = ["Johnson", "Williams", "Brown", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson",
	"Thomas", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White", "Harris", "Clark", "Lewis", "Walker", "Hall",
	"Allen", "Young", "King", "Wright", "Scott", "Green", "Baker", "Adams"]
US_GIVEN = ["James", "Mary", "Robert", "Patricia", "Michael", "Jennifer", "David", "Linda", "William", "Elizabeth",
	"Daniel", "Susan", "Matthew", "Jessica", "Anthony", "Sarah", "Mark", "Karen", "Steven", "Emily", "Andrew", "Grace",
	"Kevin", "Olivia", "Brian", "Megan", "Jason", "Laura", "Ryan", "Amy"]

SUPPLIER_PREFIX = ["Akatsuki", "Asagiri", "Fuji-Kaze", "Hoshi", "Kagerou", "Kaminari", "Kasumi", "Kiri", "Kurenai",
	"Midori", "Minamo", "Murasaki", "Nagare", "Natsume", "Oboro", "Raiden", "Sakimori", "Sekiryu", "Shiranui", "Shizuku",
	"Sora", "Tsubasa", "Tsukimi", "Wakaba", "Yamabuki", "Yuzuki", "Hikari", "Kohaku", "Ruri", "Seiran", "Arashi",
	"Hayate", "Homura", "Isana", "Kazane", "Mizuho", "Sazanami", "Tokiwa", "Usagi", "Yukimi"]
US_PREFIX = ["Great Lakes", "Prairie", "Ridgeline", "Bluewater", "Summit", "Ironwood", "Redstone", "Clearwater",
	"Northfield", "Harbor Point", "Silver Creek", "Granite Bay"]
COMMODITY_WORDS = {
	"castings": "Castings", "forgings": "Forging", "stamping": "Stamping", "electronics": "Electronics",
	"harness": "Wiring Systems", "glass": "Glass", "tires": "Tire & Wheel", "seating": "Seating", "plastics": "Polymers",
	"fasteners": "Fastener Works", "paint": "Coatings", "battery": "Energy Systems", "lighting": "Lighting",
	"rubber": "Rubber Products", "steel": "Steel", "aluminium": "Aluminium", "chemicals": "Chemicals", "machining":
	"Precision", "motors": "Motor Works", "interior": "Interior Systems",
}
JP_CITIES = ["Nagoya", "Toyohashi", "Okazaki", "Kariya", "Anjo", "Fukuoka", "Kitakyushu", "Kumamoto", "Oita", "Hamamatsu",
	"Shizuoka", "Gifu", "Yokkaichi", "Tsu", "Kanazawa", "Toyama", "Niigata", "Sendai"]
US_CITIES = ["Columbus", "Indianapolis", "Louisville", "Nashville", "Lexington", "Dayton", "Fort Wayne", "Toledo",
	"Grand Rapids", "Knoxville", "Chattanooga", "Huntsville", "Memphis", "Little Rock", "Tulsa", "Wichita"]


def person(rng: random.Random, region: str) -> tuple[str, str]:
	if region == "US":
		return rng.choice(US_GIVEN), rng.choice(US_FAMILY)
	return rng.choice(JP_GIVEN), rng.choice(JP_FAMILY)


def supplier_name(index: int, commodity: str, region: str = "JP") -> str:
	if region == "US":
		return f"{US_PREFIX[index % len(US_PREFIX)]} {COMMODITY_WORDS[commodity]} Inc."
	prefix = SUPPLIER_PREFIX[index % len(SUPPLIER_PREFIX)]
	suffix = "" if index < len(SUPPLIER_PREFIX) else f" {index // len(SUPPLIER_PREFIX) + 1}"
	return f"{prefix}{suffix} {COMMODITY_WORDS[commodity]} K.K."


def dealer_name(index: int, region: str) -> str:
	if region == "US":
		city = US_CITIES[index % len(US_CITIES)]
		return f"{city} Hinode Motors LLC"
	city = JP_CITIES[index % len(JP_CITIES)]
	return f"Hinode Motors {city}"


def carrier_name(index: int) -> str:
	return ["Raijin Logistics K.K.", "Kaze Car Carriers K.K.", "Umi Ocean Lines K.K.", "Prairie Haulage Inc.",
		"Tsubame Express K.K.", "Bluewater Auto Transport Inc."][index % 6]
