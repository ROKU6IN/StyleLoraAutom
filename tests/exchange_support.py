import json
from pathlib import Path

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from stylelora.annotations.editing import review_images
from stylelora.exchange.importer import import_images
from stylelora.project_db.repositories import images
from stylelora.tagging.catalog import load_catalog
from .support import DatabaseTest


class ExchangeTest(DatabaseTest):
    def setUp(self):
        super().setUp()
        self.inputs = Path(self.temp.name) / "输入素材"
        self.inputs.mkdir()

    def input_image(self, name="image.png", *, color="red", caption="Blue_Hair, smile\n\n原始描述。", encoding="utf-8", metadata=None):
        path = self.inputs / name
        path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (8, 6), color)
        kwargs = {}
        if metadata:
            info = PngInfo()
            for key, value in metadata.items():
                info.add_text(key, value)
            kwargs["pnginfo"] = info
        image.save(path, **kwargs)
        if caption is not None:
            path.with_suffix(".txt").write_bytes(caption.encode(encoding))
        return path

    def imported(self, **kwargs):
        self.input_image(**kwargs)
        report = import_images(self.project, self.inputs)
        self.assertEqual((report.imported, report.failed, report.pending, report.unresolved), (1, 0, 0, 0))
        return self.read(images.get_image, next(item.image_id for item in report.items if item.status == "done"))

    def kept(self, **kwargs):
        image = self.imported(**kwargs)
        review_images(self.project, (image.image_id,), "keep")
        return image

    def catalog(self, tags=None, *, complete=False):
        path = Path(self.temp.name) / "catalog.json"
        path.write_text(json.dumps({"schema_version": 1, "kind": "tag_catalog", "complete": complete,
                                   "tags": tags or [{"tag": "blue_hair", "aliases": ["azure_hair"]},
                                                    {"tag": "smile"}]}), encoding="utf-8")
        return load_catalog(path)

    def database_contents(self):
        return {table: self.sql(f"SELECT * FROM {table} ORDER BY 1") for table in (
            "sources", "source_items", "images", "annotations", "jobs", "operation_log", "import_schemes", "export_schemes")}
