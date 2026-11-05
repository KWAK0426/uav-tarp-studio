"""Require this flight's explicit map search and candidate confirmation."""
import hashlib
import json
from pathlib import Path
import time


MESSAGE = '02에서 현재 날짜·회차의 타프 위치를 먼저 확인하고, 검색한 영상의 체크를 검토한 뒤 최종 확정하세요.'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode('utf8')).hexdigest()


def scope(context):
    return dict(date=context['date'][:10].replace('_', '-'), flight=context['flight'],
                dataset_root=str(Path(context['dataset_root']).resolve()).casefold())


def evidence(context, selection):
    mosaic = Path(context['mosaic']).resolve()
    stat = mosaic.stat()
    return dict(schema=1, **scope(context), identity=context['identity'], mosaic=str(mosaic),
                mosaic_signature=[stat.st_size, stat.st_mtime_ns],
                geometry_sha256=digest({k: selection.get(k) or {} for k in ('locations', 'regions')}))


def marker(context):
    # One review per flight, shared by its DN/radiance/mosaic contexts.
    return Path(context['output']).parent / 'date_location_review.json'


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{time.time_ns()}.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    temp.replace(path)


def begin(context, selection=None):
    value = dict(status='pending', review_id=str(time.time_ns()), **scope(context))
    if selection is not None:
        value.update(evidence(context, selection))
    write(marker(context), value)
    return value['review_id']


def confirm(context, selection, review_id, path):
    current = read(marker(context))
    expected = evidence(context, selection)
    if (not review_id or current.get('review_id') != review_id
            or any(current.get(k) != v for k, v in expected.items())):
        raise ValueError(MESSAGE + ' 위치가 변경됐다면 다시 검색해야 합니다.')
    selection['location_review'] = dict(expected, review_id=review_id)
    write(path, selection)
    write(marker(context), dict(current, status='confirmed', selection=str(Path(path).resolve()),
                                selection_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest()))


def require_selection(path, wanted=None):
    """Reject old approvals, changed geometry/mosaics, and another date/flight."""
    path = Path(path).resolve()
    try:
        context = read(path.parent.parent / 'context.json')
        selection = read(path)
        proof = selection.get('location_review', {})
        expected = evidence(context, selection)
        current = read(marker(context))
        valid = (all(proof.get(k) == v for k, v in expected.items())
                 and proof.get('review_id') == current.get('review_id')
                 and current.get('status') == 'confirmed'
                 and Path(current['selection']).resolve() == path
                 and current['selection_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest())
        if wanted is not None:
            valid = valid and scope(context) == scope(dict(wanted, dataset_root=wanted['root']))
        if not valid:
            raise ValueError(MESSAGE)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(MESSAGE) from exc
    return selection
