import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { constants } from "node:fs";
import { access, appendFile, lstat, readdir, readFile, realpath, stat } from "node:fs/promises";
import path from "node:path";

type AllowedPath = {
	raw: string;
	real: string;
	isDirectory: boolean;
};

type Policy = {
	allowed: AllowedPath[];
	auditPath?: string;
	cwd: string;
	grepLiteralOnly: boolean;
};

const MAX_READ_LINES = 1200;
const MAX_GREP_FILES = 5000;
const MAX_GREP_PATTERN_LENGTH = 1024;
const MAX_GLOB_LENGTH = 256;
const DEFAULT_LIMIT = 200;
const MAX_GREP_CONTEXT = 20;
const MAX_GREP_OUTPUT_BYTES = 50 * 1024;
const GREP_TRUNCATION_NOTICE =
	`[Output truncated by analyzer path gate: maximum ${DEFAULT_LIMIT} matches, ${MAX_GREP_CONTEXT} context lines, and ${MAX_GREP_OUTPUT_BYTES / 1024}KB UTF-8 output. Refine the search.]`;

const readSchema = {
	type: "object",
	properties: {
		path: { type: "string", description: "Allowed absolute or relative file path to read" },
		offset: { type: "number", description: "1-indexed first line to return" },
		limit: { type: "number", description: "Maximum number of lines to return" },
	},
	required: ["path"],
	additionalProperties: false,
} as any;

const grepSchema = {
	type: "object",
	properties: {
		pattern: { type: "string", description: `Pattern to search for (maximum ${MAX_GREP_PATTERN_LENGTH} characters)` },
		path: { type: "string", description: "Allowed file or directory to search" },
		glob: { type: "string", description: `Optional simple glob filter using * and ? (maximum ${MAX_GLOB_LENGTH} characters)` },
		ignoreCase: { type: "boolean" },
		literal: { type: "boolean", description: "Use literal string matching. A runtime policy may force literal matching even when false." },
		context: { type: "number" },
		limit: { type: "number" },
	},
	required: ["pattern"],
	additionalProperties: false,
} as any;

const findSchema = {
	type: "object",
	properties: {
		pattern: { type: "string", description: `Simple * and ? glob pattern to find (maximum ${MAX_GLOB_LENGTH} characters)` },
		path: { type: "string", description: "Allowed directory to search" },
		limit: { type: "number" },
	},
	required: ["pattern"],
	additionalProperties: false,
} as any;

const lsSchema = {
	type: "object",
	properties: {
		path: { type: "string", description: "Allowed directory to list" },
		limit: { type: "number" },
	},
	additionalProperties: false,
} as any;

let policyPromise: Promise<Policy> | undefined;

function absolutePath(cwd: string, requested: string): string {
	return path.resolve(cwd, requested || ".");
}

