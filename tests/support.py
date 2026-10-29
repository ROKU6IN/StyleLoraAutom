import base64
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from stylelora.project_db import commands
from stylelora.project_db.artifacts import prepare_artifact
from stylelora.project_db.connection import read_snapshot, submit_write
from stylelora.project_db.project import close_project, create_project, open_project
from stylelora.project_db.repositories import annotations, jobs, sources
from stylelora.project_db.rules import make_rule_plan
from stylelora.project_db.types import (
    AnnotationInput, ImageFilter, ItemSeed, JobOutcome, JobRequest, JobSnapshot, OriginalInput,
    RuleImageChange, Scope, SourceDraft, Tag,
)

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = ROOT / ".test-tmp"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aT9sAAAAASUVORK5CYII="
)


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.root = Path(self.temp.name) / "样例.slproj"
        self.project = create_project(self.root, name="中文项目", trigger_word="my_style")
        self.addCleanup(self.cleanup_project)

    def cleanup_project(self):
        self.project._fault_hook = None
        close_project(self.project)
        self.temp.cleanup()

    def write(self, fn, *args, **kwargs):
        return submit_write(self.project, lambda db: fn(db, *args, **kwargs)).result(timeout=20)

    def read(self, fn, *args, **kwargs):
        with read_snapshot(self.project) as db:
            return fn(db, *args, **kwargs)

    def sql(self, sql, parameters=()):
        return self.read(lambda db: tuple(tuple(row) for row in db.execute(sql, parameters)))

    def execute(self, sql, parameters=()):
        return self.write(lambda db: db.execute(sql, parameters).rowcount)

    def reopen(self):
        close_project(self.project)
        self.project = open_project(self.root)
        return self.project

    def source(self, count=1, *, importing=False, start=True, cost_policy="ask"):
        if importing:
            items = tuple(ItemSeed(i, f"input{i}.png") for i in range(1, count + 1))
            config = {
                "schema_version": 1, "reader": "txt_sidecar",
                "params": {"schema_version": 1, "encoding": "utf-8-sig"},
                "import_manifest": [{"seq": item.seq, "path": "nested/" + item.prompt} for item in items],
            }
        else:
            items = tuple(ItemSeed(i, f"content {i}", "square") for i in range(1, count + 1))
            config = {
                "schema_version": 1, "profile": {"model": "fixture-nai", "sampler": "fixture",
                    "steps": 20, "scale": 5.0, "quality_toggle": False, "uc_preset": None},
                "default_aspect": "square",
                "sizes": {"landscape": {"width": 768, "height": 512},
                          "portrait": {"width": 512, "height": 768},
                          "square": {"width": 640, "height": 640}},
            }
        source = self.write(sources.create_source, SourceDraft(
            "import" if importing else "nai", "fixture source", config, cost_policy,
            target_count=None if importing else count,
        ))
        if start:
            source = self.write(commands.start_source, source.source_id, items)
        return source, items

    def item(self, source_id, seq=1):
        return self.read(lambda db: sources.get_item(db, db.execute(
            "SELECT item_id FROM source_items WHERE source_id=? AND seq=?", (source_id, seq),
        ).fetchone()[0]))

    def stage(self, item, *, tag_values=("A",), originals=None):
        item = self.write(commands.mark_sent, item.item_id).value
        relative = f"images/.pending/{item.item_id}/{item.attempts}/out.png"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        if originals is None:
            originals = (OriginalInput("tags", tuple(Tag(value) for value in tag_values)),)
        artifact = prepare_artifact(self.project, item_id=item.item_id, attempt=item.attempts,
                                    relative_staging_path=relative, width=1, height=1, originals=originals)
        return artifact, originals

    def image(self, *, tag_values=("A",), importing=False, originals=None):
        source, _ = self.source(importing=importing)
        artifact, originals = self.stage(self.item(source.source_id), tag_values=tag_values, originals=originals)
        return self.write(commands.complete_item, artifact, originals).value

    def job(self, image_ids, *, executor="wd", mode="selected", target_types=("tags",), model="fixture-model"):
        scope = Scope(selected_ids=tuple(image_ids)) if mode == "selected" else Scope(
            filter=ImageFilter(include_missing=True))
        request = JobRequest(executor, mode, scope)
        job = self.write(jobs.create_batch, uuid.uuid4().hex, (request,))[0]
        return self.write(jobs.freeze_job, job.job_id, JobSnapshot(model, target_types=target_types),
                          image_ids=tuple(image_ids)).value

    def model_result(self, image_id, tag_values, *, cache="fixture-cache", mode="selected",
                     executor="wd", natural_language=None):
        types = ("tags", "nl") if natural_language is not None else ("tags",)
        job = self.job((image_id,), executor=executor, mode=mode, target_types=types)
        layer = "wd" if executor == "wd" else "llm"
        values = [AnnotationInput(
            image_id, layer, "tags", tuple(value if isinstance(value, Tag) else Tag(value, 0.9) for value in tag_values),
            model_name="fixture-model", params={"schema_version": 1, "requested": {}, "actual": {}},
            job_id=job.job_id, cache_key=cache + ":tags",
        )]
        if natural_language is not None:
            values.append(replace(values[0], type="nl", content=natural_language, cache_key=cache + ":nl"))
        outcome = JobOutcome(image_id, "done", tuple(values))
        result = self.write(commands.commit_job_outcome, job.job_id, outcome)
        return job, outcome, result

    def rule_plan(self, desired, *, scope_ids=None, rule=None):
        changes = []
        for image_id, values in desired.items():
            old = self.read(annotations.get_current, image_id, "rule", "tags")
            changes.append(RuleImageChange(image_id, tuple(Tag(value) for value in values),
                                           old.annotation_id if old else None))
        return self.read(make_rule_plan, rule=rule or {"schema_version": 1, "name": "fixture"},
                         scope=Scope(selected_ids=tuple(sorted(scope_ids or desired))),
                         changes=tuple(changes))

    def apply(self, desired):
        return self.write(commands.apply_rule, self.rule_plan(desired))

    def fail_at(self, stage):
        def hook(current):
            if current == stage:
                raise OSError("injected crash window: " + stage)
        self.project._fault_hook = hook
