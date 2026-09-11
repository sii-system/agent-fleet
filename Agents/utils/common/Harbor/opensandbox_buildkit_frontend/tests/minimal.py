"""A static executable in scratch: no shell, distro, APT, or normal PATH."""

import argparse
import shutil
from pathlib import Path

from integration import (
    inspect_archive,
    materialize_apt_runtime_assets,
    prepare_frontend,
    run_build,
    stock_args,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--stock', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.probe, args.output / 'probe')
    dockerfile = args.output / 'Dockerfile'
    dockerfile.write_text('''FROM scratch AS first
COPY --chmod=0555 probe /probe
RUN ["/probe"]
FROM scratch
COPY --from=first /probe /probe
ENV PATH=""
WORKDIR /work
USER 1234:1234
RUN ["/probe", "quote\\\" slash\\\\ unicode☃"]
''')
    outputs = []
    for mode in ('stock', 'patched'):
        secrets = materialize_apt_runtime_assets(args.output / 'assets', {})
        build_contexts = {}
        build_args = prepare_frontend(secrets, args.output / 'assets', build_contexts=build_contexts) if mode == 'patched' else stock_args(args.stock, build_contexts)
        archive = args.output / (mode + '.tar')
        log = args.output / (mode + '.log')
        run_build(environment_dir=args.output, dockerfile=dockerfile, archive_path=archive,
                  log_path=log, platform='linux/amd64', timeout_sec=120,
                  build_args=build_args, build_contexts=build_contexts, secret_files=secrets, build_network='none', no_cache=True)
        assert 'PROBE_OK uid=1234 cwd=/work' in log.read_text()
        outputs.append(inspect_archive(archive))
    assert outputs[0] == outputs[1]
    print('PASS: scratch, no shell, empty PATH, multistage, non-root and argv')


if __name__ == '__main__':
    main()
