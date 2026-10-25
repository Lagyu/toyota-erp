#!/usr/bin/env bash
# End-to-end suite (Playwright, pinned image) against a reference site with the synthetic
# dataset. Two modes:
#   e2e.sh --url http://small.localhost:18000 --site small.localhost [-- playwright args]
#       a running site (the development bench)
#   e2e.sh IMAGE:TAG [--dataset DIR] [--scheduler] [--keep] [-- playwright args]
#       a reference stack from a reference image (CI): new site, the small dataset (restored
#       from .local/reference/ when the generator is unchanged, else generated once and
#       cached; migrations bring a cached dataset forward), scheduler off so background
#       jobs cannot race the journeys, then the API contract tests and the suite; torn down
#       unless --keep. --dataset restores a dataset.yml export (database.sql.gz, public.tgz,
#       private.tgz, encryption_key) instead, with the database sized for the standard profile;
#       --scheduler leaves the scheduler on, so background jobs run beside the journeys as on
#       a deployed site (journeys.yml)
# Persona passwords (automotive_seed.e2e.prepare_users) and journey preconditions
# (automotive_seed.e2e.prepare_journeys) are generated for the run and kept in
# .local/e2e/{credentials,fixtures}.json (0600); reports in .local/e2e/.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=e2e
load_env_file "$REPO_ROOT/infra/ci-tools.env"
url="" site="" image="" keep=0 dataset="" scheduler=0 pw_args=()
while (($#)); do
	case $1 in
		--url) url=$2; shift 2 ;;
		--site) site=$2; shift 2 ;;
		--keep) keep=1; shift ;;
		--dataset) dataset=$(cd "$2" && pwd); shift 2 ;;
		--scheduler) scheduler=1; shift ;;
		--) shift; pw_args=("$@"); break ;;
		-*) die "unknown argument $1 (usage: e2e.sh IMAGE:TAG | --url URL --site SITE [-- playwright args])" ;;
		*) image=$1; shift ;;
	esac
done
out=$REPO_ROOT/.local/e2e
mkdir -p "$out"
umask 077

if [[ -n $image ]]; then
	stamp=$(date -u +%Y%m%dT%H%M%SZ)
	work=$REPO_ROOT/.local/e2e-stack/$stamp
	secrets=$work/secrets
	mkdir -p "$secrets"
	for s in db_root_password admin_password metrics_token restic_password; do random_secret >"$secrets/$s"; done
	secret_for_frappe "$secrets/db_root_password" "$secrets/restic_password"
	site=e2e.localhost
	url=http://e2e.localhost:18091
	env_file=$work/e2e.env
	cat >"$env_file" <<EOF
