"""F05/F01 boundary: validated staging files and durable item/attempt receipts.

Image decoding and original-prompt parsing belong to F05. This module verifies
the supplied fingerprint and manages durable evidence; it never calls a backend.
"""

from dataclasses import asdict, replace
from pathlib import Path

from .errors import ArtifactConflict, FileMissing, ValidationError
from .storage import atomic_json
from .types import ImageArtifact, OriginalInput, Tag
from .validation import (
    EXTENSIONS, decode, digest, encode, file_hash, integer, object_json, relative_path, sha256, tags,
)


def receipt_path(root, item_id, attempt):
    integer(item_id, "item ID", 1)
    integer(attempt, "attempt", 1)
    return relative_path(root, f"images/.pending/{item_id}/{attempt}/receipt.json")


def validate_artifact(root, artifact):
    integer(artifact.item_id, "item ID", 1)
    integer(artifact.attempt, "attempt", 1)
    integer(artifact.width, "width", 1)
    integer(artifact.height, "height", 1)
    integer(artifact.file_size, "file size", 1)
    sha256(artifact.sha256)
    if artifact.extension not in EXTENSIONS:
        raise ValidationError("Unsupported or non-lowercase image extension")
    prefix = f"images/.pending/{artifact.item_id}/{artifact.attempt}/"
    if not artifact.relative_staging_path.startswith(prefix):
        raise ArtifactConflict("Staging path does not belong to this item and attempt")
    path = relative_path(root, artifact.relative_staging_path)
    if path.suffix != artifact.extension or path.name == "receipt.json":
        raise ValidationError("Staging extension does not match the artifact")
    if artifact.png_meta is not None:
        object_json(artifact.png_meta)
    validate_registration_check(artifact.registration_check)
    return path


def validate_registration_check(value):
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'review_status', 'summary'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['review_status'] not in ('unreviewed', 'reject')):
        raise ValidationError('Invalid registration check')
    object_json(value['summary'], versioned=True)
    if len(encode(value).encode('utf-8')) > 65536:
        raise ValidationError('Registration check is too large')


def artifact_payload(artifact):
    value = asdict(artifact)
    value.pop('receipt_digest')
    if value['registration_check'] is None:
        value.pop('registration_check')  # Preserve old v1 receipts byte-for-byte.
    return value


def check_archive(root, artifact, *, create=False):
    """Keep checks independent of annotations, including images with no original."""
    if artifact.registration_check is None:
        return
    validate_registration_check(artifact.registration_check)
    value = {'schema_version': 1, 'item_id': artifact.item_id, 'attempt': artifact.attempt,
             'sha256': artifact.sha256, 'check': artifact.registration_check}
    expected = {'content': value, 'digest': digest(value)}
    path = relative_path(root, f'images/.checks/{artifact.item_id}/{artifact.attempt}.json')
    if path.exists():
        if path.stat().st_size > 131072 or decode(path.read_text(encoding='utf-8')) != expected:
            raise ArtifactConflict('Registration check archive differs')
    elif create:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, expected)
    else:
        raise ArtifactConflict('Registration check archive is missing')


def originals_from_receipt(receipt):
    result = []
    for item in receipt["originals"]:
        content = tuple(Tag(**tag) for tag in item["content"]) if item["type"] == "tags" else item["content"]
        result.append(OriginalInput(item["type"], content, item["confidence"], item["params"], item["cache_key"]))
    return tuple(result)


def prepare_artifact(project, *, item_id, attempt, relative_staging_path, width, height,
                     originals=(), phash=None, png_meta=None, registration_check=None,
                     expected_sha256=None, expected_file_size=None):
    """Record evidence after F05 has decoded the file, before queuing complete_item."""
    root = project.root
    path = relative_path(root, relative_staging_path)
    import os
    with path.open("rb+") as stream:
        os.fsync(stream.fileno())
    artifact = ImageArtifact(item_id, attempt, relative_staging_path, path.suffix.lower(), width, height,
                             phash, file_hash(path), path.stat().st_size, png_meta, "", registration_check)
    validate_artifact(root, artifact)
    if ((expected_sha256 is not None and artifact.sha256 != expected_sha256)
            or (expected_file_size is not None and artifact.file_size != expected_file_size)):
        raise ArtifactConflict('Artifact changed after inspection')
    if len({item.type for item in originals}) != len(originals):
        raise ValidationError("Only one original annotation per type is allowed")
    for value in originals:
        if value.type == "tags":
            tags(value.content)
        elif value.type != "nl" or not isinstance(value.content, str):
            raise ValidationError("Invalid original annotation")
    payload = {"schema_version": 1, "complete": True, "artifact": artifact_payload(artifact), "originals": originals}
    checksum = digest(payload)
    receipt = {**payload, "digest": checksum}
    target = receipt_path(root, item_id, attempt)
    if target.exists():
        existing = decode(target.read_text(encoding="utf-8"))
        if existing.get("digest") != checksum:
            raise ArtifactConflict("This attempt already has different complete evidence")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        check_archive(root, artifact, create=True)
        atomic_json(target, receipt)
    check_archive(root, artifact)
    return replace(artifact, receipt_digest=checksum)


