"""Read-only presentation metadata; never part of a generation receipt contract."""
from copy import deepcopy

from .profile import DEFAULT_CONFIG, capabilities


def form_capabilities():
    # Keep the wire/receipt digest stable when only labels or defaults change.
    labels = {
        'config': '生成参数', 'config.schema_version': '配置版本', 'config.backend': '生成后端',
        'config.default_aspect': '默认图幅', 'config.sizes': '实际像素尺寸',
        'config.profile': '模型参数', 'config.profile.model': '模型', 'config.profile.effort': '生成档位',
        'config.profile.sampler': '采样器', 'config.profile.steps': '步数',
        'config.profile.scale': '提示词引导强度', 'config.profile.cfg_rescale': 'CFG 重缩放',
        'config.profile.noise_schedule': '噪声调度', 'config.profile.uc_preset': '负面词预设',
        'config.profile.quality_toggle': '质量词开关', 'config.profile.smea': 'SMEA',
        'config.profile.smea_dyn': '动态 SMEA', 'config.profile.variety_plus': 'Variety+',
        'config.profile.decrisp': 'Decrisp',
    }
    for aspect, name in [('landscape', '横图'), ('portrait', '竖图'), ('square', '方图')]:
        prefix = 'config.sizes.' + aspect
        labels.update({prefix: name, prefix + '.width': name + '宽度', prefix + '.height': name + '高度'})
    return {**capabilities(), 'form': {
        'version': 1, 'default_config': deepcopy(DEFAULT_CONFIG), 'labels': labels,
        'fixed_hint': '当前接口固定此值；其它取值尚未开放。',
        'hint': '单张文生图；字符提示词、参考图与批量生成尚未开放。尺寸须与图幅方向一致。',
    }}
