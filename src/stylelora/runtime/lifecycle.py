import asyncio
import hashlib
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..project_db.connection import submit_write
from ..project_db.project import close_project, create_project, open_project
from ..project_db.validation import primitive
from .contracts import CursorCodec, Versions
from .errors import ServiceError
from .events import EventBus
from .registry import Idempotency, PathGrants, Previews
from .supervisor import Supervisor


@dataclass
class ProjectContext:
    project_id: str
    generation: int
    handle: object
    closing: bool = False
    users: int = 0
    known_id: str | None = None
    recent_warning: str | None = None


class Runtime:
    def __init__(self, *, authority="127.0.0.1:8000", dev_origin=None, native_picker=False, product_dir=None, model_dir=None, llm_dir=None, secret_dir=None):
        self.authority = authority
        self.origin = "http://" + authority
        if dev_origin is not None:
            from urllib.parse import urlsplit
            parsed = urlsplit(dev_origin)
            if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ServiceError(422, "security:invalid_dev_origin")
        self.dev_origin = dev_origin
        self.instance_id = str(uuid4())
        self.token = secrets.token_urlsafe(32)
        self.secret = secrets.token_bytes(32)
        self.events = EventBus(self.instance_id)
        self.versions, self.cursors = Versions(self.secret), CursorCodec(self.secret)
        self.grants, self.previews, self.idempotency = PathGrants(), Previews(), Idempotency()
        self.condition = threading.Condition(threading.RLock())
        self._switch_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._shutdown_request_lock = threading.Lock()
        self.context, self.generation = None, 0
        self.draining, self.closed, self.control_busy = False, False, False
        self._bootstrap, self._attempts = {}, []
        self._session_lock = threading.Lock()
        self.request_pool = ThreadPoolExecutor(4, thread_name_prefix="stylelora-http")
        self._admissions = threading.BoundedSemaphore(36)
        self.supervisor = Supervisor(self)
        from .picker import NativePicker
        self.picker = NativePicker(self, enabled=native_picker)
        self.shutdown_requested = threading.Event()
        self.shutdown_task = None
        self.shutdown_grace_seconds = 30
        self.source_roots = {}
        from ..services.dataset import DatasetService
        self.dataset = DatasetService(self)
        from ..services.export_recovery import ExportRecovery
        self.export_recovery = ExportRecovery(self)
        from ..services.product_config import ProductConfig
        self.product_config = ProductConfig(self, llm_dir if llm_dir is not None else Path(product_dir)/'config' if product_dir else None)
        from ..services.storage import StorageService
        self.storage = StorageService(self)
        from ..services.recent_projects import RecentProjects
        self.recent = RecentProjects(self, llm_dir if llm_dir is not None else Path(product_dir)/'projects' if product_dir else None)
        from ..services.known_storage import KnownStorage
        self.known_storage = KnownStorage(self)
        from ..tagging.library import CatalogLibrary
        from ..services.cleaning import CleaningService
        self.catalog = CatalogLibrary(self, product_dir)
        self.cleaning = CleaningService(self)
        from ..services.backends import BackendService
        self.backends = BackendService(self)
        from ..services.duplicates import DuplicateService
        self.duplicates = DuplicateService(self, temp_root=Path(product_dir).parent if product_dir else None)
        from ..services.tagger import TaggerService
        self.tagger = TaggerService(self, model_dir)
        from ..services.jobs import JobService
        self.jobs = JobService(self)
        from ..llm.service import LlmService
        self.llm = LlmService(self, llm_dir, secret_dir)
        from ..services.vision_jobs import VisionJobService
        self.vision = VisionJobService(self, llm_dir)
        from ..services.job_batches import BatchService
        self.batches = BatchService(self)
        from ..prompt_production.tasks import PromptProduction
        self.prompts = PromptProduction(self, llm_dir if llm_dir is not None else Path(product_dir)/'prompt-production' if product_dir else None)
        from ..generation.settings import SettingsLeases
        recovery_dir = Path(secret_dir)/'recovery'/'launcher' if secret_dir else Path(product_dir)/'recovery'/'launcher' if product_dir else None
        self.settings_leases = SettingsLeases(recovery_dir, notify=lambda: self.emit('backend.changed',
            {'backend_id': 'nai_launcher', 'settings_lease_changed': True}))
        from ..generation.sources import SourceService
        self.generation_sources = SourceService(self)
        from ..backends.nai_direct.service import DirectService
        self.nai_direct = DirectService(self, secret_dir or (Path(product_dir)/'secrets' if product_dir else None))
        from ..generation.launcher_check import LauncherCheckService
        self.launcher_checks = LauncherCheckService(self)
        from ..generation.costs import CostService
        self.generation_costs = CostService(self)
        from ..generation.recovery import RecoveryService
        self.generation_recovery = RecoveryService(self)
        from ..generation.presets import PresetStore
        self.presets = PresetStore(self, llm_dir if llm_dir is not None else Path(product_dir)/'generation' if product_dir else None)

    async def call(self, function, *args, **kwargs):
        if not self._admissions.acquire(blocking=False):
            raise ServiceError(429, "request:queue_full")
        def work():
            try:
                return function(*args, **kwargs)
            finally:
                self._admissions.release()
        try:
            future = asyncio.get_running_loop().run_in_executor(self.request_pool, work)
        except BaseException:
            self._admissions.release()
            raise
        # Retrieve failures even when the caller disconnects and drops the
        # shield. Awaiting clients still receive the same exception normally.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(future)  # A disconnected request never abandons admitted work.

    def emit(self, *args, **kwargs):
        try:
            self.events.publish(*args, **kwargs)
        except Exception:
            pass  # GET is authoritative; a notification failure must not undo a commit.

    def session(self):
        return {"instance_id": self.instance_id, "epoch": self.events.epoch, "api_schema_version": 1,
                "project_id": self.context.project_id if self.context and not self.context.closing else None,
                "draining": self.draining, "version": self.instance_id,
                "capabilities": {"local_import": True, "manual_edit": True, "review": True, "image_txt_export": True,
                                 "sse": True, "media": True, "native_picker": self.picker.available, "nai": self.nai_direct.available(), "wd": self.tagger.can_execute(), "llm_vision": self.vision.capabilities()['available'],
                                 "review_query": True, "image_stats": True, "image_file_verification": True, "web_workbench": True, "backend_probe": True, "duplicates": True, "model_management": True, "wd_jobs": True,
                                 "local_catalog": True, "replacement_rules": True, "bulk_manual_preview": True, "project_storage": True, "recent_projects": True, "known_storage": True, "storage_thresholds": True,
                                 "llm_config": True, "prompt_production": True, "generation_drafts": True, "generation_presets": True, "cli_path_grants": True, "request_deduplication": "session", "api_scope": "f11-core-v1"}}

    def issue_bootstrap(self):
        code = secrets.token_urlsafe(32)
        origin = self.dev_origin or self.origin
        with self._session_lock:
            self._bootstrap = {key: value for key, value in self._bootstrap.items() if value[0] > time.monotonic()}
            if len(self._bootstrap) >= 64:
                raise ServiceError(429, "auth:bootstrap_limit")
            self._bootstrap[hashlib.sha256(code.encode()).hexdigest()] = (time.monotonic()+60, origin)
        return origin + "/#bootstrap=" + code

    def redeem(self, code, origin):
        with self._session_lock:
            now = time.monotonic()
            self._attempts = [value for value in self._attempts if now-value < 60]
            if len(self._attempts) >= 30:
                raise ServiceError(429, "request:rate_limited")
            self._attempts.append(now)
            record = self._bootstrap.pop(hashlib.sha256(code.encode()).hexdigest(), None)
            if record is None or record[0] <= now or record[1] != origin:
                raise ServiceError(401, "auth:bootstrap_invalid")
        return {"token": self.token, "instance_id": self.instance_id, "epoch": self.events.epoch, "api_base": "/api", "expires": "process"}

    def is_current(self, project_id, *, allow_closing=False):
        with self.condition:
            return bool(self.context and self.context.project_id == project_id and (allow_closing or not self.context.closing))

    def require(self, project_id):
        with self.condition:
            if not self.is_current(project_id):
                raise ServiceError(410, "project:handle_expired")
            return self.context

    def retain(self, context, *, readonly=False):
        with self.condition:
            if self.draining and not readonly:
                raise ServiceError(503, "process:draining")
            if context is not self.context or context.closing:
                raise ServiceError(410, "project:handle_expired")
            context.users += 1

    def release(self, context):
        with self.condition:
            context.users -= 1
            self.condition.notify_all()

    @contextmanager
    def lease(self, project_id, *, readonly=False):
        with self.condition:
            context = self.require(project_id)
            self.retain(context, readonly=readonly)
        try:
            yield context
        finally:
            self.release(context)

    def project_view(self, context):
        return {"project_id": context.project_id, "metadata": primitive(context.handle.metadata),
                "known_id": context.known_id, "recent_warning": context.recent_warning,
                "schema_version": context.handle.schema_version, "recovery": primitive(context.handle.recovery_report),
                "version": self.versions.get(context.project_id + ":project", context.handle.metadata)}

    def current(self):
        with self.condition:
            return self.project_view(self.context) if self.context and not self.context.closing else None

    def grant(self, path, purpose, project_id=None):
        if self.draining:
            raise ServiceError(503, "process:draining")
        if purpose == "import_root":
            self.require(project_id)
        return self.grants.add(path, purpose, project_id)

    def remember_project(self, context, *, touch=True):
        try:
            context.known_id = self.recent.remember(context.handle, touch=touch)
            context.recent_warning = None
        except (ServiceError, OSError):
            context.recent_warning = 'recent:save_failed'

    def open_now(self, path, *, create=False, name=None, trigger_word="", known_record=None):
        with self._switch_lock:
            if known_record:
                from ..services.recent_projects import validate_root
                validate_root(known_record)
            with self.condition:
                current = self.context
                if current and create:
                    raise ServiceError(409, "project:already_open")
                if current and not current.closing and current.handle.root == Path(path).resolve():
                    self.remember_project(current)
                    return self.project_view(current)
            self._close_current()
            if known_record:
                validate_root(known_record)
            handle = create_project(path, name=name, trigger_word=trigger_word) if create else open_project(path)
            with self.condition:
                self.generation += 1
                self.context = ProjectContext(str(uuid4()), self.generation, handle)
                context = self.context
            self.remember_project(context)
            self.emit("project.opened", {"project_id": context.project_id, "generation": context.generation, "recovery_pending": False}, project_id=context.project_id,
                      resource={"kind": "project", "id": context.project_id})
            return self.project_view(context)

    def _close_current(self):
        with self.condition:
            context = self.context
            if context is None:
                return
            context.closing = True
        for driver in (self.generation_sources.driver, self.nai_direct.driver):
            if driver and hasattr(driver, 'stop_project'):
                driver.stop_project(context.project_id)
        self.supervisor.stop_project(context.project_id)
        self.duplicates.invalidate(context.project_id)
        self.grants.invalidate(context.project_id)
        self.previews.invalidate(context.project_id)
        with self.condition:
            while context.users:
                self.condition.wait(0.25)
        self.jobs.invalidate(context.project_id)
        self.launcher_checks.invalidate(context.project_id)
        self.vision.invalidate(context.project_id)
        def pause(db):
            from ..project_db.recovery import pause_interrupted
            pause_interrupted(db)
        submit_write(context.handle, pause).result()
        close_project(context.handle)
        with self.storage.lock:
            self.storage.summaries.pop(context.project_id, None)
        with self.condition:
            self.context = None
            self.source_roots = {}
        self.emit("project.closed", {"project_id": context.project_id, "generation": context.generation, "recovery_pending": False}, project_id=context.project_id,
                  resource={"kind": "project", "id": context.project_id})
        self.events.close_project(context.project_id)

    def project_action(self, kind, *, grant_id=None, known_id=None, project_id=None, expected=None, name=None, trigger_word=""):
        known_record = None
        with self.condition:
            if self.control_busy:
                raise ServiceError(409, "project:transition_in_progress")
            if kind == "close":
                context = self.require(project_id)
                Versions.require(expected, self.project_view(context)["version"])
                path = None
                release = None
            elif known_id is not None:
                from ..services.recent_projects import read_lock
                path, known_record = self.recent.resolve(known_id)
                if not self.context or self.context.handle.root != path:
                    with read_lock(path):
                        pass
                release = None
            else:
                path, release = self.grants.acquire(grant_id, "project_create" if kind == "create" else "project_open")
            self.control_busy = True
        def work(task):
            try:
                if kind == "close":
                    with self._switch_lock:
                        submit_write(context.handle, lambda db: Versions.require(expected, self.project_view(context)["version"])).result()
                        self._close_current()
                    return {"closed": True}
                return self.open_now(path, create=kind == "create", name=name, trigger_word=trigger_word, known_record=known_record)
            finally:
                with self.condition:
                    self.control_busy = False
        try:
            return self.supervisor.submit("project." + kind, work, control=True, finalizer=release)
        except BaseException:
            if release:
                release()
            self.control_busy = False
            raise

    def request_shutdown(self, expected, grace_seconds=30):
        Versions.require(expected, self.instance_id)
        with self._shutdown_request_lock:
            if self.shutdown_task:
                return self.shutdown_task
            # This is an intent operation; the server invokes shutdown after its response.
            task = self.supervisor.submit("server.shutdown", lambda task: {"shutdown_requested": True}, control=True)
            self.shutdown_task = task
            self.shutdown_grace_seconds = grace_seconds
            with self.condition:
                self.draining = True
            self.shutdown_requested.set()
        self.emit("server.draining", {"deadline": None, "reason": "user_shutdown"})
        return task

    def shutdown(self):
        with self._shutdown_lock:
            if self.closed:
                return
            self.draining = True
            self.llm.close()
            self.supervisor.close()
            self.request_pool.shutdown(wait=True)
            with self._switch_lock:
                self._close_current()
            self.events.close()
            self.closed = True
