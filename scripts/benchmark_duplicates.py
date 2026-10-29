"""Local synthetic 10,000-fingerprint capacity check; no model or external service."""
import json
from pathlib import Path
import random
import tempfile
import threading
import time
from types import SimpleNamespace

from stylelora.imaging.phash import HashRecord, candidate_rows
from stylelora.runtime.errors import ServiceError
from stylelora.services.duplicates import DuplicateService, Scan

root = Path(__file__).resolve().parents[1]/'.test-tmp'
root.mkdir(exist_ok=True)
rng = random.Random(20261025)
records = [HashRecord(i, f'{rng.getrandbits(64):016x}') for i in range(1, 10001)]
start = time.perf_counter()
comparisons, matches = 0, 0
for count, buckets in candidate_rows(records, threshold=0):
    comparisons += count
    matches += sum(map(len, buckets))
sparse = {'images': len(records), 'threshold': 0, 'comparisons': comparisons, 'matches': matches,
          'elapsed_seconds': round(time.perf_counter()-start, 3)}
print(json.dumps({'sparse': sparse}), flush=True)
with tempfile.TemporaryDirectory(prefix='f08-capacity-', dir=root) as directory:
    base = Path(directory)
    (base/'images').mkdir()
    # Deliberately share one fixture path: this measures the scan/spool, not import/decoding throughput.
    (base/'images/sample.png').write_bytes(b'fixture')
    rows = tuple({'image_id': i, 'phash': '0'*16, 'filename': 'sample.png', 'file_missing': False, 'file_size': 7} for i in range(1, 10001))
    service = DuplicateService(SimpleNamespace(supervisor=SimpleNamespace(progress=lambda *args, **kwargs: None)), temp_root=base)
    scan = Scan('benchmark', 'fixture', 64, rows, input_count=len(rows))
    task = SimpleNamespace(cancel=threading.Event())
    start = time.perf_counter()
    try:
        service._run(SimpleNamespace(handle=SimpleNamespace(root=base)), scan, task)
    except ServiceError:
        pass
    assert scan.status == 'failed' and scan.error == 'duplicates:result_limit'
    assert scan.directory is None
    dense = {'images': len(rows), 'threshold': 64, 'status': scan.status, 'reason': scan.error,
             'matched_before_limit': scan.matched, 'elapsed_seconds': round(time.perf_counter()-start, 3),
             'max_result_bytes': service.max_bytes, 'temporary_results_removed': scan.directory is None}
    print(json.dumps({'dense': dense}), flush=True)
(root/'f08-capacity.json').write_text(json.dumps({'sparse': sparse, 'dense': dense}, indent=2), encoding='utf-8')
