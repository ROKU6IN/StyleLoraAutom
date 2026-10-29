import json
from datetime import datetime, timezone
from pathlib import Path
import threading

from .secrets import safe_path


class Audit:
    def __init__(self, directory=None):
        self.path = Path(directory) / 'llm-audit.jsonl' if directory is not None else None
        self.warnings = 0
        self.lock = threading.Lock()

    def write(self, *, request_id, role, endpoint_id, protocol, model, token_index, status, retry, tier, duration,
              attempt_id=None, phase='outcome'):
        with self.lock:
            if self.path is None:
                return
            try:
                safe_path(self.path)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.exists() and self.path.stat().st_size > 4 * 1024 * 1024:
                    previous = self.path.with_suffix('.previous.jsonl')
                    safe_path(previous)
                    self.path.replace(previous)
                with self.path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'request_id': request_id, 'attempt_id': attempt_id, 'phase': phase,
                        'role': role, 'endpoint_id': endpoint_id,
                        'protocol': protocol, 'model': model, 'token_index': token_index, 'status': status,
                        'retry': retry, 'tier': tier, 'duration': round(duration, 3),
                        'utc': datetime.now(timezone.utc).isoformat()}, ensure_ascii=True) + '\n')
            except Exception:
                self.warnings += 1
