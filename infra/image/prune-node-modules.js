// Runtime image: an app's node_modules keeps only what the realtime server loads; every
// other package built the desk assets and is never run (ops/security/findings.md V-04).
//   node prune-node-modules.js APP_DIR [ROOT_PACKAGE ...]
// ROOT_PACKAGE: packages the app loads at run time; they keep their whole dependency
// closure. The app's direct dependencies keep their package.json and licence files, which
// Frappe's /attribution page lists; their code goes.
"use strict";
const fs = require("fs");
const path = require("path");

const [appArg, ...roots] = process.argv.slice(2);
if (!appArg) throw new Error("usage: prune-node-modules.js APP_DIR [ROOT_PACKAGE ...]");
const app = path.resolve(appArg);
const top = path.join(app, "node_modules");
if (!fs.existsSync(top)) process.exit(0);
const LICENCE = /^(licen[cs]e|copying|notice)/i;

function readJson(file) {
	return JSON.parse(fs.readFileSync(file, "utf8"));
}

// Node's lookup: node_modules of the requiring package's directory, then of each parent
function resolvePackage(name, fromDir) {
	for (let dir = fromDir; dir.startsWith(app); dir = path.dirname(dir)) {
		const candidate = path.join(dir, "node_modules", name);
		if (fs.existsSync(path.join(candidate, "package.json"))) return fs.realpathSync(candidate);
		if (path.dirname(dir) === dir) break;
	}
	return null;
}

const keep = new Set();
function visit(name, fromDir) {
	const dir = resolvePackage(name, fromDir);
	if (!dir || keep.has(dir)) return; // optional dependencies may be absent
	keep.add(dir);
	const pkg = readJson(path.join(dir, "package.json"));
	for (const dep of Object.keys({ ...pkg.dependencies, ...pkg.optionalDependencies })) visit(dep, dir);
}
for (const root of roots) {
	if (!resolvePackage(root, app)) throw new Error(`run-time package ${root} is not installed in ${app}`);
	visit(root, app);
}

const direct = new Set(Object.keys(readJson(path.join(app, "package.json")).dependencies || {}));
const remove = (p) => fs.rmSync(p, { recursive: true, force: true });
let removed = 0;
let stripped = 0;

function consider(pkgDir, name, parent) {
	if (keep.has(fs.realpathSync(pkgDir))) {
		const nested = path.join(pkgDir, "node_modules");
		if (fs.existsSync(nested)) prune(nested);
	} else if (parent === top && direct.has(name)) {
		for (const entry of fs.readdirSync(pkgDir)) {
			if (entry !== "package.json" && !LICENCE.test(entry)) remove(path.join(pkgDir, entry));
		}
		stripped++;
	} else {
		remove(pkgDir);
		removed++;
	}
}

function prune(dir) {
	for (const entry of fs.readdirSync(dir)) {
		const full = path.join(dir, entry);
		if (entry.startsWith(".")) {
			remove(full); // .bin, lockfile caches
		} else if (entry.startsWith("@")) {
			for (const sub of fs.readdirSync(full)) consider(path.join(full, sub), `${entry}/${sub}`, dir);
			if (fs.readdirSync(full).length === 0) fs.rmdirSync(full);
		} else {
			consider(full, entry, dir);
		}
	}
}

prune(top);
console.log(`${path.basename(app)}: kept ${keep.size} run-time packages, kept the licence data of ${stripped}, removed ${removed}`);
