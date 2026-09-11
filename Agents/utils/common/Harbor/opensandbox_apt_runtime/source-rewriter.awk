BEGIN {
    while ((getline mapping < map_file) > 0) {
        separator = index(mapping, "\t")
        if (!separator) continue
        origins[++mapping_count] = substr(mapping, 1, separator - 1)
        replacements[mapping_count] = substr(mapping, separator + 1)
    }
    close(map_file)
    while ((getline seen_uri < seen_file) > 0) seen[seen_uri] = 1
    close(seen_file)
}

function prefix_matches(prefix, uri, suffix) {
    if (substr(uri, 1, length(prefix)) != prefix) return 0
    suffix = substr(uri, length(prefix) + 1)
    if (suffix == "") return 1
    return substr(suffix, 1, 1) == "/"
}

function display_uri(uri, clean, scheme_length, rest) {
    clean = uri
    if (clean ~ /^https?:\/\//) {
        scheme_length = (substr(clean, 1, 5) == "https") ? 8 : 7
        rest = substr(clean, scheme_length + 1)
        sub(/^[^/@]*@/, "", rest)
        clean = substr(clean, 1, scheme_length) rest
    }
    sub(/[?#].*$/, "", clean)
    return clean
}

function rewrite_uri(uri, best, best_length, i) {
    if (uri !~ /^https?:\/\//) return uri
    for (i = 1; i <= mapping_count; i++) {
        if (prefix_matches(replacements[i], uri)) return uri
    }
    best = 0
    best_length = -1
    for (i = 1; i <= mapping_count; i++) {
        if (length(origins[i]) > best_length && prefix_matches(origins[i], uri)) {
            best = i
            best_length = length(origins[i])
        }
    }
    if (best) {
        return replacements[best] substr(uri, length(origins[best]) + 1)
    }
    if (!seen[uri]) {
        printf "[opensandbox apt] WARNING event=unmapped-source " \
            "source=%s file=%s\n", display_uri(uri), display_file > "/dev/stderr"
        seen[uri] = 1
        print uri >> seen_file
        close(seen_file)
    }
    return uri
}

function rewrite_uri_fields(text, output, rest, token) {
    output = ""
    rest = text
    while (match(rest, /[^[:space:]]+/)) {
        output = output substr(rest, 1, RSTART - 1)
        token = substr(rest, RSTART, RLENGTH)
        output = output rewrite_uri(token)
        rest = substr(rest, RSTART + RLENGTH)
    }
    return output rest
}

function rewrite_list_line(line, probe, close_bracket, uri, position) {
    probe = line
    sub(/^[[:space:]]*/, "", probe)
    if (probe ~ /^#/ || probe !~ /^(deb|deb-src)[[:space:]]+/) return line
    sub(/^(deb|deb-src)[[:space:]]+/, "", probe)
    if (substr(probe, 1, 1) == "[") {
        close_bracket = index(probe, "]")
        if (!close_bracket) return line
        probe = substr(probe, close_bracket + 1)
        sub(/^[[:space:]]+/, "", probe)
    }
    uri = probe
    sub(/[[:space:]].*$/, "", uri)
    if (uri == "") return line
    position = index(line, uri)
    if (!position) return line
    return substr(line, 1, position - 1) rewrite_uri(uri) \
        substr(line, position + length(uri))
}

function flush_stanza(disabled, i, lower, colon, uri_continuation) {
    if (!stanza_count) return
    disabled = 0
    for (i = 1; i <= stanza_count; i++) {
        lower = tolower(stanza[i])
        if (lower ~ /^enabled:[[:space:]]*no([[:space:]]|$)/) disabled = 1
    }
    uri_continuation = 0
    for (i = 1; i <= stanza_count; i++) {
        lower = tolower(stanza[i])
        if (!disabled && lower ~ /^uris:[[:space:]]*/) {
            colon = index(stanza[i], ":")
            stanza[i] = substr(stanza[i], 1, colon) \
                rewrite_uri_fields(substr(stanza[i], colon + 1))
            uri_continuation = 1
        } else if (!disabled && uri_continuation && stanza[i] ~ /^[[:space:]]+/) {
            stanza[i] = rewrite_uri_fields(stanza[i])
        } else {
            uri_continuation = 0
        }
        print stanza[i]
        delete stanza[i]
    }
    stanza_count = 0
}

source_kind == "list" { print rewrite_list_line($0); next }
/^[[:space:]]*$/ { flush_stanza(); print; next }
{ stanza[++stanza_count] = $0 }
END { if (source_kind != "list") flush_stanza() }
