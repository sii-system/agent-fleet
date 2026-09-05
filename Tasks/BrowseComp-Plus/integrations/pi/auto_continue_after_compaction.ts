const CONTINUATION_PROMPT = `[BrowseComp automatic continuation]
Context compaction finished while the original research task was still incomplete.
Continue from the retained summary. Resume the next concrete search or document
inspection; do not treat compaction or this message as task completion. Stop only
after producing the required Explanation, Exact Answer, and Confidence response.`;

function textContent(content) {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.filter((part) => part && part.type === "text" && typeof part.text === "string")
		.map((part) => part.text)
		.join("\n");
}

function normalizedSectionLine(line) {
	return line
		.replace(/^\s{0,3}#{1,6}\s+/, "")
		.replace(/\*\*|__/g, "")
		.trim();
}

function sectionValue(text, sectionName) {
	const lines = text.split(/\r?\n/);
	const sectionPattern = new RegExp(`^${sectionName}\\s*:?\\s*(.*)$`, "i");
	const anySectionPattern = /^(?:Explanation|Exact Answer|Confidence)\s*:?/i;

	for (let index = 0; index < lines.length; index += 1) {
		const match = normalizedSectionLine(lines[index]).match(sectionPattern);
		if (!match) continue;

		if (match[1].trim()) return match[1].trim();

		for (let next = index + 1; next < lines.length; next += 1) {
			const value = normalizedSectionLine(lines[next]);
			if (!value || /^```/.test(value)) continue;
			if (anySectionPattern.test(value)) return "";
			return value;
		}
	}

	return "";
}

function hasCompleteBrowseCompAnswer(text) {
	return Boolean(sectionValue(text, "Exact Answer") && sectionValue(text, "Confidence"));
}

export default function (pi) {
	let lastAssistantText = "";

	pi.on("message_end", (event) => {
		if (event.message?.role !== "assistant") return;
		lastAssistantText = textContent(event.message.content);
	});

	pi.on("session_compact", (event) => {
		const enabled = !/^(?:0|false|no)$/i.test(
			process.env.BROWSECOMP_PI_AUTO_CONTINUE_COMPACTION || "1",
		);
		if (!enabled || event.reason !== "threshold" || event.willRetry) return;
		if (hasCompleteBrowseCompAnswer(lastAssistantText)) return;

		// session_compact fires while AgentSession is still streaming. Queueing a
		// follow-up here makes Pi's post-compaction hasQueuedMessages() check true,
		// so --print continues instead of emitting agent_settled immediately.
		pi.sendUserMessage(CONTINUATION_PROMPT, { deliverAs: "followUp" });
	});
}
