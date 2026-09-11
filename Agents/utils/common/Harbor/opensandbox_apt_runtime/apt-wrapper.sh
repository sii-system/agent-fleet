#!/bin/bash
set -euo pipefail

runtime=/run/opensandbox-apt
command_name=${0##*/}
real=/usr/bin/$command_name
if [ ! -x "$real" ]; then
    echo "[opensandbox apt] ERROR event=real-binary-unavailable" \
        "command=$command_name path=$real" >&2
    exit 127
fi

if [ -n "${APT_CONFIG:-}" ]; then
    echo "[opensandbox apt] WARNING event=custom-source-config" \
        "action=bypass environment=APT_CONFIG" >&2
    exec "$real" "$@"
fi

for argument in "$@"; do
    case "$argument" in
        -c|--config-file|-c*|--config-file=*|*Dir::Etc=*|*Dir::Etc::SourceList*|*Dir::Etc::SourceParts*|*RootDir*)
            echo "[opensandbox apt] WARNING event=custom-source-config" \
                "action=bypass argument=$argument" >&2
            exec "$real" "$@"
            ;;
    esac
done

shadow=$(mktemp -d "$runtime/shadow/invocation.XXXXXX") || {
    echo "[opensandbox apt] ERROR event=shadow-create-failed root=$runtime/shadow" >&2
    exit 125
}
original_sources=$shadow/original
source_view=$shadow/view
mkdir -p "$original_sources/sources.list.d" "$source_view/sources.list.d" || exit 125
if ! { printf '' > "$original_sources/sources.list" &&
       printf '' > "$source_view/sources.list" &&
       printf '' > "$shadow/seen"; }; then
    echo "[opensandbox apt] ERROR event=source-view-init-failed" >&2
    exit 125
fi

capture_source() {
    original=$1
    saved=$2
    destination=$3
    source_kind=$4
    display_file=$5
    if ! cat "$original" > "$saved"; then
        echo "[opensandbox apt] ERROR event=source-copy-failed file=$display_file" >&2
        exit 125
    fi
    if ! awk -v map_file="$runtime/source-map" -v seen_file="$shadow/seen" \
        -v source_kind="$source_kind" -v display_file="$display_file" \
        -f "$runtime/source-rewriter.awk" "$saved" > "$destination"; then
        echo "[opensandbox apt] ERROR event=source-rewrite-failed file=$display_file" >&2
        exit 125
    fi
}

if [ -f /etc/apt/sources.list ]; then
    capture_source \
        /etc/apt/sources.list \
        "$original_sources/sources.list" "$source_view/sources.list" \
        list /etc/apt/sources.list
fi
for original in /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
    [ -f "$original" ] || continue
    name=${original##*/}
    case "$name" in
        *.list) source_kind=list ;;
        *.sources) source_kind=sources ;;
        *) continue ;;
    esac
    capture_source \
        "$original" \
        "$original_sources/sources.list.d/$name" \
        "$source_view/sources.list.d/$name" "$source_kind" \
        "/etc/apt/sources.list.d/$name"
done

run_with_source_view() {
    "$real" \
        -o "Dir::Etc::SourceList=$source_view/sources.list" \
        -o "Dir::Etc::SourceParts=$source_view/sources.list.d" \
        "$@"
}

is_update=0
skip_option_value=0
for argument in "$@"; do
    if [ "$skip_option_value" -eq 1 ]; then
        skip_option_value=0
        continue
    fi
    case "$argument" in
        -o|--option|-t|--target-release|--host-architecture)
            skip_option_value=1
            ;;
        update)
            is_update=1
            break
            ;;
        -*) ;;
        *) break ;;
    esac
done

if [ "$is_update" -ne 1 ]; then
    exec "$real" \
        -o "Dir::Etc::SourceList=$source_view/sources.list" \
        -o "Dir::Etc::SourceParts=$source_view/sources.list.d" \
        "$@"
fi

if run_with_source_view "$@"; then
    :
else
    apt_status=$?
    exit "$apt_status"
fi

reconciliation_error() {
    echo "[opensandbox apt] ERROR event=index-reconciliation-failed phase=$1" >&2
    return 1
}

restore_original_view() {
    if ! cat "$original_sources/sources.list" > "$source_view/sources.list"; then
        reconciliation_error restore-source-list
        return 1
    fi
    rm -f "$source_view/sources.list.d/"*.list \
        "$source_view/sources.list.d/"*.sources || {
        reconciliation_error clear-source-parts
        return 1
    }
    for saved in \
        "$original_sources/sources.list.d/"*.list \
        "$original_sources/sources.list.d/"*.sources; do
        [ -f "$saved" ] || continue
        if ! cp -a "$saved" "$source_view/sources.list.d/${saved##*/}"; then
            reconciliation_error restore-source-parts
            return 1
        fi
    done
}

reconcile_indexes() {
    for utility in /usr/bin/apt-get sort join cp; do
        if ! command -v "$utility" >/dev/null 2>&1; then
            reconciliation_error missing-utility
            return 1
        fi
    done

    target_format='$(SOURCESENTRY)~$(IDENTIFIER)~$(RELEASE)~$(COMPONENT)~$(ARCHITECTURE)~$(CREATED_BY)~$(METAKEY)|$(COMPONENT)|$(FILENAME)'
    if ! /usr/bin/apt-get \
        -o "Dir::Etc::SourceList=$source_view/sources.list" \
        -o "Dir::Etc::SourceParts=$source_view/sources.list.d" \
        indextargets --no-release-info --format "$target_format" \
        > "$shadow/rewritten-targets"; then
        reconciliation_error rewritten-targets
        return 1
    fi
    restore_original_view || return 1
    if ! /usr/bin/apt-get \
        -o "Dir::Etc::SourceList=$source_view/sources.list" \
        -o "Dir::Etc::SourceParts=$source_view/sources.list.d" \
        indextargets --no-release-info --format "$target_format" \
        > "$shadow/original-targets"; then
        reconciliation_error original-targets
        return 1
    fi
    if ! sort "$shadow/rewritten-targets" -o "$shadow/rewritten-targets" \
        || ! sort "$shadow/original-targets" -o "$shadow/original-targets"; then
        reconciliation_error sort-targets
        return 1
    fi
    if ! join -t '|' "$shadow/rewritten-targets" "$shadow/original-targets" \
        > "$shadow/target-copies"; then
        reconciliation_error join-targets
        return 1
    fi

    while IFS='|' read -r key source_component source_path \
        target_component target_path; do
        [ -n "$key" ] || continue
        [ -n "$source_path" ] && [ -n "$target_path" ] || continue
        [ "$source_path" = "$target_path" ] && continue
        for candidate in "$source_path" "$source_path".*; do
            [ -f "$candidate" ] || continue
            suffix=${candidate#"$source_path"}
            if ! cp -a "$candidate" "$target_path$suffix"; then
                reconciliation_error copy-index-target
                return 1
            fi
        done

        [ -n "$source_component" ] || continue
        source_marker=_${source_component}_
        target_marker=_${target_component}_
        case "$source_path" in
            *"$source_marker"*) ;;
            *) continue ;;
        esac
        source_dist=${source_path%%"$source_marker"*}
        target_dist=${target_path%%"$target_marker"*}
        for release_file in InRelease Release Release.gpg; do
            [ -f "${source_dist}_${release_file}" ] || continue
            if ! cp -a "${source_dist}_${release_file}" \
                "${target_dist}_${release_file}"; then
                reconciliation_error copy-release-target
                return 1
            fi
        done
    done < "$shadow/target-copies"
}

if ! reconcile_indexes; then
    exit 125
fi
exit 0