COMPOSE_PROJECT_NAME=hinode-e2e
ENVIRONMENT=reference
SITE_NAME=$site
SITES_RULE=Host(\`$site\`)
TLS_MODE=none
HTTP_PUBLISH_PORT=127.0.0.1:18091
CUSTOM_IMAGE=${image%:*}
CUSTOM_TAG=${image##*:}
PULL_POLICY=never
CLAMAV=0
MONITORING=0
DB_PASSWORD_SECRETS_FILE=$secrets/db_root_password
ADMIN_PASSWORD_FILE=$secrets/admin_password
METRICS_TOKEN_FILE=$secrets/metrics_token
RESTIC_PASSWORD_FILE=$secrets/restic_password
RESTIC_REPOSITORY=/home/frappe/frappe-bench/sites/.restic-e2e
BACKUP_SCHEDULE=off
RESTORE_DRILL_SCHEDULE=off
BINLOG_SCHEDULE=off
DB_BINLOG_RETENTION_SECONDS=3600
GUNICORN_WORKERS=4
BACKEND_MEM_LIMIT=2g
BACKEND_CPUS=2
WORKER_MEM_LIMIT=2g
WORKER_CPUS=2
DB_BUFFER_POOL=$([[ -n $dataset ]] && echo 2G || echo 768M)
DB_MEM_LIMIT=$([[ -n $dataset ]] && echo 4g || echo 2g)
DB_CPUS=2
EOF
	compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
	teardown() {
		"${compose[@]}" logs --no-color >"$work/compose.log" 2>&1 || true
		if ((keep)); then
			log "stack kept: ${compose[*]} (secrets in ${secrets#"$REPO_ROOT"/})"
			return
		fi
		"${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
		rm -rf "$secrets" "$env_file"
	}
	trap teardown EXIT
	bench() { "${compose[@]}" exec -T backend bench --site "$site" "$@"; }
	backups=/home/frappe/frappe-bench/sites/$site/private/backups

	log "starting the reference stack ($image)"
	"${compose[@]}" up -d --quiet-pull 2>&1 | tail -3
	for _ in $(seq 1 60); do
		"${compose[@]}" exec -T db healthcheck.sh --connect >/dev/null 2>&1 && break
		sleep 5
	done
	"$REPO_ROOT/scripts/deploy/new-site.sh" "$env_file" 2>&1 | tail -3

	# the small dataset: keyed by the generator's inputs and the tenant definitions (companies,
	# taxes, policies), regenerated only when they change (the end-to-end fixtures in e2e.py do
	# not shape the dataset; other application changes reach it through bench migrate)
	seed=apps/automotive_seed/automotive_seed
	key=$(cd "$REPO_ROOT" && git ls-files -s "$seed/generator" "$seed/profiles" "$seed/reference_data" \
		"$seed/runner.py" apps/automotive_core/automotive_core/setup/tenants | sha256sum | cut -c1-12)
	cache=$REPO_ROOT/.local/reference/small-$key
	restore_dataset() { # DIR: an export (database.sql.gz public.tgz private.tgz encryption_key)
		local f
		# streamed through exec: docker cp cannot write into the container's tmpfs /tmp, and its
		# files would be root-owned while the bench runs as frappe
		for f in database.sql.gz public.tgz private.tgz; do
			"${compose[@]}" exec -T backend bash -c "cat >/tmp/$f" <"$1/$f"
		done
		"${compose[@]}" exec -T -e DB_ROOT="$(<"$secrets/db_root_password")" backend bash -c \
			"bench --site '$site' restore /tmp/database.sql.gz --with-public-files /tmp/public.tgz \
			--with-private-files /tmp/private.tgz --db-root-password \"\$DB_ROOT\" --force | tail -3 \
			&& rm -f /tmp/database.sql.gz /tmp/public.tgz /tmp/private.tgz"
		# the dataset's encrypted fields stay readable with its own key. Frappe creates one only
		# when a field is first encrypted: a dataset without one has an empty line, and setting
		# an empty key would break every later encryption
		if [[ -n $(tr -d '[:space:]' <"$1/encryption_key" 2>/dev/null) ]]; then
			"${compose[@]}" exec -T backend python3 -c "$SET_SITE_CONFIG_PY" "$site" encryption_key \
				<"$1/encryption_key"
		fi
		bench migrate >/dev/null
	}
	# paused in the site config, which a restore keeps: the dataset's own scheduler is on, and a
	# tick between the restore and disable-scheduler below would queue every overdue job
	bench scheduler pause >/dev/null
	if [[ -n $dataset ]]; then
		log "restoring the dataset of $dataset ($(python3 -c 'import json, sys
s = json.load(open(sys.argv[1]))
print(s.get("run"), s.get("status"), s.get("start_date"), "..", s.get("simulated_date"))' "$dataset/status.json" \
			2>/dev/null || echo "no status.json"))"
		restore_dataset "$dataset"
	elif [[ -f $cache/database.sql.gz ]]; then
		log "restoring the small dataset ($key)"
		restore_dataset "$cache"
	else
		log "generating the small dataset (cached afterwards as $key)"
		if ! bench execute automotive_seed.runner.run --kwargs "{'profile': 'small'}" | tail -1; then
			# bench execute reports a failing call as a NameError: the run stored the real one
			bench execute automotive_seed.runner.status --kwargs "{'profile': 'small'}" | grep '^{' | tail -1 \
				| python3 -c 'import json,sys; s=json.load(sys.stdin); print(s["status"], s.get("stage")); print(s.get("error") or "")'
			die "the small dataset could not be generated"
		fi
		# run() also returns when the dataset's invariants fail: it records that on the Seed Run
		bench execute automotive_seed.runner.status --kwargs "{'profile': 'small'}" | grep '^{' | tail -1 \
			| python3 -c 'import json, sys
s = json.load(sys.stdin)
if s["status"] != "Validated":
	failed = [c for c in (s.get("validation") or {}).get("checks", []) if not c.get("ok")]
	print(s["status"], s.get("stage"), (s.get("error") or "")[:2000], json.dumps(failed, default=str)[:4000])
	sys.exit(1)' || die "the small dataset did not pass its validation"
		bench backup --with-files --compress >/dev/null
		rm -rf "$cache.tmp"
		mkdir -p "$cache.tmp"
		newest() { "${compose[@]}" exec -T backend bash -c "ls -t $backups/*$1 | head -1"; }
		# trigger DEFINERs name this stack's database user: strip them so any site loads the dump
		"${compose[@]}" exec -T backend bash -c "zcat '$(newest -database.sql.gz)' \
			| sed -E 's#/\\*!50017 DEFINER=\`[^\`]*\`@\`[^\`]*\`\\*/ ?##g' | gzip -c" >"$cache.tmp/database.sql.gz"
		# --compress archives the files as .tgz
		"${compose[@]}" cp "backend:$(newest -private-files.tgz)" "$cache.tmp/private.tgz" >/dev/null
		public=$("${compose[@]}" exec -T backend bash -c "ls -t $backups/*-files.tgz | grep -v private-files | head -1")
		"${compose[@]}" cp "backend:$public" "$cache.tmp/public.tgz" >/dev/null
		"${compose[@]}" exec -T backend bash -c "python3 -c 'import json; \
			print(json.load(open(\"sites/$site/site_config.json\")).get(\"encryption_key\", \"\"))'" >"$cache.tmp/encryption_key"
		rm -rf "$cache"
		mv "$cache.tmp" "$cache"
	fi
	if ((scheduler)); then
		# a deployed site's scheduler: the restored dataset's jobs come due at once, then on time
		bench scheduler resume >/dev/null
		bench enable-scheduler >/dev/null
		log "scheduler on: $(bench scheduler status | tail -1)"
	else
		bench disable-scheduler >/dev/null
	fi
	"${compose[@]}" restart backend frontend >/dev/null
	for _ in $(seq 1 30); do
		curl -fsS -o /dev/null -H "Host: $site" http://127.0.0.1:18091/api/method/automotive_core.ops.health.live \
			2>/dev/null && break
		sleep 5
	done
	prepare() { # function, file
		bench execute "automotive_seed.e2e.$1" --kwargs "{'output': '/tmp/$2.json'}" | tail -1
		"${compose[@]}" exec -T backend bash -c "cat /tmp/$2.json && rm -f /tmp/$2.json" >"$out/$2.json"
	}
else
	[[ -n $url && -n $site ]] || die "usage: e2e.sh IMAGE:TAG | --url URL --site SITE [-- playwright args]"
	prepare() { # function, file
		"$REPO_ROOT/scripts/dev/compose.sh" exec -T bench bash -lc \
			"cd ~/dev/frappe-bench && bench --site '$site' execute automotive_seed.e2e.$1 \
			--kwargs \"{'output': '/workspace/.local/e2e/$2.json'}\"" | tail -1
	}
fi

# personas (one-run passwords) and the journeys' preconditions (staged material, partner
# signing secret): both files are 0600 and mounted read-only into the Playwright container
prepare prepare_users credentials
prepare prepare_journeys fixtures
if [[ -n $image ]]; then
	# a restored dataset keeps the Administrator password of the stack that generated it:
	# report whether this stack's secret is accepted, then set it
	login=$("${compose[@]}" exec -T -e PW="$(<"$secrets/admin_password")" frontend sh -c \
		"curl -s -o /tmp/login.out -w '%{http_code}' -H 'Host: $site' -H 'Content-Type: application/json' \
		-d \"{\\\"usr\\\": \\\"Administrator\\\", \\\"pwd\\\": \\\"\$PW\\\"}\" http://127.0.0.1:8080/api/method/login; \
		echo ' '; head -c 200 /tmp/login.out; rm -f /tmp/login.out")
	log "administrator login with this stack's secret before setting it: $login"
	"${compose[@]}" exec -T backend env/bin/python -c "$SET_ADMIN_PASSWORD_PY" "$site" <"$secrets/admin_password"
	# the API against its OpenAPI document (integration_tests/), as the stack's administrator
	INTEGRATION_CREDENTIALS=$out/credentials.json INTEGRATION_FIXTURES=$out/fixtures.json \
		"$REPO_ROOT/scripts/ci/contract.sh" --url "$url" --site "$site" --password-file "$secrets/admin_password" \
		|| contract=$?
fi
# the browser suite runs even when the API tests failed: one run reports both
log "running Playwright against $url ($site)"
playwright=0
docker_cmd run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e CI="${CI:-}" \
	-e E2E_BASE_URL="$url" -e E2E_CREDENTIALS=/creds/credentials.json -e E2E_FIXTURES=/creds/fixtures.json \
	-e E2E_REPORT_DIR=/report \
	-v "$REPO_ROOT/e2e:/e2e" -v "$out:/creds:ro" -v "$out:/report" -w /e2e "$PLAYWRIGHT_IMAGE" \
	bash -lc 'test -d node_modules/@axe-core/playwright || npm ci --no-audit --no-fund >/dev/null; npx playwright test "$@"' \
	e2e "${pw_args[@]}" || playwright=$?
if [[ -n $image ]]; then
	# every state-changing method against its documented request schema, with placeholders that
	# name nothing: after the browser suite, since a call may leave a record behind
	"$REPO_ROOT/scripts/ci/contract.sh" --url "$url" --site "$site" --password-file "$secrets/admin_password" \
		test_api_requests || requests=$?
fi
if ((${contract:-0} || playwright || ${requests:-0})); then
	die "API contract and concurrency tests: exit ${contract:-0}; Playwright: exit $playwright; state-changing requests: exit ${requests:-0}"
fi
