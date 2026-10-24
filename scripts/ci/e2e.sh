#!/usr/bin/env bash
# End-to-end suite (Playwright, pinned image) against a reference site with the synthetic
# dataset. Two modes:
#   e2e.sh --url http://small.localhost:18000 --site small.localhost [-- playwright args]
#       a running site (the development bench)
#   e2e.sh IMAGE:TAG [--keep] [-- playwright args]
#       a reference stack from a reference image (CI): new site, the small dataset (restored
#       from .local/reference/ when the generator is unchanged, else generated once and
#       cached; migrations bring a cached dataset forward), scheduler off so background
#       jobs cannot race the journeys, then the suite; torn down unless --keep
# Persona passwords (automotive_seed.e2e.prepare_users) and journey preconditions
# (automotive_seed.e2e.prepare_journeys) are generated for the run and kept in
# .local/e2e/{credentials,fixtures}.json (0600); reports in .local/e2e/.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=e2e
load_env_file "$REPO_ROOT/infra/ci-tools.env"
url="" site="" image="" keep=0 pw_args=()
while (($#)); do
	case $1 in
		--url) url=$2; shift 2 ;;
		--site) site=$2; shift 2 ;;
		--keep) keep=1; shift ;;
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
GUNICORN_WORKERS=4
BACKEND_MEM_LIMIT=2g
BACKEND_CPUS=2
WORKER_MEM_LIMIT=2g
WORKER_CPUS=2
DB_BUFFER_POOL=768M
DB_MEM_LIMIT=2g
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
	if [[ -f $cache/database.sql.gz ]]; then
		log "restoring the small dataset ($key)"
		# streamed through exec: docker cp cannot write into the container's tmpfs /tmp, and its
		# files would be root-owned while the bench runs as frappe
		for f in database.sql.gz public.tgz private.tgz; do
			"${compose[@]}" exec -T backend bash -c "cat >/tmp/$f" <"$cache/$f"
		done
		"${compose[@]}" exec -T -e DB_ROOT="$(<"$secrets/db_root_password")" backend bash -c \
			"bench --site '$site' restore /tmp/database.sql.gz --with-public-files /tmp/public.tgz \
			--with-private-files /tmp/private.tgz --db-root-password \"\$DB_ROOT\" --force | tail -3 \
			&& rm -f /tmp/database.sql.gz /tmp/public.tgz /tmp/private.tgz"
		# the dataset's encrypted fields stay readable with its own key (Frappe creates one only
		# when a field is first encrypted)
		if [[ -s $cache/encryption_key ]]; then
			"${compose[@]}" exec -T backend python3 -c "$SET_SITE_CONFIG_PY" "$site" encryption_key \
				<"$cache/encryption_key"
		fi
		bench migrate >/dev/null
	else
		log "generating the small dataset (cached afterwards as $key)"
		bench execute automotive_seed.runner.run --kwargs "{'profile': 'small'}" | tail -1
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
	"${compose[@]}" exec -T backend bench --site "$site" disable-scheduler >/dev/null
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
log "running Playwright against $url ($site)"
docker_cmd run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e CI="${CI:-}" \
	-e E2E_BASE_URL="$url" -e E2E_CREDENTIALS=/creds/credentials.json -e E2E_FIXTURES=/creds/fixtures.json \
	-e E2E_REPORT_DIR=/report \
	-v "$REPO_ROOT/e2e:/e2e" -v "$out:/creds:ro" -v "$out:/report" -w /e2e "$PLAYWRIGHT_IMAGE" \
	bash -lc 'test -d node_modules/@playwright/test || npm ci --no-audit --no-fund >/dev/null; npx playwright test "$@"' \
	e2e "${pw_args[@]}"
