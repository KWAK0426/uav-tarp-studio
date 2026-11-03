"""GPS-retained exposures, measured timestamps and nearest measured Ed audit."""
from __future__ import annotations
from uav_pipeline.exif_support import executable as exif_executable

import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
from bisect import bisect_left
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from .preparation import initialize, locations
from .general_project import is_general, source_shots, source_signature, raw_for_shots
from .solar import solar_position

KST = timezone(timedelta(hours=9))
SHOT = re.compile(r'^(IMG_\d{4})(?:_(10|[1-9]))?(?:\.tiff?)?$', re.I)


def timestamp(value, zone):
    if zone not in ('UTC', 'Asia/Seoul'):
        raise ValueError('촬영 시간대를 UTC 또는 Asia/Seoul로 확인하세요.')
    value = re.sub(r'^(\d{4}):(\d{2}):(\d{2})', r'\1-\2-\3', str(value).strip())
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return dt.replace(tzinfo=timezone.utc if zone == 'UTC' else KST) if dt.tzinfo is None else dt


def gps_records(path, band):
    from pyproj import CRS, Transformer
    lines = Path(path).read_text(encoding='utf-8-sig').splitlines()
    wkt = next((s.split(':', 1)[1].strip() for s in lines if s.startswith('# CoordinateSystem:')), '')
    if not wkt:
        raise ValueError('GPS.txt에 좌표계 정보가 없습니다. 좌표계를 명시한 내보내기 파일이 필요합니다.')
    crs = CRS.from_user_input(wkt)
    transform = Transformer.from_crs(crs, 'EPSG:4326', always_xy=True)
    header = next((s.lstrip('#').strip().split('\t') for s in lines if s.startswith('#Label') or s.startswith('# Label')), None)
    if not header or not {'Label', 'X_est', 'Y_est', 'Z_est'}.issubset(header):
        raise ValueError('GPS.txt에 Label, X_est, Y_est, Z_est 열이 필요합니다.')
    groups = {}
    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        row = dict(zip(header, line.split('\t')))
        match = SHOT.fullmatch(row.get('Label', ''))
        if match:
            groups.setdefault(match[1].upper(), []).append((int(match[2] or band), row))
    if not groups:
        raise ValueError('GPS에 유효한 IMG 촬영번호가 없습니다.')
    result = []
    for shot, values in sorted(groups.items()):
        selected = [r for b, r in values if b == band]
        row = {'shot': shot, 'representative_band': band, 'gps_label': '', 'gps_crs': crs.to_string(), 'status': 'ok'}
        try:
            if len(selected) != 1:
                raise ValueError('missing_or_duplicate_gps_band')
            row['gps_label'] = selected[0]['Label']
            x, y, z = (float(selected[0][k]) for k in ('X_est', 'Y_est', 'Z_est'))
            if not all(math.isfinite(v) for v in (x, y, z)):
                raise ValueError('invalid_est')
            lon, lat = transform.transform(x, y)
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError('invalid_est')
            row.update(X_est=x, Y_est=y, Z_est=z, longitude=lon, latitude=lat)
        except (ValueError, KeyError) as exc:
            row['status'] = str(exc)
        result.append(row)
    return result


def exif_times(paths):
    result = {}
    for start in range(0, len(paths), 100):
        batch = paths[start:start + 100]
        proc = subprocess.run([exif_executable(), '-charset', 'filename=UTF8', '-json', '-SubSecDateTimeOriginal', '-DateTimeOriginal',
                               '-SubSecTimeOriginal', '-OffsetTimeOriginal', '-@', '-'],
                              input='\n'.join(p.resolve().as_posix() for p in batch) + '\n',
                              capture_output=True, text=True, encoding='utf-8', check=True)
        for row in json.loads(proc.stdout):
            value = row.get('SubSecDateTimeOriginal') or row.get('DateTimeOriginal')
            if value:
                if not row.get('SubSecDateTimeOriginal'):
                    sub = str(row.get('SubSecTimeOriginal', ''))
                    if sub.isdigit():
                        value += '.' + sub
                    value += row.get('OffsetTimeOriginal', '')
                result[str(Path(row['SourceFile']).resolve())] = value
        print(f'촬영시각 읽기: {min(start + 100, len(paths))}/{len(paths)}', flush=True)
    return result


