"""ops/backup/binlog.py without a database server: the position a dump records, the logs a
replay takes, and those the mirror keeps (run by scripts/ci/unit.sh)."""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITE = "erp.test"
# the start of a dump made through bin/mariadb-dump (Frappe's header, then mariadb-dump's)
DUMP = """-- begin frappe metadata
-- [frappe]
-- version = 16.53.0
-- branch = N/A
-- end frappe metadata
-- MariaDB dump 10.19-11.8.6-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: db    Database: _0123456789abcdef
-- ------------------------------------------------------
-- Server version	11.8.6-MariaDB-log

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET NAMES utf8mb4 */;

--
-- Position to start replication or point-in-time recovery from
--

-- CHANGE MASTER TO MASTER_LOG_FILE='mariadb-bin.000042', MASTER_LOG_POS=73517;

--
-- Table structure for table `tabNote`
--
"""


# mariadb-binlog's text for a format description and three transactions: rows of an InnoDB
# table, rows of the MyISAM search index and error log, and one statement that maps both kinds
BINLOG_TEXT = """DELIMITER /*!*/;
# at 4
#251025  0:00:00 server id 1  end_log_pos 256 CRC32 0x5a0b1c2d \tStart: binlog v 4, server v 11.8.6-MariaDB-log
BINLOG '
FFFF-format-description
'/*!*/;
# at 299
#251025  0:00:10 server id 1  end_log_pos 341 CRC32 0x1a2b3c4d \tGTID 0-1-101 trans
START TRANSACTION
/*!*/;
# at 341
# at 420
#251025  0:00:10 server id 1  end_log_pos 420 CRC32 0x1a2b3c4d \tAnnotate_rows:
#Q> INSERT INTO `tabNote` (`name`) VALUES ('n2')
#251025  0:00:10 server id 1  end_log_pos 480 CRC32 0x1a2b3c4d \tTable_map: `_0123456789abcdef`.`tabNote` mapped to number 30
# at 480
#251025  0:00:10 server id 1  end_log_pos 560 CRC32 0x1a2b3c4d \tWrite_rows: table id 30 flags: STMT_END_F

BINLOG '
AAAA-note-rows
AAAA-note-rows-2
'/*!*/;
# at 560
#251025  0:00:10 server id 1  end_log_pos 591 CRC32 0x1a2b3c4d \tXid = 77
COMMIT/*!*/;
# at 591
#251025  0:00:11 server id 1  end_log_pos 633 CRC32 0x1a2b3c4d \tGTID 0-1-102
START TRANSACTION
/*!*/;
# at 633
#251025  0:00:11 server id 1  end_log_pos 700 CRC32 0x1a2b3c4d \tAnnotate_rows:
#Q> insert into __global_search (doctype, name) values ('UOM', 'Dekagram/Litre')
#251025  0:00:11 server id 1  end_log_pos 760 CRC32 0x1a2b3c4d \tTable_map: `_0123456789abcdef`.`__global_search` mapped to number 31
# at 760
#251025  0:00:11 server id 1  end_log_pos 900 CRC32 0x1a2b3c4d \tWrite_rows: table id 31 flags: STMT_END_F
BINLOG 'BBBB-search-rows'/*!*/;
#251025  0:00:11 server id 1  end_log_pos 960 CRC32 0x1a2b3c4d \tTable_map: `_0123456789abcdef`.`tabError Log` mapped to number 32
# at 960
#251025  0:00:11 server id 1  end_log_pos 1100 CRC32 0x1a2b3c4d \tWrite_rows: table id 32 flags: STMT_END_F

BINLOG '
CCCC-error-log-rows
'/*!*/;
# at 1100
#251025  0:00:11 server id 1  end_log_pos 1140 CRC32 0x1a2b3c4d \tQuery\tthread_id=5\texec_time=0\terror_code=0
COMMIT
/*!*/;
# at 1140
#251025  0:00:12 server id 1  end_log_pos 1180 CRC32 0x1a2b3c4d \tGTID 0-1-103 trans
START TRANSACTION
/*!*/;
#251025  0:00:12 server id 1  end_log_pos 1240 CRC32 0x1a2b3c4d \tTable_map: `_0123456789abcdef`.`tabNote` mapped to number 30
#251025  0:00:12 server id 1  end_log_pos 1300 CRC32 0x1a2b3c4d \tTable_map: `_0123456789abcdef`.`__global_search` mapped to number 31
# at 1300
#251025  0:00:12 server id 1  end_log_pos 1400 CRC32 0x1a2b3c4d \tUpdate_rows: table id 30 flags: STMT_END_F

BINLOG '
DDDD-mixed-rows
'/*!*/;
COMMIT/*!*/;
DELIMITER ;
"""


def load_binlog(bench: Path):
	os.environ["BENCH_DIR"] = str(bench)
	spec = importlib.util.spec_from_file_location("binlog_under_test", HERE / "binlog.py")
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


