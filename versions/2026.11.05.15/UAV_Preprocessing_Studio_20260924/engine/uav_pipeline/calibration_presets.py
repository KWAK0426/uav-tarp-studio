"""Versioned calibration presets; local paths are resolved on each PC."""
import hashlib
import re
from pathlib import Path

PROFILES = {'gain': {'id': 'mx_dual_research_v1', 'file': 'mx_dual_sensor_intercalibration_gain.csv', 'label': 'MX Dual · 연구용 고정 교정값', 'sha256': '302c81465eea20a52898418e767518630f0ebd320673b281acaa4cd54ebd51fa'}, 'tarp': {'id': 'pnu_nias_library_v1', 'file': 'tarp_reflectance_library.csv', 'label': 'PNU 1 m · PNU 3 m · NIAS (10종)', 'sha256': '752bf977af4fa2d756627afba130dbca3308840d479617b7d1fcd39853a23dc4'}}
LEGACY_TARP = {'id': 'tarp_1m_261026', 'file': 'tarp_1m_261026_legacy.csv', 'label': '기존 1 m 이름 (1m_Black/Gray/White)', 'sha256': 'cf2910f16b0e62abf8876b2f8c0ea253dd4612265168c57abf2a2149ead41022'}
DEFAULTS = Path(__file__).resolve().parents[1] / 'defaults'


def tarp_library_key(name):
    match = re.fullmatch(r'(PNU_s_(?:3|36|56))__(?:[2-9]|[1-9]\d+)', name)
    if match: return match[1]
    return 'PNU_03' if name == 'PNU_3' else name


def expand_tarp_names(value):
    """PNU_s is one three-tarp set; repeated small tarps are separate instances."""
    tokens = value.split(',') if isinstance(value, str) else value
    names = []
    for token in tokens:
        token = token.strip()
        if not token: continue
        members = ('PNU_s_3','PNU_s_36','PNU_s_56') if token == 'PNU_s' else (token,)
        for member in members:
            name = member
            if member in names and member in ('PNU_s_3','PNU_s_36','PNU_s_56'):
                number = 2
                while f'{member}__{number}' in names: number += 1
                name = f'{member}__{number}'
            if name in names: raise ValueError(f'타프 ID가 중복됩니다: {name}')
            names.append(name)
    return names

def preset_path(kind, profile=None):
    preset = LEGACY_TARP if kind == "tarp" and profile == LEGACY_TARP["id"] else PROFILES[kind]
    path = DEFAULTS / preset['file']
    if hashlib.sha256(path.read_bytes()).hexdigest() != preset['sha256']:
        raise ValueError('내장 교정 자료가 변경되었습니다. 원본 패키지를 복원하거나 사용자 CSV로 명시적으로 연결하세요.')
    return str(path)

def resolve(cfg):
    settings = cfg.setdefault('preparation', {})
    for kind, key in (('gain', 'gain_csv'), ('tarp', 'tarp_reflectance_csv')):
        profile = settings.get(kind + '_profile', 'custom')
        if profile == 'custom':
            continue
        if profile != PROFILES[kind]['id'] and not (kind == 'tarp' and profile == LEGACY_TARP['id']):
            raise ValueError('알 수 없는 교정 자료: ' + str(profile))
        settings[key] = preset_path(kind, profile)
    return cfg

def use_defaults(cfg):
    settings = cfg.setdefault('preparation', {})
    for kind in PROFILES:
        settings[kind + '_profile'] = PROFILES[kind]['id']
    return resolve(cfg)
