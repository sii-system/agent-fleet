"""Real flat repository update and original-source index reconciliation."""

import argparse
from pathlib import Path

from integration import (
    inspect_archive,
    materialize_apt_runtime_assets,
    prepare_frontend,
    run_build,
)

SOURCE = r'''
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources && printf '%s\n' 'deb [trusted=yes] http://original.invalid:18765/repo ./' >/etc/apt/sources.list.d/probe.list && sha256sum /etc/apt/sources.list.d/probe.list >/tmp/sources.sha256
RUN mkdir -p /tmp/apt-repo/repo && printf 'Package: opensandbox-index-probe\nVersion: 1.0\nArchitecture: all\nMaintainer: OpenSandbox Test <noreply@example.invalid>\nFilename: pool/probe.deb\nSize: 0\nMD5sum: d41d8cd98f00b204e9800998ecf8427e\nSHA256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855\nDescription: index reconciliation probe\n\n' >/tmp/apt-repo/repo/Packages; python3 -m http.server 18765 --bind 127.0.0.1 --directory /tmp/apt-repo >/tmp/apt-http.log 2>&1 & server=$!; trap 'kill "$server" 2>/dev/null || :' EXIT; ready=0; for attempt in 1 2 3 4 5 6 7 8 9 10; do if python3 -c 'import socket; socket.create_connection(("127.0.0.1", 18765), 0.2).close()'; then ready=1; break; fi; sleep 0.2; done; test "$ready" -eq 1; apt-get update && sha256sum -c /tmp/sources.sha256
RUN ["/bin/sh", "-c", "sha256sum -c /tmp/sources.sha256 && /usr/bin/apt-cache policy opensandbox-index-probe | tee /tmp/policy && grep -F '1.0' /tmp/policy && grep -F 'original.invalid:18765/repo' /tmp/policy"]
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    dockerfile = args.output / 'Dockerfile'
    dockerfile.write_text(f'FROM {args.base}\n' + SOURCE)
    secrets = materialize_apt_runtime_assets(args.output / 'assets', {
        'http://original.invalid:18765/repo': 'http://127.0.0.1:18765/repo',
    })
    build_contexts = {}
    build_args = prepare_frontend(secrets, args.output / 'assets', build_contexts=build_contexts)
    archive = args.output / 'image.tar'
    run_build(environment_dir=args.output, dockerfile=dockerfile, archive_path=archive,
              log_path=args.output / 'build.log', platform='linux/amd64', timeout_sec=180,
              build_args=build_args, build_contexts=build_contexts, secret_files=secrets, build_network='none')
    inspect_archive(archive)
    print('PASS: real update, source checksums, original-source apt-cache policy, OCI pollution')


if __name__ == '__main__':
    main()
