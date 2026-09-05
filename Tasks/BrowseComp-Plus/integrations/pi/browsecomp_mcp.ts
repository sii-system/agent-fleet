import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { appendFile, mkdir } from "fs/promises";
import { dirname } from "path";
import { Type } from "typebox";

type RpcResponse = { result?: any; error?: { code?: number; message?: string } };

const endpoint = process.env.BROWSECOMP_MCP_URL || "";
const eventLog = process.env.BROWSECOMP_EVENT_LOG || "/logs/agent/browsecomp-events.jsonl";
let requestId = 0;
let sessionId = "";
let initialized: Promise<void> | undefined;

function parseBody(body: string): RpcResponse {
	const trimmed = body.trim();
	if (!trimmed) return {};
	if (trimmed.startsWith("{")) return JSON.parse(trimmed);
	const data = trimmed
		.split(/\r?\n/)
		.filter((line) => line.startsWith("data:"))
		.map((line) => line.slice(5).trim())
		.filter((line) => line && line !== "[DONE]");
	if (!data.length) return {};
	return JSON.parse(data[data.length - 1]);
}

async function post(payload: object): Promise<RpcResponse> {
	if (!endpoint) throw new Error("BROWSECOMP_MCP_URL is not configured");
	const headers: Record<string, string> = {
		Accept: "application/json, text/event-stream",
		"Content-Type": "application/json",
	};
	if (sessionId) headers["Mcp-Session-Id"] = sessionId;
	const response = await fetch(endpoint, { method: "POST", headers, body: JSON.stringify(payload) });
	const assigned = response.headers.get("mcp-session-id");
	if (assigned) sessionId = assigned;
	const body = await response.text();
	if (!response.ok) throw new Error(`BrowseComp MCP HTTP ${response.status}: ${body.slice(0, 400)}`);
	return parseBody(body);
}

async function ensureInitialized(): Promise<void> {
	if (!initialized) {
		initialized = (async () => {
			const response = await post({
				jsonrpc: "2.0",
				id: ++requestId,
				method: "initialize",
				params: {
					protocolVersion: "2025-03-26",
					capabilities: {},
					clientInfo: { name: "agent-fleet-pi", version: "1" },
				},
			});
			if (response.error) throw new Error(response.error.message || "MCP initialize failed");
			await post({ jsonrpc: "2.0", method: "notifications/initialized", params: {} });
		})();
	}
	return initialized;
}

function collectDocids(value: any, output = new Set<string>()): Set<string> {
	if (Array.isArray(value)) for (const item of value) collectDocids(item, output);
	else if (value && typeof value === "object") {
		if (typeof value.docid === "string" || typeof value.docid === "number") output.add(String(value.docid));
		for (const item of Object.values(value)) collectDocids(item, output);
	} else if (typeof value === "string") {
		for (const match of value.matchAll(/"docid"\s*:\s*"([^"]+)"/g)) output.add(match[1]);
	}
	return output;
}

async function logEvent(tool: string, args: object, result: any, error?: string) {
	try {
		await mkdir(dirname(eventLog), { recursive: true });
		await appendFile(
			eventLog,
			`${JSON.stringify({ timestamp: new Date().toISOString(), tool, args, docids: [...collectDocids(result)], error })}\n`,
		);
	} catch {
		// Retrieval must not fail because optional telemetry cannot be written.
	}
}

function resultText(result: any): string {
	if (result?.structuredContent !== undefined) return JSON.stringify(result.structuredContent, null, 2);
	if (Array.isArray(result?.content)) {
		return result.content.map((item: any) => (item?.type === "text" ? item.text : JSON.stringify(item))).join("\n");
	}
	return JSON.stringify(result, null, 2);
}

async function callTool(name: string, args: object) {
	try {
		await ensureInitialized();
		const response = await post({ jsonrpc: "2.0", id: ++requestId, method: "tools/call", params: { name, arguments: args } });
		if (response.error) throw new Error(response.error.message || `${name} failed`);
		await logEvent(name, args, response.result);
		return { content: [{ type: "text" as const, text: resultText(response.result) }], details: response.result || {} };
	} catch (error: any) {
		const message = error instanceof Error ? error.message : String(error);
		await logEvent(name, args, undefined, message);
		return { content: [{ type: "text" as const, text: `BrowseComp ${name} error: ${message}` }], details: { error: true } };
	}
}

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "search",
		label: "BrowseComp corpus search",
		description: "Search the fixed BrowseComp-Plus corpus. Use iterative, specific queries and cite returned docids.",
		parameters: Type.Object({ query: Type.String({ description: "Corpus search query" }) }),
		async execute(_id, args) {
			return callTool("search", args);
		},
	});
	pi.registerTool({
		name: "get_document",
		label: "BrowseComp document",
		description: "Fetch the first 4096 tokens of a fixed-corpus document for a docid returned by search.",
		parameters: Type.Object({ docid: Type.String({ description: "Document identifier" }) }),
		async execute(_id, args) {
			return callTool("get_document", args);
		},
	});
}
