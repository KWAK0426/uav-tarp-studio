"""One wavelength-based output scope shared by fit, apply and validation."""
from html import escape


def bands(cfg):
    scope = cfg['preparation'].get('auto_band_scope')
    if scope is None:
        return list(range(1, 11))
    kept, excluded = scope['included'], scope['excluded']
    if (not kept or any(type(b) is not int or b not in range(1,11) for b in kept)
            or kept != sorted(set(kept)) or set(excluded) != {str(b) for b in set(range(1,11))-set(kept)}
            or any(not isinstance(reason,str) or not reason.strip() for reason in excluded.values())):
        raise ValueError('정상 밴드 목록과 제외 사유가 일치하지 않습니다.')
    return kept


def describe(cfg):
    scope = cfg['preparation'].get('auto_band_scope')
    if scope is None:return ''
    names=cfg['band_names']
    text=', '.join(names[str(b)] for b in bands(cfg))
    excluded='; '.join(f'{names[b]}: {reason}' for b,reason in scope['excluded'].items())
    return f'<p><b>산출 {len(bands(cfg))}/10 밴드</b>: {escape(text)}</p><p>제외: {escape(excluded or "없음")}</p>'
