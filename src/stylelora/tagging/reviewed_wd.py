"""Reviewed public model facts. Hash provenance is documented in docs/18.

2026-10-25: user approved checking the publisher's fixed Git blob identity
before pinning the locally computed CSV SHA-256. It is not publisher-issued.
"""
EVA02 = {
    'model_id': 'wd-eva02-large-tagger-v3',
    'repo_id': 'SmilingWolf/wd-eva02-large-tagger-v3',
    'revision': 'b25b82a03f7282e41aa2f257a52c7583b710bd1c',
    'files': [
        {'name': 'model.onnx', 'size': 1260435999,
         'sha256': '9e768793060c7939b277ccb382783e8670e8a042d29d77aa736be0c8cc898bfc'},
        {'name': 'selected_tags.csv', 'size': 308468,
         'sha256': '298633d94d0031d2081c0893f29c82eab7f0df00b08483ba8f29d1e979441217'},
    ],
    'tensor_schema': {'input_name': 'input', 'output_name': 'output', 'size': 448,
        'layout': 'NHWC', 'dtype': 'float32', 'batch': 'dynamic', 'color_order': 'BGR',
        'resize': 'pillow-bicubic', 'normalization': '0_255', 'alpha': 'white',
        'aspect': 'pad_square', 'output_count': 10861},
    'preprocess_version': 'wd-square-v1',
    'csv_schema': {'columns': ['tag_id', 'name', 'category', 'count'],
        'name_column': 'name', 'category_column': 'category', 'general': 0, 'character': 4, 'rating': 9},
}

EVA02_PROVENANCE = {
    'reviewed_at': '2026-10-25',
    'license': 'apache-2.0',
    'model_sha256_source': 'publisher_git_lfs',
    'csv_sha256_source': 'locally_computed_after_publisher_git_blob_verification',
    'csv_git_blob': 'a2a653a5c89c8e387765eb1de9cfd2aedc92ee8d',
}
