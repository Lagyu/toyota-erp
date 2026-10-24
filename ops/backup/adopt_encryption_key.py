#!/usr/bin/env python3
"""Give a restored site the encryption key of the site its backup came from.

Password fields, API secrets and two-factor secrets are encrypted with the site's
``encryption_key``; a site restored on another host (or into a scratch site) has a key
of its own and could not decrypt them. ``bench backup`` saves the site configuration
next to the database dump (``*-site_config_backup.json``); this copies its key into the
target site's ``site_config.json`` (file to file: the key never appears on a command
line). No key in the backup (nothing was ever encrypted): nothing to do.

	adopt_encryption_key.py BACKUP_SITE_CONFIG TARGET_SITE_CONFIG
"""

import json
import os
import sys


def main(source: str, target: str) -> int:
	with open(source) as handle:
		key = json.load(handle).get("encryption_key")
	if not key:
		print("no encryption key in the backup: nothing to adopt")
		return 0
	with open(target) as handle:
		config = json.load(handle)
	if config.get("encryption_key") == key:
		print("encryption key already in place")
		return 0
	config["encryption_key"] = key
	tmp = f"{target}.tmp"
	with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
		json.dump(config, handle, indent=1)
	os.replace(tmp, target)
	print("encryption key adopted from the backup")
	return 0


if __name__ == "__main__":
	sys.exit(main(*sys.argv[1:3]))