def image_rows(cfg, read_times=exif_times):
    initialize(cfg)
    s = cfg['preparation']
    timestamp('2000-01-01', s['source_timezone'])
    band = int(s.get('time_band', 1))
    if band not in range(1, 11):
        raise ValueError('대표 밴드는 1~10번입니다.')
    if is_general(cfg):
        wanted = source_shots(cfg)
        raw_for_shots(s['raw_directory'], wanted)
        gps = {}
        unavailable = 'no_gps'
        if s.get('gps_path'):
            try:
                gps = {row['shot']: row for row in gps_records(s['gps_path'], band)}
                unavailable = 'no_valid_gps_for_shot'
            except (OSError, ValueError) as exc:
                unavailable = 'invalid_optional_gps: ' + str(exc)
        rows = []
        for shot in sorted(wanted):
            location = gps.get(shot, {})
            available = location.get('status') == 'ok'
            rows.append({'shot': shot, 'representative_band': band, 'status': 'ok',
                         'gps_status': 'available' if available else unavailable,
                         'solar_status': 'available' if available else 'unavailable',
                         **{key: location.get(key, '') if available else '' for key in
                            ('gps_label', 'gps_crs', 'X_est', 'Y_est', 'Z_est', 'longitude', 'latitude')}})
    else:
        rows = gps_records(s['gps_path'], band)
    raw = Path(s['raw_directory'])
    if not raw.is_dir():
        raise ValueError('원영상 폴더를 확인하세요.')
    wanted = {r['shot'] for r in rows}
    found = {}
    for p in raw.rglob('*'):
        m = SHOT.fullmatch(p.name)
        if p.is_file() and p.suffix.lower() in ('.tif', '.tiff') and m and int(m[2] or 0) == band and m[1].upper() in wanted:
            found.setdefault(m[1].upper(), []).append(p)
    times = read_times([v[0] for v in found.values() if len(v) == 1 and v[0].stat().st_size > 0])
    date = cfg['dataset']['date'][:10].replace('_', '-')
    for row in rows:
        row.update(date=date, cycle=cfg['dataset']['flight'], source_path='', capture_time_raw='', capture_time_utc='', capture_time_kst='',
                   sza_deg='', saa_deg='', solar_location='sensor_est', solar_method='NOAA_fractional_year_geometric')
        if is_general(cfg) and row['solar_status'] == 'unavailable':
            row.update(solar_location='unavailable', solar_method='unavailable')
        files = found.get(row['shot'], [])
        if len(files) != 1:
            row['status'] = 'missing_or_duplicate_raw_band'
            continue
        row['source_path'] = str(files[0].resolve())
        value = times.get(row['source_path'])
        try:
            dt = timestamp(value, s['source_timezone'])
            row.update(capture_time_raw=value, capture_time_utc=dt.astimezone(timezone.utc).isoformat(),
                       capture_time_kst=dt.astimezone(KST).isoformat())
            if dt.astimezone(KST).date().isoformat() != date:
                row['status'] = 'capture_date_mismatch'
            if row['status'] == 'ok' and row.get('solar_status') != 'unavailable':
                row['sza_deg'], row['saa_deg'] = solar_position(dt, row['latitude'], row['longitude'])
        except (ValueError, TypeError):
            row['status'] = 'missing_or_invalid_capture_time'
    return rows


