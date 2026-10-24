"""Deterministic synthetic data generation for non-production sites (requirements §5).

    bench --site SITE execute automotive_seed.runner.run --kwargs "{'profile': 'small'}"

* Refuses to run unless the site sets ``automotive_allow_seed: 1`` and is not marked
  ``automotive_environment: production`` (disabled by default everywhere).
* Stages are recorded on the Seed Run; a rerun skips finished stages and resumes the
  history at the first day not yet committed (one transaction per simulated day), so an
  interrupted import costs at most the day in flight and produces the same result as an
  uninterrupted one.
* Everything goes through ERPNext / app documents and their validations; nothing is
  inserted into ledgers directly. Validation of stock and accounting invariants runs at
  the end and is stored on the Seed Run.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import time
import traceback
import warnings
from pathlib import Path

import frappe
from frappe.utils import add_days, getdate, now_datetime

from automotive_seed.generator.context import SeedContext, load_profile
from automotive_seed.generator.inline_jobs import inline_background_jobs

STAGES = ["tenant", "people", "catalog", "suppliers", "dealers", "bank_accounts", "sourcing", "kanban", "pricing",
	"edi_partners", "opening", "history", "validation"]


class SeedNotAllowed(frappe.PermissionError):
	pass


def guard() -> None:
	if frappe.conf.get("automotive_environment") == "production":
		raise SeedNotAllowed("Synthetic data is never generated on a production site.")
	if not frappe.conf.get("automotive_allow_seed"):
		raise SeedNotAllowed("Set automotive_allow_seed: 1 in site_config.json to generate reference data.")


def _state_path(run: str) -> Path:
	"""The generator's state (roster, catalogue structure, pending work) is kept with the
	site's private files, so backups — and datasets restored from them — carry it."""
	path = Path(frappe.get_site_path("private", "files", "seed"))
	path.mkdir(parents=True, exist_ok=True)
	target = path / f"{run}-state.json"
	legacy = Path(frappe.get_site_path("private", "seed", f"{run}-state.json"))
	if not target.exists() and legacy.exists():
		legacy.replace(target)
	return target


def load_state(run: str) -> dict:
	path = _state_path(run)
	return json.loads(path.read_text()) if path.exists() else {}


def save_state(run: str, state: dict) -> None:
	path = _state_path(run)
	tmp = path.with_suffix(".tmp")
	tmp.write_text(json.dumps(state, default=str, sort_keys=True))
	tmp.replace(path)


def _run_doc(profile: dict) -> str:
	name = f"SEED-{profile['profile']}-{profile['seed']}"
	if not frappe.db.exists("Seed Run", name):
		_state_path(name).unlink(missing_ok=True)  # a new run never inherits an old run's state
		doc = frappe.get_doc({"doctype": "Seed Run", "profile": profile["profile"], "seed": int(profile["seed"]),
			"status": "Queued", "stages": [{"stage": s, "stage_status": "Pending"} for s in STAGES]})
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
	return name


def _mark(run: str, stage: str, note: str = "", documents: int = 0) -> None:
	frappe.db.sql("""update `tabSeed Run Stage` set stage_status='Done', completed_at=%s, note=%s, documents=%s
		where parent=%s and stage=%s""", (now_datetime(), note[:500], documents, run, stage))
	frappe.db.commit()


def _done(run: str) -> set[str]:
	return set(frappe.get_all("Seed Run Stage", filters={"parent": run, "stage_status": "Done"}, pluck="stage"))


def run(profile: str = "small", seed: int | None = None, until: str | None = None) -> str:
	guard()
	frappe.set_user("Administrator")
	spec = load_profile(profile)
	if seed is not None:
		spec["seed"] = int(seed)
	name = _run_doc(spec)
	ctx = SeedContext(name, spec)
	state = load_state(name)
	frappe.flags.automotive_bulk = True
	frappe.flags.automotive_inline_integration = True
	frappe.flags.automotive_inline_planning = True
	frappe.flags.mute_emails = True
	# Users are created in bulk under a frozen clock; Frappe's per-hour signup throttle would
	# count all of them as "just now". Raised for this process only (site config untouched).
	frappe.local.conf["throttle_user_limit"] = 10**9
	frappe.db.set_value("Seed Run", name, {"status": "Running", "start_date": ctx.start, "end_date": ctx.end,
		"started_at": frappe.db.get_value("Seed Run", name, "started_at") or now_datetime(), "error": None})
	frappe.db.commit()
	try:
		with inline_background_jobs():
			_stages(ctx, name, state, until)
		# a run that reached validation ends with its outcome (Validated / Validation Failed);
		# one that stopped earlier (until date, stop request) is Paused and resumes on the next run
		outcome = frappe.db.get_value("Seed Run", name, "status")
		finished = "validation" in _done(name)
		status = outcome if finished and outcome in ("Validated", "Validation Failed") else "Paused"
		frappe.db.set_value("Seed Run", name, {"status": status, "finished_at": now_datetime() if finished else None})
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.db.set_value("Seed Run", name, {"status": "Failed", "error": traceback.format_exc()[-6000:]})
		frappe.db.commit()
		raise
	return name


