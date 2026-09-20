#!/usr/bin/env python3
"""Promote an existing immutable EB version. Read-only unless --live is explicit."""
import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import urllib.request
import zipfile
import uuid
from pathlib import Path

ACCOUNT = '387653681120'
REGION = 'us-east-1'
APPLICATION = 'flowise'
PRODUCTION = 'flowise-prod'
REPOSITORY = f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/flowise'
EVIDENCE_BUCKET = f'elasticbeanstalk-{REGION}-{ACCOUNT}'
HASH = re.compile(r'[0-9a-f]{64}\Z')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')


def aws(*args):
    result = subprocess.run(['aws', '--region', REGION, *args, '--output', 'json'],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def environment(name):
    rows = aws('elasticbeanstalk', 'describe-environments', '--environment-names', name)['Environments']
    if len(rows) != 1 or rows[0]['ApplicationName'] != APPLICATION:
        raise ValueError('Expected one environment in application flowise')
    return rows[0]


def ready(env, version, healthy=True):
    if env['Status'] != 'Ready' or env['VersionLabel'] != version:
        raise ValueError('Environment changed or is not Ready; refresh the release plan')
    if healthy and env['Health'] != 'Green':
        raise ValueError('Environment is not Green')


def immutable_image(bundle, digest):
    if not DIGEST.fullmatch(digest):
        raise ValueError('Expected a complete sha256 image digest')
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
        if names.count('Dockerrun.aws.json') != 1 or any(
                name in names for name in ('Dockerfile', 'docker-compose.yml', 'docker-compose.yaml')):
            raise ValueError('Expected one Dockerrun v1 remote-image bundle without competing Docker definitions')
        if len(names) != len(set(names)):
            raise ValueError('Duplicate bundle entries')
        document = json.loads(archive.read('Dockerrun.aws.json'))
    if str(document.get('AWSEBDockerrunVersion')) != '1':
        raise ValueError('Only the existing Dockerrun v1 contract is supported')
    if document.get('Image', {}).get('Name') != f'{REPOSITORY}@{digest}':
        raise ValueError('Bundle image is floating or does not match the reviewed digest')
    if document['Image'].get('Update') not in ('true', True):
        raise ValueError('Dockerrun must pull the exact digest')
    normalized = {}
    with zipfile.ZipFile(bundle) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            data = archive.read(info)
            if info.filename == 'Dockerrun.aws.json':
                document['Image']['Name'] = '<reviewed-image-digest>'
                data = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
            normalized[info.filename] = {'sha256': hashlib.sha256(data).hexdigest(), 'mode': info.external_attr >> 16}
    return (hashlib.sha256(Path(bundle).read_bytes()).hexdigest(),
            hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest())


def version_evidence(label, digest, expected_hash, directory):
    if not HASH.fullmatch(expected_hash):
        raise ValueError('Expected a reviewed bundle SHA256')
    rows = aws('elasticbeanstalk', 'describe-application-versions', '--application-name', APPLICATION,
               '--version-labels', label)['ApplicationVersions']
    if len(rows) != 1:
        raise ValueError('Application version does not exist')
    source = rows[0]['SourceBundle']
    target = directory / (hashlib.sha256(label.encode()).hexdigest() + '.zip')
    aws('s3api', 'get-object', '--bucket', source['S3Bucket'], '--key', source['S3Key'], str(target))
    bundle_hash, configuration_hash = immutable_image(target, digest)
    if bundle_hash != expected_hash:
        raise ValueError('Bundle does not match the reviewed SHA256')
    images = aws('ecr', 'describe-images', '--repository-name', 'flowise',
                 '--image-ids', 'imageDigest=' + digest)['imageDetails']
    if len(images) != 1 or images[0]['imageDigest'] != digest:
        raise ValueError('Digest is absent from the expected ECR repository')
    return {'version': label, 'digest': digest, 'bundle_sha256': bundle_hash,
            'configuration_sha256': configuration_hash, 'source': source}


def execute(args):
    if aws('sts', 'get-caller-identity')['Account'] != ACCOUNT:
        raise ValueError('Wrong AWS account')
    if args.version == args.expected_current:
        raise ValueError('Target and current versions must differ')
    current = environment(PRODUCTION)
    ready(current, args.expected_current, healthy=args.operation != 'rollback')
    with tempfile.TemporaryDirectory(prefix='flowise-release-') as temporary:
        directory = Path(temporary)
        target = version_evidence(args.version, args.digest, args.bundle_sha256, directory)
        previous = version_evidence(args.expected_current, args.current_digest, args.current_bundle_sha256, directory)
        if target['configuration_sha256'] != previous['configuration_sha256']:
            raise ValueError('Configuration changed: this release path permits only the image digest to change')
        canary = None
        if args.operation != 'rollback':
            if not args.canary or args.canary == PRODUCTION:
                raise ValueError('A distinct canary environment is required for promotion')
            canary = environment(args.canary)
            ready(canary, args.version)
        plan = {'operation': args.operation, 'target': target, 'previous': previous,
                'canary': args.canary if canary else None, 'live': args.live}
        if not args.live:
            return plan
        if not args.evidence:
            raise ValueError('--evidence is required for live operations')
        plan['evidence_s3'] = {'bucket': EVIDENCE_BUCKET, 'key': 'flowise/release-evidence/' + uuid.uuid4().hex + '.json'}
        evidence = Path(args.evidence)
        evidence.parent.mkdir(parents=True, exist_ok=True)
        # Refuse replacement: each operation gets its own durable rollback record.
        with evidence.open('x', encoding='utf-8') as stream:
            json.dump(plan, stream, indent=2)
            stream.write('\n')
        evidence.chmod(0o600)
        # Persist and verify the rollback record before any production mutation.
        destination = plan['evidence_s3']
        aws('s3api', 'put-object', '--bucket', destination['bucket'], '--key', destination['key'],
            '--body', str(evidence), '--server-side-encryption', 'AES256', '--if-none-match', '*')
        receipt = directory / 'persisted-evidence.json'
        aws('s3api', 'get-object', '--bucket', destination['bucket'], '--key', destination['key'], str(receipt))
        if receipt.read_bytes() != evidence.read_bytes():
            raise ValueError('Rollback evidence persistence was not confirmed')
        # Recheck both S3 inputs and the environment immediately before mutation.
        if target != version_evidence(args.version, args.digest, args.bundle_sha256, directory):
            raise ValueError('Target bundle drifted')
        if previous != version_evidence(args.expected_current, args.current_digest, args.current_bundle_sha256, directory):
            raise ValueError('Rollback bundle drifted')
        ready(environment(PRODUCTION), args.expected_current, healthy=args.operation != 'rollback')
        if canary:
            ready(environment(args.canary), args.version)
        aws('elasticbeanstalk', 'update-environment', '--environment-name', PRODUCTION,
            '--version-label', args.version)
        aws('elasticbeanstalk', 'wait', 'environment-updated', '--environment-names', PRODUCTION)
        final = environment(PRODUCTION)
        ready(final, args.version)
        with urllib.request.urlopen('https://flowise.xmacna.ai/api/v1/ping', timeout=30) as response:
            if response.status != 200:
                raise ValueError('Public health did not return HTTP 200')
        plan['verified'] = {'version': final['VersionLabel'], 'health': final['Health'], 'http': 200}
        evidence.write_text(json.dumps(plan, indent=2) + '\n')
        return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operation', choices=('promote', 'rollback'), default='promote')
    parser.add_argument('--version', required=True)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--expected-current', required=True)
    parser.add_argument('--current-digest', required=True)
    parser.add_argument('--bundle-sha256', required=True)
    parser.add_argument('--current-bundle-sha256', required=True)
    parser.add_argument('--canary')
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--evidence')
    args = parser.parse_args()
    try:
        print(json.dumps(execute(args), indent=2))
    except (ValueError, KeyError, subprocess.CalledProcessError, OSError, zipfile.BadZipFile) as error:
        # AWS stderr can contain operational payloads; do not echo it.
        parser.exit(1, f'Release stopped: {type(error).__name__}: '
                    f'{str(error) if not isinstance(error, subprocess.CalledProcessError) else "AWS command failed"}\n')


if __name__ == '__main__':
    main()
