import assert from "node:assert/strict";
import { test } from "node:test";
import { SessionKnowledge } from "./completions.js";
import { buildSlashCommands, parseSlashInput } from "./registry.js";

test("parseSlashInput recognizes a bare command with no arguments", () => {
	assert.deepEqual(parseSlashInput("/doctor"), { name: "doctor", args: "" });
});

test("parseSlashInput splits the command name from its arguments", () => {
	assert.deepEqual(parseSlashInput("/run features"), { name: "run", args: "features" });
});

test("parseSlashInput trims surrounding whitespace from args", () => {
	assert.deepEqual(parseSlashInput("/explain   art_91b2   "), {
		name: "explain",
		args: "art_91b2",
	});
});

test("parseSlashInput returns null for free text (no leading slash)", () => {
	assert.equal(parseSlashInput("run everything"), null);
});

test("parseSlashInput returns null for a bare slash with no command name", () => {
	assert.equal(parseSlashInput("/"), null);
});

test("buildSlashCommands only exposes commands backed by a real cairn subcommand", () => {
	const commands = buildSlashCommands(new SessionKnowledge());
	const names = commands.map((c) => c.name).sort();
	// Every one of these maps 1:1 to src/cairn/cli.py — see registry.ts's
	// own docstring for why /sessions, /claims, /resume, /tree are absent.
	assert.deepEqual(names, [
		"clear",
		"doctor",
		"explain",
		"help",
		"memory",
		"model",
		"plan",
		"run",
		"settings",
		"status",
		"theme",
		"usage",
	]);
	assert.equal(names.includes("sessions"), false);
	assert.equal(names.includes("claims"), false);
});

test("/run argument completions include the real fixed pipeline stages", async () => {
	const commands = buildSlashCommands(new SessionKnowledge());
	const run = commands.find((c) => c.name === "run");
	assert.ok(run?.getArgumentCompletions);
	const suggestions = await run.getArgumentCompletions("");
	const values = (suggestions ?? []).map((s) => s.value);
	assert.deepEqual(values, ["env", "dataset", "features", "checkpoint", "eval", "--all"]);
});

test("/run argument completions filter by prefix", async () => {
	const commands = buildSlashCommands(new SessionKnowledge());
	const run = commands.find((c) => c.name === "run");
	const suggestions = await run?.getArgumentCompletions?.("check");
	assert.deepEqual((suggestions ?? []).map((s) => s.value), ["checkpoint"]);
});

test("/explain argument completions only suggest artifact ids seen this session", async () => {
	const knowledge = new SessionKnowledge();
	knowledge.noteArtifactId("art_91b2");
	knowledge.noteArtifactId("art_4f81");
	const commands = buildSlashCommands(knowledge);
	const explain = commands.find((c) => c.name === "explain");
	const suggestions = await explain?.getArgumentCompletions?.("art_9");
	assert.deepEqual((suggestions ?? []).map((s) => s.value), ["art_91b2"]);
});

test("/theme argument completions are the fixed theme names", async () => {
	const commands = buildSlashCommands(new SessionKnowledge());
	const theme = commands.find((c) => c.name === "theme");
	const suggestions = await theme?.getArgumentCompletions?.("cairn");
	assert.deepEqual(
		(suggestions ?? []).map((s) => s.value).sort(),
		["cairn-dark", "cairn-light"],
	);
});
