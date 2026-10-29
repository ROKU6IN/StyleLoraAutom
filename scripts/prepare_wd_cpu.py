"""Explicitly prepare reviewed model/reference inputs for accept_wd_cpu.py.

--download fetches public fixed revisions, without credentials or environment
proxies. --local-bundle copies already downloaded inputs and performs no network.
Existing reference files must match the pinned digest; they are never replaced.
"""
import argparse
import hashlib
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urljoin
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stylelora.exchange.common import is_link
from stylelora.tagging.manifests import REVIEWED_MANIFESTS
from stylelora.tagging.models import ModelStore, allowed_url, check_cancel
from tests.wd_cpu_reference import REFERENCE_REVISION, REFERENCE_SHA256, IMAGE_SHA256, checked_bytes


def fetch_reference(name, expected, limit):
    import httpx
    try:
        return _fetch_reference(name, expected, limit)
    except httpx.HTTPError:
        raise RuntimeError('Reference network request failed') from None


def _fetch_reference(name, expected, limit):
    import httpx
    deadline = time.monotonic()+120
    url = f'https://huggingface.co/spaces/SmilingWolf/wd-tagger/resolve/{REFERENCE_REVISION}/{name}'
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=httpx.Timeout(10, connect=10),
                      headers={'Accept-Encoding': 'identity'}) as client:
        for _ in range(6):
            if not allowed_url(url):
                raise RuntimeError('Untrusted reference redirect')
            with client.stream('GET', url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers['location'])
                    continue
                if response.status_code != 200 or response.headers.get('content-encoding', 'identity') != 'identity':
                    raise RuntimeError('Reference download failed')
                data = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise RuntimeError('Reference time budget exceeded')
                    data.extend(chunk)
                    if len(data) > limit:
                        raise RuntimeError('Oversized reference file')
                if hashlib.sha256(data).hexdigest() != expected:
                    raise RuntimeError('Reference digest mismatch')
                return bytes(data)
    raise RuntimeError('Reference redirect limit')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--reference-dir', type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--download', action='store_true')
    source.add_argument('--local-bundle', type=Path, help='Directory containing model.onnx, selected_tags.csv, reference-app.py and reference-power.jpg')
    args = parser.parse_args()
    root = args.reference_dir.absolute()
    if any(is_link(p) for p in (root, *root.parents)):
        raise RuntimeError('Reference directory contains a link')
    root.mkdir(parents=True, exist_ok=True)
    for name, expected, limit in [('app.py', REFERENCE_SHA256, 128*1024), ('power.jpg', IMAGE_SHA256, 10*1024**2)]:
        target = root/('reference-'+name)
        if is_link(target):
            raise RuntimeError('Reference file is a link')
        if target.exists():
            checked_bytes(target, expected, limit)
            continue
        if args.local_bundle:
            data = checked_bytes(args.local_bundle/target.name, expected, limit)
        else:
            data = fetch_reference(name, expected, limit)
        staging = root/(target.name+'.'+uuid4().hex+'.partial')
        try:
            with staging.open('xb') as stream:
                stream.write(data)
            if target.exists():
                raise RuntimeError('Reference target appeared during preparation')
            staging.rename(target)
        finally:
            staging.unlink(missing_ok=True)
    def local_download(manifest, file, cancel):
        with (args.local_bundle/file.name).open('rb') as stream:
            while chunk := stream.read(1024**2):
                check_cancel(cancel)
                yield chunk
    store = ModelStore(args.model_dir, download=local_download if args.local_bundle else None)
    result = store.install(REVIEWED_MANIFESTS[0], threading.Event(), phase=lambda phase: print(phase, flush=True))
    print('Reviewed model ready; reused:', result['reused'], flush=True)


if __name__ == '__main__':
    main()