def load_receipt(root, artifact):
    validate_artifact(root, artifact)
    path = receipt_path(root, artifact.item_id, artifact.attempt)
    if not path.exists():
        raise FileMissing("Complete artifact receipt is missing")
    receipt = decode(path.read_text(encoding="utf-8"))
    payload = {key: receipt[key] for key in ("schema_version", "complete", "artifact", "originals")}
    if payload["schema_version"] != 1 or payload["complete"] is not True:
        raise ArtifactConflict("Incomplete artifact receipt")
    if digest(payload) != receipt.get("digest") or receipt["digest"] != artifact.receipt_digest:
        raise ArtifactConflict("Artifact receipt digest mismatch")
    supplied = artifact_payload(artifact)
    if encode(supplied) != encode(receipt["artifact"]):
        raise ArtifactConflict("Artifact does not match the durable receipt")
    check_archive(root, artifact)
    return receipt


def artifact_from_receipt(receipt):
    return ImageArtifact(**receipt["artifact"], receipt_digest=receipt["digest"])


def verified_file(root, artifact, receipt):
    staged = validate_artifact(root, artifact)
    if staged.is_file():
        path = staged
    else:
        name = receipt.get("filename")
        candidate = receipt.get("candidate_image_id")
        if not isinstance(name, str) or type(candidate) is not int or name != f"{candidate}{artifact.extension}":
            raise FileMissing("Neither a complete staging file nor a trusted renamed file exists")
        path = relative_path(root, "images/" + name)
    if not path.is_file() or path.stat().st_size != artifact.file_size or file_hash(path) != artifact.sha256:
        raise ArtifactConflict("Artifact bytes do not match the receipt")
    return path


def finalize_file(db, artifact, receipt):
    """Reserve a final filename, persist the reservation, then move without overwrite."""
    root = db.project.root
    source = verified_file(root, artifact, receipt)
    if source.parent == root / "images" and db.execute(
        "SELECT 1 FROM images WHERE filename=?", (source.name,),
    ).fetchone():
        raise ArtifactConflict("The renamed file is already owned by another image")
    image_id = db.execute("SELECT COALESCE(MAX(image_id),0)+1 FROM images").fetchone()[0]
    candidate = receipt.get("candidate_image_id")
    filename = receipt.get("filename")
    if candidate is not None and filename == f"{candidate}{artifact.extension}" and not db.execute(
        "SELECT 1 FROM images WHERE image_id=?", (candidate,),
    ).fetchone():
        target = relative_path(root, "images/" + filename)
        if source == target:
            return candidate, filename
    while True:
        filename = f"{image_id}{artifact.extension}"
        target = relative_path(root, "images/" + filename)
        # Rolled-back IDs may have durable, unregistered files. Never overwrite them.
        if not any((root / "images").glob(f"{image_id}.*")):
            break
        image_id += 1
    receipt = {**receipt, "candidate_image_id": image_id, "filename": filename}
    atomic_json(receipt_path(root, artifact.item_id, artifact.attempt), receipt)
    db.project._fault("artifact_before_rename")
    # Windows rename fails if target exists. On POSIX use a no-clobber hard link
    # followed by unlink; both crash windows are recoverable through the receipt.
    import os
    if os.name == "nt":
        source.rename(target)
    else:
        os.link(source, target)
        source.unlink()
    db.project._fault("artifact_after_rename")
    return image_id, filename


def cleanup_receipt(root, item_id, attempt):
    try:
        receipt_path(root, item_id, attempt).unlink(missing_ok=True)
    except OSError:
        # Committed data is authoritative. Recovery can safely clean this later.
        pass
