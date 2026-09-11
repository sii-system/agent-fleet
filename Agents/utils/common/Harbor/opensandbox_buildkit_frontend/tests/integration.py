"""Serial executor differential; artifacts and per-build logs stay in --output."""

import argparse
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from corpus import corpus
from opensandbox_buildkit_frontend import prepare_frontend
from opensandbox_image_manager import (
    materialize_apt_runtime_assets,
    run_build,
    schema2_manifest,
)

STUB = '''#!/usr/local/bin/python3
import json, os, sys
args = sys.argv[1:]
intercepted = bool(args and args[0] == '-o')
if intercepted:
    assert args[1].startswith('Dir::Etc::SourceList=/run/opensandbox-apt/shadow/')
    assert 'cache.invalid' in open(args[1].split('=', 1)[1]).read()
    args = args[4:]
with open('/tmp/observations', 'a') as out:
    out.write(json.dumps(dict(argv=args, command=os.path.basename(sys.argv[0]),
        intercepted=intercepted, uid=os.getuid(), cwd=os.getcwd(),
        home=os.environ.get('HOME'), path=os.environ.get('PATH'))) + '\\n')
'''


def inspect_archive(path):
    observed = []
    selected = {}
    with tarfile.open(path) as archive:
        _, descriptors = schema2_manifest(archive)
        config = json.load(archive.extractfile('blobs/sha256/' + descriptors[0]['digest'].split(':')[1]))['config']
        for descriptor in descriptors[1:]:
            blob = archive.extractfile('blobs/sha256/' + descriptor['digest'].split(':')[1])
            with tarfile.open(fileobj=blob, mode='r|*') as layer:
                for member in layer:
                    name = member.name.lstrip('./')
                    assert not name.startswith('run/opensandbox-apt/'), name
                    assert 'dockerfile-frontend' not in name, name
                    if name == 'tmp/observations' and member.isfile():
                        observed = [json.loads(line) for line in layer.extractfile(member).read().splitlines()]
                    if name in {'tmp/doc', 'tmp/install.sh', 'tmp/downloaded'} and member.isfile():
                        selected[name] = layer.extractfile(member).read().decode()
    assert '/run/opensandbox-apt' not in json.dumps(config)
    return config, observed, selected


def stock_args(reference, contexts):
    if reference.startswith('oci-layout://'):
        contexts['stock-frontend'] = reference
        return {'BUILDKIT_SYNTAX': 'stock-frontend'}
    return {'BUILDKIT_SYNTAX': reference}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--stock', required=True)
    parser.add_argument('--case', action='append')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = {}
    for name, fixture in corpus().items():
        if args.case and name not in args.case:
            continue
        root = args.output / name
        root.mkdir(exist_ok=True)
        (root / 'fake-apt').write_text(STUB)
        (root / 'sources.list').write_text('deb http://original.invalid/repo stable main\n')
        source = f'FROM {args.base} AS fixture-base\n' + (
            'COPY --chmod=0555 fake-apt /usr/bin/apt\n'
            'COPY --chmod=0555 fake-apt /usr/bin/apt-get\n'
            'COPY sources.list /etc/apt/sources.list\n'
        ) + fixture
        (root / 'Dockerfile').write_text(source)
        outputs = {}
        for mode in ('stock', 'patched'):
            secrets = materialize_apt_runtime_assets(root / 'assets', {'http://original.invalid/repo': 'http://cache.invalid/repo'})
            build_contexts = {}
            build_args = prepare_frontend(secrets, root / 'assets', build_contexts=build_contexts) if mode == 'patched' else stock_args(args.stock, build_contexts)
            archive = root / (mode + '.tar')
            if name in {'device', 'security'}:
                # Probe the actual builder, explicitly requesting entitlements.
                # A policy denial is a conditional skip, not an assumed capability.
                command = ['docker', 'buildx', 'build', '--progress=plain', '--provenance=false',
                           '--platform=linux/amd64', f'--output=type=oci,dest={archive}',
                           '--allow=device' if name == 'device' else '--allow=security.insecure',
                           '--allow=network.host']
                for key, context in build_contexts.items():
                    command.extend(["--build-context", f"{key}={context}"])
                for key in build_args:
                    command.extend(['--build-arg', key])
                for key, path in secrets.items():
                    command.extend(['--secret', f'id={key},src={path}'])
                command.append(str(root))
                log = root / (mode + '.log')
                with log.open('w') as out:
                    result = subprocess.run(command, env={**os.environ, **build_args},
                                            stdout=out, stderr=subprocess.STDOUT, timeout=120, check=False)
                if result.returncode:
                    detail = log.read_text()
                    denials = [line for line in detail.splitlines()
                               if 'not allowed' in line or 'not supported' in line]
                    if not denials:
                        raise RuntimeError(f'{name} build failed; see {log}')
                    results[name] = {'skip': denials[0]}
                    break
                outputs[mode] = inspect_archive(archive)
                continue
            try:
                run_build(environment_dir=root, dockerfile=root / 'Dockerfile', archive_path=archive,
                          log_path=root / (mode + '.log'), platform='linux/amd64', timeout_sec=120,
                          build_args=build_args, build_contexts=build_contexts, secret_files=secrets, build_network='default')
            except RuntimeError:
                if name != 'exit-code':
                    raise
                assert 'exit code: 37' in (root / (mode + '.log')).read_text()
                outputs[mode] = 'exit-37'
                continue
            assert name != 'exit-code'
            outputs[mode] = inspect_archive(archive)
        if name in results and isinstance(results[name], dict):
            print(f'{name}: {results[name]}', flush=True)
            continue
        if name != 'exit-code':
            stock_config, stock_calls, stock_files = outputs['stock']
            patched_config, patched_calls, patched_files = outputs['patched']
            assert stock_config == patched_config, (name, stock_config, patched_config)
            assert stock_files == patched_files, name
            bypass = name in {'absolute', 'json-absolute', 'reset-path', 'empty-env'}
            for call in stock_calls:
                assert not call.pop('intercepted')
            for call in patched_calls:
                assert call.pop('intercepted') == (not bypass), (name, call)
                path = call['path']
                if path and path.startswith('/run/opensandbox-apt/bin:'):
                    call['path'] = path.removeprefix('/run/opensandbox-apt/bin:')
            assert stock_calls == patched_calls, (name, stock_calls, patched_calls)
        results[name] = 'pass'
        print(f'{name}: pass', flush=True)
        (args.output / 'results.json').write_text(json.dumps(results, indent=2))
    (args.output / 'results.json').write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
