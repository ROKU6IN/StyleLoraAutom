"""Explicit, bounded model installs with atomic publication and no project writes."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

from ..exchange.common import is_link
from ..runtime.errors import ServiceError
from .manifests import ModelManifest, reviewed_provenance
from .model_runtime import validate_tensors


def check_cancel(cancel):
    if cancel.is_set():
        raise ServiceError(409, 'operation:canceled')


def allowed_url(url):
    try:
        value = urlsplit(url)
        host = value.hostname or ''
        return (value.scheme == 'https' and value.port in (None, 443) and not value.username
                and not value.password and not value.fragment and (host == 'huggingface.co'
                or host.endswith('.huggingface.co') or host.endswith('.hf.co')))
    except ValueError:
        return False


class HuggingFaceDownload:
    def __init__(self, *, transport=None, clock=time.monotonic):
        self.transport, self.clock = transport, clock

    def __call__(self, manifest, file, cancel):
        url = f'https://huggingface.co/{manifest.repo_id}/resolve/{manifest.revision}/{file.name}'
        deadline = self.clock() + 1800
        try:
            with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(5, connect=10), headers={'Accept-Encoding': 'identity'}) as client:
                for _ in range(6):
                    check_cancel(cancel)
                    if not allowed_url(url) or self.clock() >= deadline:
                        raise ServiceError(503, 'tagger:download_failed')
                    with client.stream('GET', url) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get('location')
                            if not location:
                                raise ServiceError(503, 'tagger:download_failed')
                            url = urljoin(url, location)
                            continue
                        if (response.status_code != 200 or response.headers.get('content-encoding', 'identity') != 'identity'
                                or ('content-length' in response.headers and response.headers['content-length'] != str(file.size))):
                            raise ServiceError(503, 'tagger:download_failed')
                        # Do not buffer a full application chunk: slow trickles
                        # must still reach cancellation/total-deadline checks.
                        for chunk in response.iter_bytes():
                            check_cancel(cancel)
                            if self.clock() >= deadline:
                                raise ServiceError(503, 'tagger:download_failed')
                            yield chunk
                        check_cancel(cancel)
                        if self.clock() >= deadline:
                            raise ServiceError(503, 'tagger:download_failed')
                        return
                raise ServiceError(503, 'tagger:download_failed')
        except httpx.HTTPError:
            # In particular, do not expose signed CDN URLs or raw response bodies.
            raise ServiceError(503, 'tagger:download_failed') from None


def validate_csv(manifest, path):
    try:
        raw = path.read_bytes()
        if len(raw) != manifest.csv_file.size:
            raise ValueError()
        proof = reviewed_provenance(manifest)
        if proof is not None:
            from .provenance import verify_git_blob
            verified = verify_git_blob(raw, oid=proof['csv_git_blob'], size=manifest.csv_file.size)
            if verified != manifest.csv_file.sha256:
                raise ServiceError(503, 'tagger:provenance_mismatch')
        rows = csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
        columns = next(rows)
        if columns != list(manifest.csv_schema.columns):
            raise ValueError()
        name_idx, category_idx = columns.index('name'), columns.index('category')
        seen, count = set(), 0
        for row in rows:
            if len(row) != len(columns):
                raise ValueError()
            name, category = row[name_idx], row[category_idx]
            if (not name.strip() or len(name) > 1024 or any(ord(c) < 32 for c in name)
                    or category not in ('0', '4', '9') or name in seen):
                raise ValueError()
            seen.add(name)
            count += 1
            if count > manifest.tensor_schema.output_count:
                raise ValueError()
        if count != manifest.tensor_schema.output_count:
            raise ValueError()
    except (ValueError, UnicodeError, csv.Error, StopIteration):
        raise ServiceError(503, 'tagger:csv_invalid') from None


class ModelStore:
    def __init__(self, root, *, download=None, validator=validate_tensors):
        self.root = Path(root).absolute() if root else None
        self.download = download or HuggingFaceDownload()
        self.validator = validator

    def safe(self, path):
        if not self.root or not path.is_relative_to(self.root):
            raise ServiceError(503, 'tagger:storage_unavailable')
        # Includes ancestors: junctions/reparse points must not redirect model IO.
        if any(is_link(p) for p in (path, *path.parents)):
            raise ServiceError(503, 'tagger:path_unsafe')
        return path

    def directory(self, manifest):
        if not self.root:
            raise ServiceError(503, 'tagger:storage_unavailable')
        return self.safe(self.root/manifest.model_id/manifest.revision)

    def footprint(self, manifest):
        directory = self.directory(manifest)
        result = []
        for name in ('ready.json', *(f.name for f in manifest.files)):
            path = self.safe(directory/name)
            stat = path.stat()
            if not path.is_file():
                raise ServiceError(503, 'tagger:model_invalid')
            result.append((stat.st_size, stat.st_mtime_ns, stat.st_ino))
        return tuple(result)

    def verify(self, manifest, cancel, phase=lambda *_: None):
        directory = self.directory(manifest)
        check_cancel(cancel)
        marker = self.safe(directory/'ready.json')
        if not marker.is_file() or marker.stat().st_size > 32768:
            raise ServiceError(503, 'tagger:model_invalid')
        from ..runtime.contracts import strict_json
        try:
            ready = strict_json(marker.read_bytes())
            if ready != {'schema_version': 1, 'manifest': manifest.model_dump(mode='json')}:
                raise ValueError()
        except (ValueError, ServiceError):
            raise ServiceError(503, 'tagger:model_invalid') from None
        before = self.footprint(manifest)
        self._verify_files(manifest, directory, cancel, phase)
        after = self.footprint(manifest)
        if before != after:
            raise ServiceError(503, 'tagger:model_changed')
        return after

    def _verify_files(self, manifest, directory, cancel, phase):
        phase('verifying_files')
        for file in manifest.files:
            check_cancel(cancel)
            path = self.safe(directory/file.name)
            if not path.is_file() or path.stat().st_size != file.size:
                raise ServiceError(503, 'tagger:checksum_mismatch')
            actual = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(1024*1024):
                    check_cancel(cancel)
                    actual.update(chunk)
            if actual.hexdigest() != file.sha256:
                raise ServiceError(503, 'tagger:checksum_mismatch')
        phase('verifying_csv')
        validate_csv(manifest, directory/manifest.csv_file.name)
        check_cancel(cancel)
        phase('verifying_tensors')
        self.validator(manifest, directory/manifest.model_file.name)
        check_cancel(cancel)

    def install(self, manifest: ModelManifest, cancel, *, phase=lambda *_: None,
                progress=lambda *_: None, publish_lock=None):
        directory = self.directory(manifest)
        check_cancel(cancel)
        if directory.exists():
            stamp = self.verify(manifest, cancel, phase)
            return {'reused': True, 'stamp': stamp}
        parent = self.safe(directory.parent)
        parent.mkdir(parents=True, exist_ok=True)
        staging = self.safe(parent/f'{manifest.revision}.{uuid4().hex}.partial')
        staging.mkdir()
        completed, total = 0, sum(file.size for file in manifest.files)
        try:
            phase('downloading')
            progress(0, total)
            for file in manifest.files:
                path = self.safe(staging/file.name)
                downloaded = 0
                actual = hashlib.sha256()
                with path.open('xb') as output:
                    chunks = iter(self.download(manifest, file, cancel))
                    try:
                        for chunk in chunks:
                            check_cancel(cancel)
                            if not isinstance(chunk, bytes) or len(chunk) > 1024*1024:
                                raise ServiceError(503, 'tagger:download_failed')
                            downloaded += len(chunk)
                            if downloaded > file.size:
                                raise ServiceError(503, 'tagger:checksum_mismatch')
                            output.write(chunk)
                            actual.update(chunk)
                            completed += len(chunk)
                            progress(completed, total)
                    finally:
                        if hasattr(chunks, 'close'):
                            chunks.close()
                    output.flush()
                    os.fsync(output.fileno())
                check_cancel(cancel)
                if downloaded != file.size or actual.hexdigest() != file.sha256:
                    raise ServiceError(503, 'tagger:checksum_mismatch')
            self._verify_files(manifest, staging, cancel, phase)
            phase('publishing')
            marker = self.safe(staging/'ready.json')
            with marker.open('x', encoding='utf-8') as stream:
                json.dump({'schema_version': 1, 'manifest': manifest.model_dump(mode='json')}, stream,
                          ensure_ascii=False, sort_keys=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            # Serialize the commit point against task cancellation. No published
            # version is overwritten, repaired in place or deleted by cancellation.
            with publish_lock or threading.Lock():
                check_cancel(cancel)
                self.safe(directory)
                self.safe(staging)
                if directory.exists():
                    raise ServiceError(409, 'tagger:in_use')
                staging.rename(directory)
            return {'reused': False, 'stamp': self.footprint(manifest)}
        finally:
            # Only this invocation's flat, validated staging bundle is cleaned.
            if staging.exists():
                self.safe(staging)
                for name in (*(f.name for f in manifest.files), 'ready.json'):
                    path = self.safe(staging/name)
                    path.unlink(missing_ok=True)
                staging.rmdir()
