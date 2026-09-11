"""Compare observed ExecOps, permitting only the specified instrumentation."""

import json
import sys
from pathlib import Path


def compare(stock, patched):
    files = sorted(stock.glob('*.json'))
    assert {p.name for p in files} == {p.name for p in patched.glob('*.json')}
    for path in files:
        before = json.loads(path.read_text())
        after = json.loads((patched / path.name).read_text())
        for ex in before['execs']:
            ex['meta']['env'] = sorted(ex['meta']['env'])
        for ex in after['execs']:
            ex['meta']['env'] = [env.replace('PATH=/run/opensandbox-apt/bin:', 'PATH=', 1)
                                 if env.startswith('PATH=/run/opensandbox-apt/bin:') else env
                                 for env in ex['meta']['env']]
            extra = [mount for mount in ex['mounts'] if mount['dest'].startswith('/run/opensandbox-apt/')]
            assert len(extra) == 6, path
            for mount in extra:
                if mount['dest'].endswith('/shadow'):
                    assert mount['mountType'] == 4, mount
                else:
                    assert mount['mountType'] == 1, mount
                    assert not mount['secretOpt'].get('optional', False), mount
                    assert mount['secretOpt'].get('uid', 0) == 0, mount
            ex['mounts'] = [mount for mount in ex['mounts'] if mount not in extra]
            ex['meta']['env'] = sorted(ex['meta']['env'])
        assert before == after, path.name
    print(f'{len(files)} lowering differentials passed: all original ExecOp fields and image config preserved')


if __name__ == '__main__':
    compare(Path(sys.argv[1]), Path(sys.argv[2]))