class TestBinlog(unittest.TestCase):
	def setUp(self):
		self._tmp = tempfile.TemporaryDirectory()
		self.bench = Path(self._tmp.name)
		(self.bench / "sites" / SITE).mkdir(parents=True)
		os.environ["SITE_NAME"] = SITE
		self.binlog = load_binlog(self.bench)

	def tearDown(self):
		self._tmp.cleanup()

	def dump(self, text: str) -> Path:
		path = self.bench / "20261024_173000-erp_test-database.sql.gz"
		with gzip.open(path, "wt") as handle:
			handle.write(text + "INSERT INTO `tabNote` VALUES ('n1');\n" * 50)
		return path

	def test_a_replay_leaves_out_the_rows_of_tables_without_transactions(self):
		"""The dump reads MyISAM tables after its snapshot: their rows are left as it has them."""
		skipped: list[str] = []
		kept = b"".join(
			self.binlog.without_rows_of(
				BINLOG_TEXT.encode().splitlines(keepends=True), {"__global_search", "tabError Log"}, skipped
			)
		)
		for rows in (b"FFFF-format-description", b"AAAA-note-rows", b"AAAA-note-rows-2", b"DDDD-mixed-rows"):
			self.assertIn(rows, kept)
		for rows in (b"BBBB-search-rows", b"CCCC-error-log-rows"):
			self.assertNotIn(rows, kept)
		self.assertEqual(skipped, ["__global_search", "tabError Log"])
		# the transactions' frames stay whole
		self.assertEqual(kept.count(b"START TRANSACTION"), 3)
		self.assertEqual(kept.count(b"COMMIT"), 3)
		self.assertEqual(kept.count(b"BINLOG '"), 3)
		self.assertTrue(kept.rstrip().endswith(b"DELIMITER ;"))

	def test_position_is_read_from_the_dump(self):
		out = self.bench / "set-binlog.json"
		self.assertEqual(self.binlog.position(str(self.dump(DUMP)), str(out)), 0)
		record = json.loads(out.read_text())
		self.assertEqual((record["file"], record["position"]), ("mariadb-bin.000042", 73517))
		kept = self.binlog.state_dir(SITE) / "positions" / "20261024_173000-erp_test.json"
		self.assertEqual(json.loads(kept.read_text())["position"], 73517)

	def test_a_dump_without_a_position_gives_none(self):
		out = self.bench / "set-binlog.json"
		head = "\n".join(line for line in DUMP.splitlines() if "CHANGE MASTER" not in line)
		self.assertEqual(self.binlog.position(str(self.dump(head)), str(out)), 1)
		self.assertFalse(out.exists())

	def logs(self, numbers: list[int]) -> Path:
		root = self.bench / "restored"
		where = (
			root / "home" / "frappe" / "frappe-bench" / "sites" / SITE / "private" / "ops" / "binlog" / "logs"
		)
		where.mkdir(parents=True)
		for n in numbers:
			(where / f"mariadb-bin.{n:06d}").write_bytes(b"\xfebin")
		(where / "mariadb-bin.index").write_text("")
		return root

	def test_replay_takes_the_logs_from_the_position_in_order(self):
		root = self.logs([40, 41, 42, 43, 44])
		self.assertEqual(
			[f.name for f in self.binlog.logs_from("mariadb-bin.000042", root)],
			["mariadb-bin.000042", "mariadb-bin.000043", "mariadb-bin.000044"],
		)

	def test_numbering_past_six_digits_stays_in_order(self):
		root = self.logs([999998, 999999, 1000000])
		self.assertEqual(
			[f.name for f in self.binlog.logs_from("mariadb-bin.999998", root)],
			["mariadb-bin.999998", "mariadb-bin.999999", "mariadb-bin.1000000"],
		)

	def test_a_missing_start_or_a_gap_is_refused(self):
		root = self.logs([43, 44, 46])
		with self.assertRaises(self.binlog.LogsMissing):
			self.binlog.logs_from("mariadb-bin.000042", root)
		with self.assertRaisesRegex(self.binlog.LogsMissing, "000044..mariadb-bin.000046"):
			self.binlog.logs_from("mariadb-bin.000043", root)

	def test_the_mirror_keeps_the_logs_of_the_last_backups(self):
		state = self.binlog.state_dir(SITE)
		self.assertEqual(self.binlog._keep_from(state, ["mariadb-bin.000050", "mariadb-bin.000051"]), 51)
		(state / "positions").mkdir(parents=True)
		for stamp, number in (("20261020", 10), ("20261021", 20), ("20261022", 30), ("20261023", 40)):
			(state / "positions" / f"{stamp}_173000-erp_test.json").write_text(
				json.dumps({"file": f"mariadb-bin.{number:06d}", "position": 4})
			)
		# the oldest of the last three backups' positions
		self.assertEqual(self.binlog._keep_from(state, ["mariadb-bin.000050"]), 20)


if __name__ == "__main__":
	unittest.main()
