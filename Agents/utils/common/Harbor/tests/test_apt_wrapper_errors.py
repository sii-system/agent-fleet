"""Run the Bash wrapper with isolated paths and stub APT; no host APT calls."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / 'opensandbox_apt_runtime/apt-wrapper.sh'


class AptWrapperErrorsTest(unittest.TestCase):
    def run_wrapper(self, *, command='update', apt_status=0, failure=''):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / 'runtime'
            (runtime / 'shadow').mkdir(parents=True)
            sources = root / 'sources'
            (sources / 'sources.list.d').mkdir(parents=True)
            (sources / 'sources.list').write_text('deb http://origin.invalid stable main\n')
            (runtime / 'source-map').write_text('')
            (runtime / 'source-rewriter.awk').write_text('{print}')
            tools = root / 'bin'
            tools.mkdir()
            calls = root / 'calls'
            apt = tools / 'apt-get'
            apt.write_text('#!/bin/sh\n'
                           'printf "%s\\n" "$*" >> "$CALLS"\n'
                           'case " $* " in *" indextargets "*) exit 0;; esac\n'
                           'exit "$APT_STATUS"\n')
            apt.chmod(0o755)
            if failure == 'initialization':
                stub = tools / 'mkdir'
                stub.write_text('#!/bin/sh\n/bin/mkdir "$@" || exit $?\n'
                                '/bin/mkdir "${2%/sources.list.d}/sources.list"\n')
                stub.chmod(0o755)
            elif failure:
                stub = tools / failure
                stub.write_text('#!/bin/sh\nexit 42\n')
                stub.chmod(0o755)
            # Relocate fixed runtime paths in a test-only copy; production keeps
            # its fixed paths and gains no environment override mechanism.
            script = root / 'apt-get'
            source = WRAPPER.read_text().replace('/run/opensandbox-apt', str(runtime))
            source = source.replace('/etc/apt', str(sources)).replace('/usr/bin/', str(tools) + '/')
            script.write_text(source)
            script.chmod(0o755)
            result = subprocess.run([str(script), command], env={
                'PATH': str(tools) + os.pathsep + os.defpath,
                'CALLS': str(calls), 'APT_STATUS': str(apt_status),
            }, capture_output=True, text=True, timeout=10, check=False)
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_update_success_reconciles(self):
        result, calls = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all('indextargets' in call for call in calls[1:]))

    def test_update_failure_preserves_status_without_reconciliation(self):
        result, calls = self.run_wrapper(apt_status=100)
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(len(calls), 1)

    def test_install_failure_preserves_status(self):
        result, calls = self.run_wrapper(command='install', apt_status=100)
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(len(calls), 1)

    def test_preparation_errors_never_call_apt(self):
        for failure, event in [('initialization', 'source-view-init-failed'),
                               ('cat', 'source-copy-failed'),
                               ('awk', 'source-rewrite-failed')]:
            with self.subTest(failure=failure):
                result, calls = self.run_wrapper(failure=failure)
                self.assertEqual(result.returncode, 125, result.stderr)
                self.assertIn(event, result.stderr)
                self.assertEqual(calls, [])

    def test_reconciliation_failure_is_not_hidden_by_conditional(self):
        result, calls = self.run_wrapper(failure='join')
        self.assertEqual(result.returncode, 125, result.stderr)
        self.assertIn('event=index-reconciliation-failed phase=join-targets', result.stderr)
        self.assertEqual(len(calls), 3)


if __name__ == '__main__':
    unittest.main()