def _stages(ctx: SeedContext, name: str, state: dict, until: str | None) -> None:
	from automotive_seed.generator import catalog, parties, supply

	done = _done(name)

	def stage(key, fn):
		if key in done:
			return
		frappe.db.set_value("Seed Run", name, "stage", key)
		frappe.db.commit()
		started = time.monotonic()
		if key in ("history", "validation"):
			note = fn() or ""
		else:
			# master data is set up the day before the first simulated business day, so its dates
			# (supplier approvals, partner access, agreements) precede the history that uses them
			with ctx.frozen(dt.datetime.combine(ctx.start - dt.timedelta(days=1), dt.time(9, 0))):
				note = fn() or ""
		_mark(name, key, f"{note} ({time.monotonic() - started:.0f}s)", sum(ctx.counts.values()))
		save_state(name, state)

	def tenant():
		from automotive_core.setup.tenant import apply

		apply("hinode")

	stage("tenant", tenant)

	def people():
		state["roster"] = parties.people(ctx)
		state["plant_of"] = ctx.cache.get("plant_of", {})
		return f"{sum(len(v) for v in state['roster'].values())} internal users"

	stage("people", people)
	ctx.cache["roster"] = state.get("roster") or {}
	ctx.cache["plant_of"] = state.get("plant_of") or {}

	def build_catalog():
		structure = catalog.build(ctx)
		state["structure"] = structure
		return f"{len(structure['variants'])} variants, {len(structure['purchased'])} purchased items"

	stage("catalog", build_catalog)

	def suppliers():
		state["suppliers"] = parties.suppliers(ctx, state["roster"])
		state["carriers"] = parties.carriers(ctx)
		return f"{sum(len(v) for v in state['suppliers'].values())} suppliers"

	stage("suppliers", suppliers)

	def dealers():
		state["dealers"] = parties.dealers(ctx, state["roster"])
		return f"{len(state['dealers']['JP'])} JP / {len(state['dealers']['US'])} US dealers"

	stage("dealers", dealers)
	stage("bank_accounts", lambda: parties.bank_accounts(ctx, state["roster"],
		sorted({s for g in state["suppliers"].values() for s in g})))
	stage("sourcing", lambda: supply.sourcing(ctx, state["structure"], state["roster"], state["suppliers"]))

	def kanban():
		state["loops"] = supply.blanket_orders_and_kanban(ctx, state["structure"], state["roster"])
		return f"{len(state['loops'])} kanban items"

	stage("kanban", kanban)

	def pricing():
		catalog.price_lists(ctx, state["structure"])
		supply.transfer_prices(ctx, state["roster"])

	stage("pricing", pricing)
	stage("edi_partners", lambda: parties.integration_partners(ctx, state["suppliers"], state["dealers"]))
	stage("opening", lambda: supply.opening(ctx, state["structure"], state["roster"]))

	def history():
		from automotive_seed.generator import simulation

		stop_at = getdate(until) if until else ctx.end
		last = frappe.db.get_value("Seed Run", name, "simulated_date")
		day = add_days(getdate(last), 1) if last else ctx.start
		sim = simulation.Simulation(ctx, state)
		with ctx.auto_tick():
			_simulate(ctx, sim, name, state, day, stop_at)
		return f"history to {min(stop_at, ctx.end)}"

	def _simulate(ctx, sim, name, state, day, stop_at):
		while day <= min(stop_at, ctx.end):
			with _atomic_day():
				sim.run_day(day)
			frappe.db.commit()
			with ctx.step(23, 30):
				_repost_pending()
			ctx.save_progress(simulated_date=day, stage=f"history {day}")
			frappe.db.commit()
			save_state(name, state)
			if frappe.db.get_value("Seed Run", name, "stop_requested"):
				frappe.db.set_value("Seed Run", name, {"stop_requested": 0})
				frappe.db.commit()
				raise _Paused
			frappe.local.document_cache = {}
			day = add_days(day, 1)
		if day <= ctx.end:
			raise _Paused

	try:
		stage("history", history)
	except _Paused:
		return

	def validation():
		from automotive_seed.generator import validate

		result = validate.run(ctx, state)
		frappe.db.set_value("Seed Run", name, {"validation": json.dumps(result, indent=1, default=str),
			"status": "Validated" if result["ok"] else "Validation Failed"})
		frappe.db.commit()
		return "ok" if result["ok"] else "FAILED"

	stage("validation", validation)


class _Paused(Exception):
	pass


@contextlib.contextmanager
def _atomic_day():
	"""A simulated day is one transaction: code that commits on its own (background-job
	style work run inline) is suppressed until the day ends, and a full mid-day rollback
	is a no-op rather than silently discarding the morning (Frappe's transaction control)."""
	frappe.db._disable_transaction_control += 1
	try:
		with warnings.catch_warnings():
			warnings.filterwarnings("ignore", message="Commit/rollback are disabled")
			yield
	finally:
		frappe.db._disable_transaction_control -= 1


def _repost_pending() -> None:
	"""Process ERPNext's queued stock/GL reposts (normally an hourly scheduler job) so the
	ledgers are final before the next simulated day starts."""
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
		execute_reposting_entry,
		get_repost_item_valuation_entries,
	)

	for row in get_repost_item_valuation_entries():
		execute_reposting_entry(row.name)
	frappe.db.commit()


@frappe.whitelist(methods=["POST"])
def request_stop(run: str) -> None:
	frappe.only_for("System Manager")
	frappe.db.set_value("Seed Run", run, "stop_requested", 1)


def revalidate(profile: str = "small", seed: int | None = None) -> dict:
	"""Re-run the invariant checks of a finished run and store the result on its Seed Run."""
	guard()
	frappe.set_user("Administrator")
	from automotive_seed.generator import validate

	spec = load_profile(profile)
	if seed is not None:
		spec["seed"] = int(seed)
	name = f"SEED-{spec['profile']}-{spec['seed']}"
	ctx = SeedContext(name, spec)
	result = validate.run(ctx, load_state(name))
	frappe.db.set_value("Seed Run", name, {"validation": json.dumps(result, indent=1, default=str),
		"status": "Validated" if result["ok"] else "Validation Failed"})
	frappe.db.commit()
	return {"ok": result["ok"], "failed": [c["check"] for c in result["checks"] if not c["ok"]]}
