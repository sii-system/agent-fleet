// Byte limits also apply to JSONL: a single source line can contain megabytes.
import { open } from "node:fs/promises";

export const MAX_FILE_BYTES = 32 * 1024 * 1024;
export const MAX_TOOL_BYTES = 48 * 1024;
export const MAX_LINE_BYTES = 8 * 1024;
const TRUNCATED = "[Evidence truncated by analyzer byte limit]";
const LINE_TRUNCATED = " ... [line truncated] ... ";

function prefix(text, bytes) {
	return Buffer.from(text).subarray(0, Math.max(0, bytes)).toString("utf8").replace(/\uFFFD+$/, "");
}

function clipLine(text) {
	const data = Buffer.from(text);
	if (data.length <= MAX_LINE_BYTES) return { text, truncated: false };
	const half = Math.floor((MAX_LINE_BYTES - Buffer.byteLength(LINE_TRUNCATED)) / 2);
	const head = prefix(text, half);
	const tail = data.subarray(data.length - half).toString("utf8").replace(/^\uFFFD+/, "");
	return { text: head + LINE_TRUNCATED + tail, truncated: true };
}

export function matchExcerpt(text, index, length) {
	if (Buffer.byteLength(text) <= MAX_LINE_BYTES) return text;
	const start = Math.max(0, index - 256);
	const end = Math.min(text.length, index + Math.min(length, 1024) + 256);
	return `[Long line excerpt at character ${start}]: ${text.slice(start, end)} [end excerpt]`;
}

export function boundedLines(lines, offset, maxBytes = MAX_TOOL_BYTES) {
	const budget = Math.max(0, maxBytes - Buffer.byteLength(TRUNCATED) - 1);
	const output = [];
	let bytes = 0;
	let truncated = false;
	for (const line of lines) {
		const clipped = clipLine(line);
		const rendered = `L${offset + output.length}: ${clipped.text}`;
		const size = Buffer.byteLength(rendered) + (output.length ? 1 : 0);
		if (bytes + size > budget) break;
		output.push(rendered);
		bytes += size;
		truncated ||= clipped.truncated;
	}
	const count = output.length;
	truncated ||= count < lines.length;
	if (truncated) output.push(prefix(TRUNCATED, Math.max(0, maxBytes - bytes - (count ? 1 : 0))));
	return { text: output.join("\n"), count, truncated, lineEnd: offset + count - 1 };
}

export function boundedEntries(entries, { maxBytes = MAX_TOOL_BYTES, limit = Infinity, incomplete = false } = {}) {
	const output = [];
	let bytes = Buffer.byteLength(TRUNCATED) + 1;
	for (const entry of entries) {
		const size = Buffer.byteLength(entry) + 1;
		if (output.length >= limit || bytes + size > maxBytes) break;
		output.push(entry);
		bytes += size;
	}
	const count = output.length;
	const truncated = incomplete || count < entries.length;
	if (truncated) output.push(TRUNCATED);
	return { text: output.join("\n"), count, truncated };
}

export async function readTextFile(file) {
	const handle = await open(file, "r");
	try {
		const info = await handle.stat();
		if (info.size > MAX_FILE_BYTES) {
			throw new Error("File exceeds the 32 MiB analyzer evidence limit; inspect smaller task artifacts.");
		}
		const buffer = Buffer.alloc(info.size + 1);
		let size = 0;
		while (size < buffer.length) {
			const result = await handle.read(buffer, size, buffer.length - size, null);
			if (!result.bytesRead) break;
			size += result.bytesRead;
		}
		if (size === buffer.length) throw new Error("Evidence file grew during read; read it again.");
		const data = buffer.subarray(0, size);
		if (data.subarray(0, 4096).includes(0)) return undefined;
		return data.toString("utf8");
	} finally {
		await handle.close();
	}
}
