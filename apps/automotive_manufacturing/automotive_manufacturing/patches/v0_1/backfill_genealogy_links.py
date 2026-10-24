"""Record genealogy links for manufacture entries whose output was numbered automatically
(sub-assembly batches, powertrain and engine serials) and were posted without links."""

from automotive_manufacturing.traceability.genealogy import backfill


def execute():
	backfill()
