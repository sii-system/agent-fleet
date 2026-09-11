"""Source fixtures shared by offline lowering and real executor differentials."""

import itertools
import json
import shlex


def corpus():
    cases = {}
    calls = {
        "direct": "apt-get probe",
        "apt": "apt probe",
        "sh": "sh -c 'apt-get probe'",
        "bash": "bash -c 'apt-get probe'",
        "nested": '''bash -c 'sh -c "apt-get probe"' '''.strip(),
        "python": "python3 -c 'import subprocess; subprocess.run([\"apt-get\", \"probe\"], check=True)'",
    }
    # A bounded Cartesian product deliberately leaves all grammar to upstream.
    shapes = {
        "plain": "{}",
        "and": "echo before && {} && echo after",
        "semicolon": "echo before; {}; :",
        "if": "if true; then {}; fi",
        "subshell": "({})",
        "group": "{{ {}; }}",
    }
    for (call_name, call), (shape_name, shape) in itertools.product(calls.items(), shapes.items()):
        cases[f"shell-{call_name}-{shape_name}"] = "RUN " + shape.format(call) + "\n"
    for name, argv in {
        "apt": ["apt", "probe"],
        "apt-get": ["apt-get", "probe"],
        "sh": ["sh", "-c", "apt-get probe"],
        "bash": ["bash", "-c", "apt-get probe"],
        "python": ["python3", "-c", "import subprocess; subprocess.run(['apt-get', 'probe'], check=True)"],
        "escapes": ["printf", "%s", 'a "quote" \\ backslash \u2603\n'],
        "absolute": ["/usr/bin/apt-get", "probe"],
    }.items():
        cases[f"json-{name}"] = "RUN " + json.dumps(argv) + "\n"
    cases.update({
        "whitespace": "RUN\t  apt-get\tprobe   \n",
        "comment": "# before\nRUN apt-get probe # trailing\n",
        "continuation": "RUN apt-get \\\n    probe\n",
        "continuation-comment": "RUN echo before && \\\n    # between continuations\n    apt-get \\\n        probe && \\\n    echo after\n",
        "json-continuation": 'RUN ["apt-get", \\\n    "probe"]\n',
        "heredoc": "RUN <<EOF\napt-get probe\nEOF\n",
        "heredoc-quoted": "RUN <<'EOF'\n# comment\ntool=apt-get\n$tool probe\nEOF\n",
        "heredoc-tabs": "RUN <<-EOF\n\tapt-get probe\n\tEOF\n",
        "heredoc-bash": "RUN bash <<'EOF'\n[[ -n $BASH_VERSION ]]\napt-get probe\nEOF\n",
        "heredoc-multiple": "RUN cat <<A <<B >/tmp/doc && apt-get probe\na\nA\nb\nB\n",
        "shebang-python": "RUN <<EOF\n#!/usr/bin/env python3\nimport sys, subprocess\nassert sys.version_info.major == 3\nsubprocess.run(['apt-get', 'probe'], check=True)\nEOF\n",
        "shebang-bash": "RUN <<EOF\n#!/usr/bin/env bash\n[[ -n $BASH_VERSION ]] || exit 91\napt-get probe\nEOF\n",
        "shell-switch": 'SHELL ["/bin/bash", "-c"]\nRUN [[ -n $BASH_VERSION ]] && apt-get probe\nSHELL ["/bin/sh", "-c"]\nRUN apt-get probe\n',
        "generated": "RUN printf '#!/bin/sh\\napt-get probe\\n' >/tmp/install.sh && sh /tmp/install.sh\n",
        "arg-tool": "ARG TOOL=apt-get\nRUN $TOOL probe\n",
        "env-tool": "ENV TOOL=apt-get\nRUN $TOOL probe\n",
        "path-prefix": "ENV PATH=/custom/bin:$PATH\nRUN apt-get probe\n",
        "path-only": 'ENV PATH=/custom/bin\nRUN ["/bin/echo", "custom-path"]\n',
        "path-empty": 'ENV PATH=""\nRUN ["/bin/echo", "empty-path"]\n',
        "user-workdir": 'WORKDIR /tmp\nUSER 65534:65534\nENV HOME=/tmp CUSTOM=preserved\nRUN ["/usr/bin/id"]\n',
        "cache-mount": "RUN --mount=type=cache,target=/tmp/cache apt-get probe\n",
        "tmpfs-network": "RUN --mount=type=tmpfs,target=/tmp/work --network=none apt-get probe\n",
        "security": "RUN --security=insecure --network=host --mount=type=cache,target=/tmp/cache apt-get probe\n",
        "device": "RUN --device=example.com/device=probe,required=false apt-get probe\n",
        "absolute": "RUN /usr/bin/apt-get probe\n",
        "reset-path": "RUN PATH=/usr/bin apt-get probe\n",
        "empty-env": "RUN env -i apt-get probe\n",
        "negative-shell": "RUN echo hello\n",
        "negative-json": 'RUN ["printf", "%s", "hello"]\n',
        "negative-python": 'RUN python3 -c \'print("hello")\'\n',
        "negative-heredoc": "RUN <<EOF\n#!/usr/bin/env python3\nprint('hello')\nEOF\n",
        "exit-code": "RUN exit 37\n",
    })
    for interpreter, script in {
        'sh': 'apt-get probe\n',
        'bash': '[[ -n $BASH_VERSION ]] || exit 92\napt-get probe\n',
        'python3': 'import subprocess\nsubprocess.run(["apt-get", "probe"], check=True)\n',
    }.items():
        cases['downloaded-' + interpreter] = (
            'RUN mkdir -p /tmp/http && printf %b ' + shlex.quote(script.replace(chr(10), chr(92) + 'n'))
            + ' >/tmp/http/install; python3 -m http.server 18766 --bind 127.0.0.1 --directory /tmp/http >/tmp/http.log 2>&1 & server=$!; '
            + 'trap \'kill "$server" 2>/dev/null || :\' EXIT; '
            + 'for attempt in 1 2 3 4 5 6 7 8 9 10; do '
            + 'python3 -c \'import urllib.request; urllib.request.urlretrieve("http://127.0.0.1:18766/install", "/tmp/downloaded")\' && break; sleep 0.2; done; '
            + interpreter + ' /tmp/downloaded\n'
        )
    cases['environment-preservation'] = (
        'ENV HOME=/tmp CUSTOM=preserved\nWORKDIR /tmp\nUSER root\n'
        'RUN test "$HOME" = /tmp && test "$CUSTOM" = preserved && test "$PWD" = /tmp && apt-get probe\n'
        'ENTRYPOINT ["/bin/echo"]\nCMD ["preserved"]\n'
    )
    cases['stage-inheritance'] = (
        'ENV PATH=/custom/bin:$PATH\nRUN apt-get probe\n'
        'ENV SAVED_PATH=$PATH\nRUN case "$SAVED_PATH" in *opensandbox*) exit 93;; esac\n'
    )
    cases['multi-stage'] = (
        'FROM fixture-base AS a\nENV PATH=/a/bin:$PATH\nSHELL ["/bin/bash", "-c"]\nRUN apt-get probe\n'
        'FROM fixture-base AS b\nENV PATH=/b/bin:$PATH\nSHELL ["/bin/sh", "-c"]\nRUN ["apt", "probe"]\n'
        'COPY --from=a /tmp/observations /tmp/a-observations\n'
        'RUN echo non-apt\n'
    )
    return cases


if __name__ == "__main__":
    print(json.dumps(corpus(), indent=2))