def match_ed(rows, csv_path, cfg):
    with Path(csv_path).open(encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        bands = [c for c in columns if re.fullmatch(r'Band\d+_.+', c)]
        expected = [f'Band{k}_{v}' for k, v in cfg['band_names'].items()]
        if not all(c in bands for c in expected) or 'datetime_kst' not in columns:
            raise ValueError('실측 밴드별 Ed CSV에 datetime_kst와 설정된 파장별 Band 열이 필요합니다. 원시 RAMSES 변환은 별도입니다.')
        ed = []
        date = cfg['dataset']['date'][:10].replace('_', '-')
        for number, row in enumerate(reader, 2):
            if row.get('cycle') and row['cycle'] != cfg['dataset']['flight']:
                continue
            dt = timestamp(row['datetime_kst'], 'Asia/Seoul')
            if dt.astimezone(KST).date().isoformat() == date:
                ed.append((dt, number, row))
    ed.sort(key=lambda v: (v[0], v[1]))
    if len({e[0] for e in ed}) != len(ed):
        raise ValueError('Ed에 같은 측정시각이 중복됩니다. 날짜·회차별 단일 실측 목록으로 정리하세요.')
    limit = float(cfg['preparation']['max_ed_delta_seconds'])
    if not math.isfinite(limit) or not 0 <= limit <= 5:
        raise ValueError('Measured Ed tolerance must be between 0 and 5 seconds')
    times = [e[0] for e in ed]
    output = []
    for image in rows:
        row = dict(image, match_status='invalid_image', ed_time_kst='', ed_delta_seconds='', ed_abs_delta_seconds='',
                   ed_source_file='', ed_csv=str(Path(csv_path).resolve()), ed_csv_row='', match_method='nearest_measured_no_interpolation')
        row.update({b: '' for b in bands})
        if image['status'] == 'ok':
            if not ed:
                row['match_status'] = 'no_ed_for_date_cycle'
            else:
                dt = timestamp(image['capture_time_kst'], 'Asia/Seoul')
                i = bisect_left(times, dt)
                candidates = ed[max(0, i - 1):min(len(ed), i + 1)]
                t, number, source = min(candidates, key=lambda v: (abs((v[0] - dt).total_seconds()), v[0]))
                delta = (t - dt).total_seconds()
                valid = True
                for b in bands:
                    try:
                        v = float(source[b])
                        valid &= math.isfinite(v) and v > 0
                    except (ValueError, TypeError):
                        valid = False
                state = 'matched' if abs(delta) <= limit else 'outside_tolerance'
                if not times[0] <= dt <= times[-1]:
                    state = 'outside_measured_range'
                if not valid:
                    state = 'invalid_ed'
                row.update(match_status=state, ed_time_kst=t.astimezone(KST).isoformat(), ed_delta_seconds=delta,
                           ed_abs_delta_seconds=abs(delta), ed_source_file=source.get('source_file', ''), ed_csv_row=number)
                if state == 'matched':
                    row.update({b: source[b] for b in bands})
        output.append(row)
    return output


def write_csv(path, rows):
    columns = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def time_signature(cfg, rows):
    s = cfg['preparation']
    files = []
    for row in rows:
        path = Path(row['source_path']) if row.get('source_path') else None
        stat = path.stat() if path and path.is_file() else None
        files.append((str(path), stat.st_size if stat else None, stat.st_mtime_ns if stat else None))
    result = {'date': cfg['dataset']['date'], 'flight': cfg['dataset']['flight'],
            'raw_root': str(Path(s['raw_directory']).resolve()),
            'band': int(s.get('time_band', 1)), 'timezone': s['source_timezone'], 'source_files': files}
    if is_general(cfg):
        result.update(source_signature(cfg), general=cfg['general'])
    if s.get('gps_path'):
        gps = Path(s['gps_path'])
        result.update(gps_path=str(Path(s['gps_path']).resolve()),
                      gps_sha256=hashlib.sha256(gps.read_bytes()).hexdigest() if gps.is_file() else None)
    return result


def load_prepared_times(cfg, directory):
    candidates = []
    for summary_path in directory.glob('*/summary.json'):
        summary = json.loads(summary_path.read_text(encoding='utf-8'))
        if not summary.get('ed_requested') and summary.get('time_signature'):
            candidates.append((summary_path.stat().st_mtime_ns, summary_path.parent, summary))
    if not candidates:
        raise ValueError('02에서 촬영시간·태양각 CSV를 먼저 생성하세요. 03에서는 촬영시각을 새로 추출하지 않습니다.')
    _, run, summary = max(candidates, key=lambda v: v[0])
    csv_path = run / 'image_time_solar.csv'
    if hashlib.sha256(csv_path.read_bytes()).hexdigest() != summary['time_csv_sha256']:
        raise ValueError('시간 CSV가 변경되었습니다. 02에서 다시 생성하세요.')
    with csv_path.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    # JSON canonicalization also normalizes tuples recorded as arrays.
    actual = json.loads(json.dumps(time_signature(cfg, rows)))
    if actual != summary['time_signature']:
        raise ValueError('GPS·원영상·대표 밴드·시간대가 02 결과와 다릅니다. 02에서 시간 CSV를 다시 생성하세요.')
    return rows, csv_path


def connect_prepared_time_csv(cfg):
    """Connect the latest verified time CSV without replacing a manual choice."""
    current = cfg.setdefault('geometry', {}).get('frame_times_csv', '')
    if current:
        return current
    p = locations(cfg)
    directory = p['base'] / p['base'].name.replace('_00_ELC_Process', '_03_Time_Ed')
    try:
        _, csv_path = load_prepared_times(cfg, directory)
    except (OSError, ValueError, KeyError):
        return ''
    cfg['geometry']['frame_times_csv'] = str(csv_path)
    return str(csv_path)


def export(cfg, with_ed=False):
    initialize(cfg)
    p = locations(cfg)
    directory = p['base'] / (p['base'].name.replace('_00_ELC_Process', '_03_Time_Ed'))
    rows, source_csv = load_prepared_times(cfg, directory) if with_ed else (image_rows(cfg), None)
    run = directory / (datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:6])
    run.mkdir(parents=True)
    write_csv(run / 'image_time_solar.csv', rows)
    summary = {'images': len(rows), 'valid_images': sum(r['status'] == 'ok' for r in rows),
               'time_valid_images': sum(bool(r.get('capture_time_kst')) for r in rows),
               'solar_valid_images': sum(bool(r.get('sza_deg') != '' and r.get('saa_deg') != '') for r in rows),
               'ed_requested': with_ed,
               'representative_band': cfg['preparation'].get('time_band', 1), 'source_timezone': cfg['preparation']['source_timezone'],
               'gps_path': cfg['preparation']['gps_path'], 'max_ed_delta_seconds': cfg['preparation']['max_ed_delta_seconds']}
    if is_general(cfg):
        summary.update(source_signature(cfg))
    summary['image_time_solar_csv'] = str(run / 'image_time_solar.csv')
    summary['time_signature'] = time_signature(cfg, rows)
    summary['time_csv_sha256'] = hashlib.sha256((run / 'image_time_solar.csv').read_bytes()).hexdigest()
    if source_csv:
        summary['source_time_csv'] = str(source_csv)
    valid = [r for r in rows if r['status'] == 'ok']
    if valid:
        ordered = sorted(valid, key=lambda r: r['capture_time_utc'])
        start, end = (timestamp(r['capture_time_utc'], 'UTC') for r in (ordered[0], ordered[-1]))
        middle = start + (end - start) / 2
        summary.update(observation_start_kst=start.astimezone(KST).isoformat(), observation_end_kst=end.astimezone(KST).isoformat(),
                       observation_duration_minutes=(end - start).total_seconds() / 60,
                       observation_time_note='valid retained exposure start-to-end span, not processing runtime')
        summary['midpoint_candidate_not_approved'] = min(valid, key=lambda r: abs((timestamp(r['capture_time_utc'], 'UTC') - middle).total_seconds()))['shot']
    try:
        if with_ed:
            matched = match_ed(rows, cfg['preparation']['ed_csv'], cfg)
            write_csv(run / 'image_ed_matching.csv', matched)
            summary['matched'] = sum(r['match_status'] == 'matched' for r in matched)
            summary['excluded_images'] = sum(r['match_status'] == 'invalid_image' for r in matched)
            summary['unmatched'] = sum(r['status'] == 'ok' and r['match_status'] != 'matched' for r in matched)
    except Exception as exc:
        summary['error'] = str(exc)
        raise
    finally:
        (run / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print('결과 폴더: ' + str(run), flush=True)
    print(json.dumps({k: v for k, v in summary.items() if k not in ('time_signature', 'time_csv_sha256')}, ensure_ascii=False), flush=True)
    return run, summary


if __name__ == '__main__':
    from .config import load_config
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--with-ed', action='store_true')
    args = parser.parse_args()
    export(load_config(args.config), args.with_ed)
