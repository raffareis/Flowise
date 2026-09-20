import argparse
import io
import hashlib
import stat
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import release

D1 = 'sha256:' + 'a' * 64
D2 = 'sha256:' + 'b' * 64


def bundle(image, extra=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('Dockerrun.aws.json', json.dumps({
            'AWSEBDockerrunVersion': '1', 'Image': {'Name': image, 'Update': 'true'},
            'Ports': [{'ContainerPort': 80}]}))
        for name in extra:
            archive.writestr(name, 'not a remote image')
    return stream.getvalue()


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.persisted = {}
        self.persist_failure = False
        self.live_version = 'previous'
        self.health = 'Green'
        self.status = 'Ready'
        self.canary_version = 'candidate'
        self.canary_health = 'Green'
        self.account = release.ACCOUNT
        self.bundles = {'previous': bundle(release.REPOSITORY + '@' + D1),
                        'candidate': bundle(release.REPOSITORY + '@' + D2)}
        self.args = argparse.Namespace(operation='promote', version='candidate', digest=D2,
                                       expected_current='previous', current_digest=D1,
                                       canary='flowise-canary', live=False,
                                       evidence=str(Path(self.tmp.name) / 'evidence.json'))
        self.args.bundle_sha256 = hashlib.sha256(self.bundles['candidate']).hexdigest()
        self.args.current_bundle_sha256 = hashlib.sha256(self.bundles['previous']).hexdigest()
        self.mock = patch.object(release, 'aws', side_effect=self.aws)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def aws(self, *args):
        self.calls.append(args)
        service, command, *rest = args
        if service == 'sts': return {'Account': self.account}
        if command == 'describe-environments':
            prod = rest[1] == release.PRODUCTION
            return {'Environments': [{'ApplicationName': 'flowise', 'Status': self.status if prod else 'Ready',
                                      'Health': self.health if prod else self.canary_health,
                                      'VersionLabel': self.live_version if prod else self.canary_version}]}
        if command == 'describe-application-versions':
            return {'ApplicationVersions': [{'SourceBundle': {'S3Bucket': 'private', 'S3Key': rest[-1]}}]}
        if command == 'get-object':
            Path(rest[-1]).write_bytes(self.persisted[rest[3]] if rest[3] in self.persisted else self.bundles[rest[3]])
            return {}
        if command == 'put-object':
            if self.persist_failure: raise ValueError('S3 persistence failed')
            self.persisted[rest[3]] = Path(rest[5]).read_bytes()
            return {}
        if command == 'describe-images':
            return {'imageDetails': [{'imageDigest': rest[-1].split('=', 1)[1]}]}
        if command == 'update-environment':
            self.live_version = rest[-1]
            return {}
        if command == 'wait': return {}
        raise AssertionError(args)

    def mutations(self):
        return [args for args in self.calls if args[1] in ('update-environment', 'restart-app-server', 'create-application-version')]

    def reject(self):
        with self.assertRaises(ValueError): release.execute(self.args)
        self.assertEqual([], self.mutations())

    def test_default_plan_performs_no_mutations(self):
        plan = release.execute(self.args)
        self.assertFalse(plan['live'])
        self.assertEqual(D1, plan['previous']['digest'])
        self.assertEqual([], self.mutations())
        self.assertFalse(Path(self.args.evidence).exists())

    def test_wrong_account_rejected(self):
        self.account = '488884465199'
        self.reject()

    def test_stale_current_rejected(self):
        self.live_version = 'someone-else-deployed'
        self.reject()

    def test_unhealthy_production_rejected(self):
        self.health = 'Red'
        self.reject()

    def test_busy_production_rejected(self):
        self.status = 'Updating'
        self.reject()

    def test_floating_rollback_rejected(self):
        self.bundles['previous'] = bundle(release.REPOSITORY + ':latest')
        self.reject()

    def test_wrong_target_digest_rejected(self):
        self.bundles['candidate'] = bundle(release.REPOSITORY + '@' + D1)
        self.reject()

    def test_wrong_repository_rejected(self):
        self.bundles['candidate'] = bundle('unreviewed/repo@' + D2)
        self.reject()

    def test_competing_dockerfile_rejected(self):
        self.bundles['candidate'] = bundle(release.REPOSITORY + '@' + D2, ('Dockerfile',))
        self.reject()

    def test_missing_canary_rejected(self):
        self.args.canary = None
        self.reject()

    def test_production_cannot_be_its_own_canary(self):
        self.args.canary = release.PRODUCTION
        self.reject()

    def test_wrong_canary_version_rejected(self):
        self.canary_version = 'other'
        self.reject()

    def test_bad_canary_health_rejected(self):
        self.canary_health = 'Red'
        self.reject()

    def test_live_requires_durable_evidence(self):
        self.args.live = True
        self.args.evidence = None
        self.reject()

    def test_bundle_drift_before_mutation_rejected(self):
        self.args.live = True
        original = release.version_evidence
        reads = 0
        def changing(*args):
            nonlocal reads
            reads += 1
            result = original(*args)
            if reads == 3: result['bundle_sha256'] = 'changed'
            return result
        with patch.object(release, 'version_evidence', side_effect=changing): self.reject()

    def test_live_promote_updates_exact_version_and_records_rollback(self):
        self.args.live = True
        with patch.object(release.urllib.request, 'urlopen') as request:
            request.return_value.__enter__.return_value.status = 200
            plan = release.execute(self.args)
        self.assertEqual('candidate', plan['verified']['version'])
        self.assertEqual([('elasticbeanstalk', 'update-environment', '--environment-name', 'flowise-prod',
                           '--version-label', 'candidate')], self.mutations())
        saved = json.loads(Path(self.args.evidence).read_text())
        self.assertEqual(D1, saved['previous']['digest'])
        persisted = json.loads(next(iter(self.persisted.values())))
        self.assertEqual(D1, persisted['previous']['digest'])
        names = [call[1] for call in self.calls]
        self.assertLess(names.index('put-object'), names.index('update-environment'))
        self.assertEqual(0o600, Path(self.args.evidence).stat().st_mode & 0o777)
        final = json.loads(next(data for key, data in self.persisted.items() if key.endswith('.result.json')))
        self.assertEqual(200, final['verified']['http'])

    def test_rollback_allows_unhealthy_current_without_canary(self):
        self.args.operation = 'rollback'
        self.args.canary = None
        self.health = 'Red'
        plan = release.execute(self.args)
        self.assertEqual('rollback', plan['operation'])
        self.assertEqual([], self.mutations())

    def test_reviewed_bundle_hash_mismatch_rejected(self):
        self.args.bundle_sha256 = '0' * 64
        self.reject()

    def test_extra_configuration_even_with_reviewed_hash_rejected(self):
        self.bundles['candidate'] = bundle(release.REPOSITORY + '@' + D2, ('.ebextensions/99-env.config',))
        self.args.bundle_sha256 = hashlib.sha256(self.bundles['candidate']).hexdigest()
        self.reject()

    def test_directory_permissions_are_part_of_configuration(self):
        for version, mode in [('previous', 0o700), ('candidate', 0o755)]:
            stream = io.BytesIO(self.bundles[version])
            with zipfile.ZipFile(stream, 'a') as archive:
                directory = zipfile.ZipInfo('.ebextensions/')
                directory.external_attr = (stat.S_IFDIR | mode) << 16
                archive.writestr(directory, b'')
            self.bundles[version] = stream.getvalue()
        self.args.bundle_sha256 = hashlib.sha256(self.bundles['candidate']).hexdigest()
        self.args.current_bundle_sha256 = hashlib.sha256(self.bundles['previous']).hexdigest()
        self.reject()

    def test_unsupported_symlink_entry_rejected(self):
        stream = io.BytesIO(self.bundles['candidate'])
        with zipfile.ZipFile(stream, 'a') as archive:
            link = zipfile.ZipInfo('config')
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, b'/etc/passwd')
        self.bundles['candidate'] = stream.getvalue()
        self.args.bundle_sha256 = hashlib.sha256(self.bundles['candidate']).hexdigest()
        self.reject()

    def test_persistence_failure_prevents_production_write(self):
        self.args.live = True
        self.persist_failure = True
        self.reject()

    def test_existing_evidence_never_overwritten(self):
        self.args.live = True
        Path(self.args.evidence).write_text('original')
        with self.assertRaises(FileExistsError): release.execute(self.args)
        self.assertEqual([], self.mutations())
        self.assertEqual('original', Path(self.args.evidence).read_text())


if __name__ == '__main__': unittest.main()
