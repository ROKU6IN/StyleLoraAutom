"""A065: verify restored original bytes before clearing file_missing."""
from ..project_db.connection import read_snapshot, submit_write
from ..project_db.errors import FileMissing
from ..project_db.repositories import images
from ..project_db.validation import utc_now
from ..runtime.contracts import Versions
from ..runtime.errors import ServiceError
from .storage import exclusive
from .verified_file import verified_file


def start(runtime, context, image_id, expected):
    project, dataset = context.handle, runtime.dataset
    with read_snapshot(project) as db:
        image = images.get_image(db, image_id)
        Versions.require(expected, dataset.image_version(context, image))

    def work(task):
        with exclusive(project):
            with read_snapshot(project) as db:
                image = images.get_image(db, image_id)
                Versions.require(expected, dataset.image_version(context, image))
            try:
                with verified_file(project, image, lambda: task.cancel.is_set() or context.closing) as check:
                    def write(db):
                        check()
                        current = images.get_image(db, image_id)
                        Versions.require(expected, dataset.image_version(context, current))
                        if (current.filename, current.sha256, current.file_size) != (image.filename, image.sha256, image.file_size):
                            raise ServiceError(409, 'image:file_changed')
                        result = images.set_file_missing(db, image_id, missing=False, verified_sha256=image.sha256)
                        check()
                        if result.changed:
                            db.after_commit.append(lambda: runtime.versions.bump(dataset.key(context, 'image', image_id)))
                        return result.changed
                    changed = submit_write(project, write).result()
                    # The pinned Windows handles remain held through the commit.
            except FileNotFoundError:
                raise ServiceError(410, 'image:file_missing') from None
            except FileMissing:
                raise ServiceError(409, 'image:hash_mismatch') from None
            except PermissionError:
                raise ServiceError(403, 'image:file_unreadable') from None
            if changed:
                runtime.emit('image.updated', {'image_ids': [image_id], 'fields': ['file_missing']},
                             project_id=context.project_id, resource={'kind': 'image', 'id': image_id})
            return {'image': dataset.get_image(context, image_id), 'changed': changed,
                    'verified_sha256': image.sha256, 'verified_bytes': image.file_size, 'as_of': utc_now()}

    return runtime.supervisor.submit('image.verify_file', work, context=context)
