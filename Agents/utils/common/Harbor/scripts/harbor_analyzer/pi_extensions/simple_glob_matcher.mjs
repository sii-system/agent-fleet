function matchesWildcardCharacter(value) {
	return value !== "\n" && value !== "\r" && value !== "\u2028" && value !== "\u2029";
}

function normalizeStars(pattern) {
	let normalized = "";
	for (let index = 0; index < pattern.length; index++) {
		const token = pattern[index];
		if (token === "*" && normalized.endsWith("*")) continue;
		normalized += token;
	}
	return normalized;
}

export function compileSimpleGlob(pattern) {
	const normalized = normalizeStars(pattern);
	// Bit i means the NFA is ready to consume normalized[i].
	let starMask = 0n;
	let questionMask = 0n;
	const literalMasks = new Map();
	for (let index = 0; index < normalized.length; index++) {
		const token = normalized[index];
		const bit = 1n << BigInt(index);
		if (token === "*") {
			starMask |= bit;
		} else if (token === "?") {
			questionMask |= bit;
		} else {
			literalMasks.set(token, (literalMasks.get(token) || 0n) | bit);
		}
	}
	const acceptBit = 1n << BigInt(normalized.length);
	// Adjacent stars are normalized, so one shift completes epsilon closure.
	const epsilonClosure = (states) => states | ((states & starMask) << 1n);
	const initialStates = epsilonClosure(1n);

	return (value) => {
		let states = initialStates;
		for (let index = 0; index < value.length; index++) {
			const token = value[index];
			let nextStates = 0n;
			if (matchesWildcardCharacter(token)) {
				nextStates |= states & starMask;
				nextStates |= (states & questionMask) << 1n;
			}
			const literalMask = literalMasks.get(token);
			if (literalMask !== undefined) {
				nextStates |= (states & literalMask) << 1n;
			}
			states = epsilonClosure(nextStates);
			if (states === 0n) return false;
		}
		return (states & acceptBit) !== 0n;
	};
}
