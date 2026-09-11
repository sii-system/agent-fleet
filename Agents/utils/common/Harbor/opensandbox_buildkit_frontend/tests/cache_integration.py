"""Exercise solver cache reuse and invalidation without relying on secret data."""

import argparse
import json
import tarfile
from pathlib import Path

from integration import (
    materialize_apt_runtime_assets,
    prepare_frontend,
    run_build,
    schema2_manifest,
)


def nonce(archive_path):
    result = None
    with tarfile.open(archive_path) as archive:
        _, descriptors = schema2_manifest(archive)
        for descriptor in descriptors[1:]:
            blob = archive.extractfile('blobs/sha256/' + descriptor['digest'].split(':')[1])
            with tarfile.open(fileobj=blob, mode='r|*') as layer:
                for member in layer:
                    if member.name.lstrip('./') == 'nonce':
                        result = layer.extractfile(member).read()
    assert result
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    dockerfile = args.output / 'Dockerfile'
    dockerfile.write_text(f'FROM {args.base}\nRUN ["python3", "-c", "import uuid; open(\'/nonce\', \'w\').write(str(uuid.uuid4()))"]\n')
    original = None
    results = {}
    for name in ('initial', 'repeat', 'OPENSANDBOX_APT_WRAPPER', 'OPENSANDBOX_APT_REWRITER', 'OPENSANDBOX_APT_SOURCE_MAP', 'OPENSANDBOX_FRONTEND_IDENTITY'):
        secrets = materialize_apt_runtime_assets(args.output / 'assets', {})
        build_contexts = {}
        build_args = prepare_frontend(secrets, args.output / 'assets', build_contexts=build_contexts)
        if name.startswith('OPENSANDBOX_'):
            old = build_args[name]
            # Python contract tests verify content -> IDs. Here isolate IDs ->
            # solver behavior, keeping the supplied bytes deliberately equal.
            build_args[name] = old + '-changed'
            secrets[build_args[name]] = secrets.pop(old)
        archive = args.output / (name + '.tar')
        run_build(environment_dir=args.output, dockerfile=dockerfile, archive_path=archive,
                  log_path=args.output / (name + '.log'), platform='linux/amd64', timeout_sec=120,
                  build_args=build_args, build_contexts=build_contexts, secret_files=secrets, build_network='none')
        value = nonce(archive)
        if name == 'initial':
            original = value
        elif name == 'repeat':
            assert value == original, 'identical instrumentation must reuse cache'
        else:
            assert value != original, f'{name} reused stale cache'
        results[name] = 'pass'
        print(name + ': pass', flush=True)
        (args.output / 'results.json').write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
