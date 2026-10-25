"""The generator's preconditions for a reproducible run (DV-1). Not test_runner.py: Frappe's test
discovery skips files of that name."""

from __future__ import annotations

import sys

from frappe.tests import IntegrationTestCase

from automotive_seed.runner import SeedNotAllowed, reproducible


class TestReproducible(IntegrationTestCase):
	def test_a_process_with_randomised_string_hashes_cannot_generate(self):
		if sys.flags.hash_randomization:
			with self.assertRaises(SeedNotAllowed):
				reproducible()
		else:
			reproducible()  # PYTHONHASHSEED is set: allowed
