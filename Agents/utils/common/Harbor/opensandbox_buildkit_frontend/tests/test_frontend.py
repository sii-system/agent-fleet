import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from corpus import corpus
from opensandbox_buildkit_frontend import (
    FrontendBuildError,
    ensure_frontend,
    layout_digest,
    prepare_frontend,
    source_identity,
)
from opensandbox_image_manager import (
    materialize_apt_runtime_assets,
    render_build_dockerfile,
)


class FrontendContractTest(unittest.TestCase):
    def setUp(self):
        self.frontend = patch('opensandbox_buildkit_frontend.ensure_frontend', return_value=(Path('/local/layout'), 'sha256:' + '0' * 64))
        self.resolve = self.frontend.start()
        self.addCleanup(self.frontend.stop)

    def test_existing_renderer_preserves_every_corpus_run(self):
        for name, fixture in corpus().items():
            with self.subTest(name=name):
                source = 'FROM scratch AS fixture-base\n' + fixture
                self.assertEqual(source, render_build_dockerfile(
                    source, dockerhub_mirror_prefix='', apt_mirror='',
                    apt_source_overrides={'http://original.invalid/repo': 'http://cache.invalid/repo'},
                ))

    def test_content_addressed_args_and_frontend_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secrets = materialize_apt_runtime_assets(root, {'http://original/repo': 'http://gateway/repo'})
            first = prepare_frontend(secrets, root, build_contexts={})
            self.assertEqual(first, prepare_frontend(secrets, root, build_contexts={}))
            for name in ('OPENSANDBOX_APT_WRAPPER', 'OPENSANDBOX_APT_REWRITER', 'OPENSANDBOX_APT_SOURCE_MAP', 'OPENSANDBOX_FRONTEND_IDENTITY'):
                self.assertIn(first[name], secrets)
            with patch('opensandbox_buildkit_frontend.ensure_frontend', return_value=(Path('/other/layout'), 'sha256:' + '1' * 64)):
                second = prepare_frontend(secrets, root, build_contexts={})
            self.assertNotEqual(first['OPENSANDBOX_FRONTEND_IDENTITY'], second['OPENSANDBOX_FRONTEND_IDENTITY'])
            changed = materialize_apt_runtime_assets(root, {'http://original/repo': 'http://gateway/other'})
            third = prepare_frontend(changed, root, build_contexts={})
            self.assertNotEqual(first['OPENSANDBOX_APT_SOURCE_MAP'], third['OPENSANDBOX_APT_SOURCE_MAP'])

    def test_missing_assets_fail_before_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'runtime secret'):
                prepare_frontend({}, Path(tmp), build_contexts={})
            self.resolve.assert_not_called()

    def test_local_context_and_path_independent_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secrets = materialize_apt_runtime_assets(root, {})
            contexts = {}
            first = prepare_frontend(secrets, root, build_contexts=contexts)
            self.assertEqual(contexts[first['BUILDKIT_SYNTAX']], 'oci-layout:///local/layout@sha256:' + '0' * 64)
            self.resolve.return_value = (Path('/moved/layout'), 'sha256:' + '0' * 64)
            self.assertEqual(first, prepare_frontend(secrets, root, build_contexts={}))

    def test_cache_miss_hit_and_corruption_rebuild(self):
        # Exercise the real cache while replacing only the expensive compiler.
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HARBOR_OPENSANDBOX_FRONTEND_CACHE': tmp}):
            layout = Path(tmp) / source_identity() / 'layout'

            def compile_frontend(*args, **kwargs):
                blobs = layout / 'blobs' / 'sha256'
                blobs.mkdir(parents=True)
                (layout / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
                data = b'{"schemaVersion":2,"layers":[]}'
                digest = hashlib.sha256(data).hexdigest()
                (blobs / digest).write_bytes(data)
                (layout / 'index.json').write_text(json.dumps({'manifests': [{
                    'digest': 'sha256:' + digest, 'size': len(data),
                    'mediaType': 'application/vnd.oci.image.manifest.v1+json'}]}))
                from unittest.mock import Mock
                return Mock(wait=Mock(return_value=0))

            with patch('opensandbox_buildkit_frontend.subprocess.Popen', side_effect=compile_frontend) as build:
                first = ensure_frontend()
                self.assertEqual(first, ensure_frontend())
                self.assertEqual(build.call_count, 1)
                blob = layout / 'blobs' / 'sha256' / first[1].split(':')[1]
                blob.write_bytes(b'corrupt')
                with self.assertRaisesRegex(ValueError, 'corrupt'):
                    layout_digest(layout)
                self.assertEqual(first, ensure_frontend())
                self.assertEqual(build.call_count, 2)

    def test_shared_failure_attempted_once_per_batch_and_new_batch_can_retry(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_batch = root / 'batch-one'
            second_batch = root / 'batch-two'
            first_batch.mkdir()
            second_batch.mkdir()
            env = {'HARBOR_OPENSANDBOX_FRONTEND_CACHE': str(root / 'cache'),
                   'HARBOR_OPENSANDBOX_PREBUILD_RUN_DIR': str(first_batch)}
            with patch.dict(os.environ, env), patch('opensandbox_buildkit_frontend.subprocess.Popen', return_value=Mock(wait=Mock(return_value=1))) as build:
                with self.assertRaises(FrontendBuildError):
                    ensure_frontend()
                with self.assertRaisesRegex(FrontendBuildError, 'already failed'):
                    ensure_frontend()
                self.assertEqual(build.call_count, 1)
                self.assertEqual(len(list(first_batch.glob('frontend-*.failed'))), 1)
                with patch.dict(os.environ, {'HARBOR_OPENSANDBOX_PREBUILD_RUN_DIR': str(second_batch)}), self.assertRaises(FrontendBuildError):
                    ensure_frontend()
                self.assertEqual(build.call_count, 2)

    def test_manager_reports_shared_frontend_failure_separately(self):
        from opensandbox_image_manager import main
        with patch('opensandbox_image_manager.parse_args'), patch('opensandbox_image_manager.prepare_bundle', side_effect=FrontendBuildError('compiler unavailable')):
            self.assertEqual(main(), 78)


if __name__ == '__main__':
    unittest.main()