function isInside(root: string, target: string): boolean {
	const relative = path.relative(root, target);
	return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

async function loadPolicy(cwd: string): Promise<Policy> {
	if (policyPromise) return policyPromise;
	policyPromise = (async () => {
		const rawPaths = JSON.parse(process.env.HARBOR_ANALYZER_ALLOWED_PATHS_JSON || "[]");
		const allowed: AllowedPath[] = [];
		if (Array.isArray(rawPaths)) {
			for (const raw of rawPaths) {
				if (typeof raw !== "string" || raw.trim() === "") continue;
				const absolute = absolutePath(cwd, raw);
				try {
					const resolved = await realpath(absolute);
					const info = await stat(resolved);
					allowed.push({ raw: absolute, real: resolved, isDirectory: info.isDirectory() });
				} catch {
					// Missing allowlist entries are ignored; the analyzer Python side records the intended list.
				}
			}
		}
		return {
			allowed,
			auditPath: process.env.HARBOR_ANALYZER_ACCESS_AUDIT_PATH || undefined,
			cwd,
			grepLiteralOnly: process.env.HARBOR_ANALYZER_GREP_LITERAL_ONLY === "1",
		};
	})();
	return policyPromise;
}

async function writeAudit(policy: Policy, record: Record<string, unknown>) {
	if (!policy.auditPath) return;
	const payload = {
		ts: new Date().toISOString(),
		...record,
	};
	try {
		await appendFile(policy.auditPath, `${JSON.stringify(payload)}\n`, "utf8");
	} catch {
		// Audit write failures must not expose broader filesystem access.
	}
}

async function resolveAllowed(
	policy: Policy,
	tool: string,
	requested: string,
): Promise<{ absolute: string; real: string; allowedRoot: string } | { denied: string; absolute: string; real?: string }> {
	const absolute = absolutePath(policy.cwd, requested);
	let resolved: string | undefined;
	try {
		resolved = await realpath(absolute);
	} catch (error: any) {
		await writeAudit(policy, {
			tool,
			requested_path: requested,
			absolute_path: absolute,
			allowed: false,
			reason: "path_not_found",
			error: error?.message,
		});
		return { denied: `Path not found or unreadable: ${requested}`, absolute };
	}
	for (const root of policy.allowed) {
		const allowed = root.isDirectory ? isInside(root.real, resolved) : root.real === resolved;
		if (allowed) {
			await writeAudit(policy, {
				tool,
				requested_path: requested,
				absolute_path: absolute,
				resolved_path: resolved,
				allowed: true,
				allowed_root: root.raw,
			});
			return { absolute, real: resolved, allowedRoot: root.raw };
		}
	}
	await writeAudit(policy, {
		tool,
		requested_path: requested,
		absolute_path: absolute,
		resolved_path: resolved,
		allowed: false,
		reason: "outside_allowed_paths",
	});
	return { denied: `Access denied: ${requested} is outside analyzer allowed evidence paths.`, absolute, real: resolved };
}

function redactLine(line: string): { text: string; redacted: boolean } {
	let redacted = false;
	let text = line;
	text = text.replace(
		/(^|[^A-Za-z0-9_])([A-Za-z0-9_.-]*(?:api[_-]?key|auth[_-]?token|access[_-]?key|secret|token|password)[A-Za-z0-9_.-]*)\s*([:=])\s*['"]?[^'"\s,}]+/gi,
		(_match, prefix, key, separator) => {
			redacted = true;
			return `${prefix}${key}${separator}<REDACTED>`;
		},
	);
	text = text.replace(/\b(authorization)\s*[:=]\s*['"]?bearer\s+[^'"\s,}]+/gi, (_match, key) => {
		redacted = true;
		return `${key}=Bearer <REDACTED>`;
	});
	for (const [pattern, replacement] of [
		[/\bBearer\s+[A-Za-z0-9._~+/=-]{12,}/g, "Bearer <REDACTED>"],
		[/\bsk-[A-Za-z0-9_-]{12,}\b/g, "sk-<REDACTED>"],
		[/\bgh[pousr]_[A-Za-z0-9_]{12,}\b/g, "gh_<REDACTED>"],
	] as Array<[RegExp, string]>) {
		text = text.replace(pattern, () => {
			redacted = true;
			return replacement;
		});
	}
	return { text, redacted };
}

function redactLines(lines: string[]): { lines: string[]; redacted: boolean } {
	let anyRedacted = false;
	const redactedLines = lines.map((line) => {
		const result = redactLine(line);
		if (result.redacted) anyRedacted = true;
		return result.text;
	});
	return { lines: redactedLines, redacted: anyRedacted };
}

function formatLines(lines: string[], offset: number): string {
	return lines.map((line, index) => `L${offset + index}: ${line}`).join("\n");
}

function utf8Prefix(text: string, maxBytes: number): string {
	const buffer = Buffer.from(text, "utf8");
	if (buffer.length <= maxBytes) return text;
	let end = Math.max(0, maxBytes);
	while (end > 0 && (buffer[end] & 0xc0) === 0x80) {
		end--;
	}
	return buffer.subarray(0, end).toString("utf8");
}

function wildcardMatches(pattern: string, value: string): boolean {
	let patternIndex = 0;
	let valueIndex = 0;
	let starIndex = -1;
	let starValueIndex = 0;
	while (valueIndex < value.length) {
		if (
			patternIndex < pattern.length &&
			(pattern[patternIndex] === "?" || pattern[patternIndex] === value[valueIndex])
		) {
			patternIndex++;
			valueIndex++;
		} else if (patternIndex < pattern.length && pattern[patternIndex] === "*") {
			starIndex = patternIndex;
			patternIndex++;
			starValueIndex = valueIndex;
		} else if (starIndex !== -1) {
			patternIndex = starIndex + 1;
			starValueIndex++;
			valueIndex = starValueIndex;
		} else {
			return false;
		}
	}
	while (patternIndex < pattern.length && pattern[patternIndex] === "*") {
		patternIndex++;
	}
	return patternIndex === pattern.length;
}

async function collectFiles(root: string, limit: number): Promise<string[]> {
	const files: string[] = [];
	async function visit(current: string) {
		if (files.length >= limit) return;
		let info;
		try {
			info = await lstat(current);
		} catch {
			return;
		}
		if (info.isSymbolicLink()) {
			return;
		}
		if (info.isFile()) {
			files.push(current);
			return;
		}
		if (!info.isDirectory()) return;
		let entries: string[];
		try {
			entries = await readdir(current);
		} catch {
			return;
		}
		for (const entry of entries.sort()) {
			if (files.length >= limit) return;
			await visit(path.join(current, entry));
		}
	}
	await visit(root);
	return files;
}

function isProbablyText(buffer: Buffer): boolean {
	return !buffer.subarray(0, 4096).includes(0);
}

async function readTextFile(file: string): Promise<string | undefined> {
	const buffer = await readFile(file);
	if (!isProbablyText(buffer)) return undefined;
	return buffer.toString("utf8");
}

export default function analyzerPathGate(pi: ExtensionAPI) {
	pi.registerTool({
		name: "read",
		label: "read allowed analyzer evidence",
		description: "Read a file only from analyzer-allowed evidence paths. Secret-looking values are redacted in the returned text.",
		parameters: readSchema,
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const policy = await loadPolicy(ctx.cwd);
			const resolved = await resolveAllowed(policy, "read", params.path);
			if ("denied" in resolved) {
				return { content: [{ type: "text", text: resolved.denied }], details: { blocked: true } };
			}
			try {
				await access(resolved.real, constants.R_OK);
				const info = await stat(resolved.real);
				if (!info.isFile()) {
					return { content: [{ type: "text", text: `Not a file: ${params.path}` }], details: { error: true } };
				}
				const text = await readTextFile(resolved.real);
				if (text === undefined) {
					return { content: [{ type: "text", text: `Binary file skipped: ${resolved.absolute}` }], details: { binary: true } };
				}
				const allLines = text.split(/\r?\n/);
				const start = Math.max(1, Math.floor(params.offset ?? 1));
				const requestedLimit = Math.max(1, Math.floor(params.limit ?? MAX_READ_LINES));
				const limit = Math.min(requestedLimit, MAX_READ_LINES);
				const selected = allLines.slice(start - 1, start - 1 + limit);
				const redacted = redactLines(selected);
				await writeAudit(policy, {
					tool: "read",
					requested_path: params.path,
					absolute_path: resolved.absolute,
					resolved_path: resolved.real,
					allowed: true,
					line_start: start,
					line_end: start + selected.length - 1,
					redacted: redacted.redacted,
					truncated: start - 1 + limit < allLines.length,
				});
				const header = `Path: ${resolved.absolute}\nLines: ${start}-${start + selected.length - 1}${redacted.redacted ? "\nRedaction: secret-looking values replaced with <REDACTED>" : ""}`;
				const suffix = start - 1 + limit < allLines.length ? "\n[Output truncated by analyzer path gate]" : "";
				return { content: [{ type: "text", text: `${header}\n${formatLines(redacted.lines, start)}${suffix}` }], details: { redacted: redacted.redacted } };
			} catch (error: any) {
				return { content: [{ type: "text", text: `Error reading ${params.path}: ${error?.message || error}` }], details: { error: true } };
			}
		},
	});

	pi.registerTool({
		name: "ls",
		label: "list allowed analyzer evidence",
		description: "List an analyzer-allowed evidence directory.",
		parameters: lsSchema,
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const policy = await loadPolicy(ctx.cwd);
			const requested = params.path || ".";
			const resolved = await resolveAllowed(policy, "ls", requested);
			if ("denied" in resolved) {
				return { content: [{ type: "text", text: resolved.denied }], details: { blocked: true } };
			}
			const info = await stat(resolved.real);
			if (!info.isDirectory()) {
				return { content: [{ type: "text", text: `Path: ${resolved.absolute}\n(file)` }], details: {} };
			}
			const limit = Math.max(1, Math.floor(params.limit ?? DEFAULT_LIMIT));
			const entries = (await readdir(resolved.real, { withFileTypes: true }))
				.sort((a, b) => a.name.localeCompare(b.name))
				.slice(0, limit)
				.map((entry) => `${entry.name}${entry.isDirectory() ? "/" : ""}`);
			return { content: [{ type: "text", text: `Path: ${resolved.absolute}\n${entries.join("\n")}` }], details: { entry_count: entries.length } };
		},
	});

	pi.registerTool({
		name: "find",
		label: "find allowed analyzer evidence",
		description: "Find files under an analyzer-allowed evidence directory using a simple glob pattern.",
		parameters: findSchema,
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			const findPattern = params.pattern || "*";
			if (findPattern.length > MAX_GLOB_LENGTH) {
				return {
					content: [{ type: "text", text: `find pattern exceeds ${MAX_GLOB_LENGTH} characters` }],
					details: { error: true, max_pattern_length: MAX_GLOB_LENGTH },
				};
			}
			const policy = await loadPolicy(ctx.cwd);
			const requested = params.path || ".";
			const resolved = await resolveAllowed(policy, "find", requested);
			if ("denied" in resolved) {
				return { content: [{ type: "text", text: resolved.denied }], details: { blocked: true } };
			}
			const info = await stat(resolved.real);
			const candidates = info.isDirectory() ? await collectFiles(resolved.real, MAX_GREP_FILES) : [resolved.real];
			const limit = Math.max(1, Math.floor(params.limit ?? DEFAULT_LIMIT));
			const matches = candidates
				.filter(
					(file) =>
						wildcardMatches(findPattern, path.basename(file)) ||
						wildcardMatches(findPattern, path.relative(resolved.real, file)),
				)
				.slice(0, limit);
			return { content: [{ type: "text", text: matches.join("\n") || "No matches" }], details: { match_count: matches.length } };
		},
	});

	pi.registerTool({
		name: "grep",
		label: "grep allowed analyzer evidence",
		description: "Search text files under analyzer-allowed evidence paths. A runtime policy may force literal string matching regardless of the literal argument. Secret-looking values are redacted in returned lines.",
		parameters: grepSchema,
		async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
			if (params.pattern.length > MAX_GREP_PATTERN_LENGTH) {
				return {
					content: [{ type: "text", text: `grep pattern exceeds ${MAX_GREP_PATTERN_LENGTH} characters` }],
					details: { error: true, max_pattern_length: MAX_GREP_PATTERN_LENGTH },
				};
			}
			if (params.glob && params.glob.length > MAX_GLOB_LENGTH) {
				return {
					content: [{ type: "text", text: `grep glob exceeds ${MAX_GLOB_LENGTH} characters` }],
					details: { error: true, max_glob_length: MAX_GLOB_LENGTH },
				};
			}
			const policy = await loadPolicy(ctx.cwd);
			const requested = params.path || ".";
			const resolved = await resolveAllowed(policy, "grep", requested);
			if ("denied" in resolved) {
				return { content: [{ type: "text", text: resolved.denied }], details: { blocked: true } };
			}
			const info = await stat(resolved.real);
			const files = info.isDirectory() ? await collectFiles(resolved.real, MAX_GREP_FILES) : [resolved.real];
			const glob = params.glob || undefined;
			const requestedLimit = Math.max(1, Math.floor(params.limit ?? DEFAULT_LIMIT));
			const limit = Math.min(requestedLimit, DEFAULT_LIMIT);
			const requestedContext = Math.max(0, Math.floor(params.context ?? 0));
			const context = Math.min(requestedContext, MAX_GREP_CONTEXT);
			const flags = params.ignoreCase ? "i" : "";
			let pattern: RegExp | undefined;
			let effectiveLiteral = policy.grepLiteralOnly || params.literal === true;
			if (!effectiveLiteral) {
				try {
					pattern = new RegExp(params.pattern, flags);
				} catch {
					effectiveLiteral = true;
				}
			}
			const needle = params.ignoreCase ? params.pattern.toLowerCase() : params.pattern;
			const noticeBytes = Buffer.byteLength(`\n${GREP_TRUNCATION_NOTICE}`, "utf8");
			const outputBudget = MAX_GREP_OUTPUT_BYTES - noticeBytes;
			let output = "";
			let outputBytes = 0;
			let outputByteLimitReached = false;
			let matchLimitReached = false;
			let matchCount = 0;
			const matches: Array<{ path: string; resolved_path: string; line_start: number; line_end: number }> = [];
			let redactedAny = false;
			search: for (const file of files) {
				if (matchCount >= limit) {
					matchLimitReached = true;
					break;
				}
				const rel = path.relative(resolved.real, file);
				if (
					glob &&
					!(
						wildcardMatches(glob, path.basename(file)) ||
						wildcardMatches(glob, rel)
					)
				) continue;
				let text: string | undefined;
				try {
					text = await readTextFile(file);
				} catch {
					continue;
				}
				if (text === undefined) continue;
				const lines = text.split(/\r?\n/);
				for (let index = 0; index < lines.length; index++) {
					if (matchCount >= limit) {
						matchLimitReached = true;
						break search;
					}
					const haystack = params.ignoreCase ? lines[index].toLowerCase() : lines[index];
					const matched = pattern ? pattern.test(lines[index]) : haystack.includes(needle);
					if (!matched) continue;
					const start = Math.max(0, index - context);
					const end = Math.min(lines.length - 1, index + context);
					const selected = lines.slice(start, end + 1);
					const redacted = redactLines(selected);
					if (redacted.redacted) redactedAny = true;
					const separator = output ? "\n---\n" : "";
					const block = `${separator}File: ${file}\n${formatLines(redacted.lines, start + 1)}`;
					const blockBytes = Buffer.byteLength(block, "utf8");
					const remainingBytes = outputBudget - outputBytes;
					if (blockBytes <= remainingBytes) {
						output += block;
						outputBytes += blockBytes;
					} else {
						const prefix = utf8Prefix(block, remainingBytes);
						const lastCompleteLine = prefix.lastIndexOf("\n");
						const completePrefix = lastCompleteLine > 0 ? prefix.slice(0, lastCompleteLine) : "";
						if (completePrefix) {
							output += completePrefix;
							outputBytes += Buffer.byteLength(completePrefix, "utf8");
						}
						outputByteLimitReached = true;
					}
					matchCount++;
					matches.push({ path: file, resolved_path: file, line_start: start + 1, line_end: end + 1 });
					if (outputByteLimitReached) break search;
				}
			}
			const truncated =
				requestedLimit > limit ||
				requestedContext > context ||
				matchLimitReached ||
				outputByteLimitReached;
			let content = output || "No matches";
			if (truncated) {
				content += `${output ? "\n" : ""}${GREP_TRUNCATION_NOTICE}`;
			}
			const finalOutputBytes = Buffer.byteLength(content, "utf8");
			await writeAudit(policy, {
				tool: "grep",
				requested_path: requested,
				absolute_path: resolved.absolute,
				resolved_path: resolved.real,
				allowed: true,
				pattern: params.pattern,
				match_count: matchCount,
				matches,
				redacted: redactedAny,
				truncated,
				requested_limit: requestedLimit,
				effective_limit: limit,
				requested_context: requestedContext,
				effective_context: context,
				output_bytes: finalOutputBytes,
				output_byte_limit: MAX_GREP_OUTPUT_BYTES,
				effective_literal: effectiveLiteral,
				literal_forced: policy.grepLiteralOnly,
			});
			return {
				content: [{ type: "text", text: content }],
				details: {
					match_count: matchCount,
					redacted: redactedAny,
					truncated,
					requested_limit: requestedLimit,
					effective_limit: limit,
					requested_context: requestedContext,
					effective_context: context,
					output_bytes: finalOutputBytes,
					output_byte_limit: MAX_GREP_OUTPUT_BYTES,
					match_limit_reached: matchLimitReached,
					output_byte_limit_reached: outputByteLimitReached,
					effective_literal: effectiveLiteral,
					literal_forced: policy.grepLiteralOnly,
				},
			};
		},
	});
}
