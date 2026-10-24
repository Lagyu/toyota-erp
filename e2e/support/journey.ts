// Journey stage marks: one line per stage with the elapsed time, in the run log and in the
// report's stdout, so a slow or failing stage is visible at a glance.
export function stages(journey: string) {
	const started = Date.now();
	return (stage: string) => console.log(`${journey} +${((Date.now() - started) / 1000).toFixed(1)}s  ${stage}`);
}
