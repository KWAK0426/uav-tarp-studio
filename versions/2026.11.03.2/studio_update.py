"""Manual GitHub Releases update, with staged immutable versions and rollback."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import time
import requests
import zipfile

APP = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('UAV_STUDIO_ROOT', str(APP.parents[1]))).resolve()
VERSION = re.compile(r'^\d{4}\.\d{2}\.\d{2}\.\d+$')
MAX_PACKAGE = 100 * 1024 * 1024


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def channel():
    config = json.loads((APP/'channel.json').read_text(encoding='utf-8'))
    if config['repository'] != 'KWAK0426/uav-tarp-studio':
        raise ValueError('승인된 업데이트 저장소가 아닙니다.')
    return config


def get_json(url):
    # Requests uses its maintained CA bundle even when an existing Conda Python
    # has no usable OpenSSL default CA file. Certificate verification stays on.
    with requests.get(url, headers={'User-Agent': 'UAV-Tarp-Studio',
                      'Accept': 'application/vnd.github+json'}, timeout=25, stream=True) as response:
        response.raise_for_status()
        raw = bytearray()
        for chunk in response.iter_content(65536):
            raw.extend(chunk)
            if len(raw) > 2_000_000:
                raise ValueError('업데이트 정보가 너무 큽니다.')
    return json.loads(raw)


def check_update():
    config = channel()
    release = get_json('https://api.github.com/repos/' + config['repository'] + '/releases/latest')
    version = release['tag_name'].removeprefix('v')
    if not VERSION.fullmatch(version):
        raise ValueError('지원하지 않는 업데이트 버전입니다.')
    asset = next((a for a in release['assets'] if a['name'] == config['asset']), None)
    if not asset or not 0 < asset['size'] <= MAX_PACKAGE:
        raise ValueError('정상 업데이트 패키지가 없습니다.')
    digest = asset.get('digest', '') or ''
    if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        raise ValueError('GitHub의 SHA256 정보가 없어 업데이트하지 않습니다.')
    url = asset['browser_download_url']
    prefix = 'https://github.com/' + config['repository'] + '/releases/download/'
    if not url.startswith(prefix):
        raise ValueError('업데이트 다운로드 주소가 다릅니다.')
    newer = tuple(map(int, version.split('.'))) > tuple(map(int, config['version'].split('.')))
    return dict(version=version, current=config['version'], newer=newer, url=url,
                sha256=digest.split(':')[1].lower(), size=asset['size'],
                notes=release.get('body', '')[:4000], release_url=release['html_url'])


def download(url, target, expected, max_bytes):
    if not url.startswith('https://github.com/'):
        raise ValueError('HTTPS 공식 배포 주소만 사용할 수 있습니다.')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.part')
    digest, total = hashlib.sha256(), 0
    with requests.get(url, headers={'User-Agent': 'UAV-Tarp-Studio'}, timeout=60,
                      stream=True) as response, temporary.open('wb') as stream:
        response.raise_for_status()
        for chunk in response.iter_content(1024 * 1024):
            total += len(chunk)
            if total > max_bytes:
                raise ValueError('다운로드 크기 제한을 초과했습니다.')
            digest.update(chunk)
            stream.write(chunk)
    if digest.hexdigest() != expected.lower():
        raise ValueError('SHA256 불일치. 기존 버전은 유지됩니다.')
    os.replace(temporary, target)
    return target


def safe_extract(archive, destination, max_bytes=500 * 1024 * 1024):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 10000 or sum(e.file_size for e in entries) > max_bytes:
            raise ValueError('압축파일 크기 제한 초과')
        seen = set()
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            if ('\\' in name or ':' in name or path.is_absolute() or '..' in path.parts
                    or not path.parts or any(p.endswith((' ', '.')) for p in path.parts)
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('안전하지 않은 압축 경로: ' + name)
            target = (destination / name).resolve()
            target.relative_to(destination)
            if name.casefold() in seen:
                raise ValueError('중복 압축 경로: ' + name)
            seen.add(name.casefold())
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open('xb') as stream:
                    stream.write(bundle.read(entry))


def verify_package(folder):
    folder = Path(folder).resolve()
    manifest = json.loads((folder/'PACKAGE_MANIFEST.json').read_text(encoding='utf-8'))
    expected = set()
    for row in manifest['files']:
        relative = PurePosixPath(row['path'])
        if relative.is_absolute() or '..' in relative.parts or ':' in row['path'] or '\\' in row['path']:
            raise ValueError('패키지 경로 오류')
        path = (folder / row['path']).resolve()
        path.relative_to(folder)
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes']:
            raise ValueError('누락 또는 잘못된 패키지 파일: '+row['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('파일 검증 실패: '+row['path'])
        expected.add(row['path'])
    actual = {p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file()}
    if actual != expected | {'PACKAGE_MANIFEST.json'}:
        raise ValueError('배포 목록 밖의 파일이 있습니다.')
    return manifest


def prepare_update(info):
    if not info.get('newer') or not VERSION.fullmatch(info['version']):
        raise ValueError('새로운 업데이트가 아닙니다.')
    target = ROOT/'versions'/info['version']
    if target.exists():
        verify_package(target)
    else:
        archive = ROOT/'updates/downloads'/('studio-'+info['version']+'.zip')
        download(info['url'], archive, info['sha256'], MAX_PACKAGE)
        staging = ROOT/'updates/staging'/(info['version']+'-'+str(time.time_ns()))
        safe_extract(archive, staging)
        manifest = verify_package(staging)
        candidate = json.loads((staging/'channel.json').read_text(encoding='utf-8'))
        if candidate['version'] != info['version'] or candidate['repository'] != channel()['repository']:
            raise ValueError('패키지 버전 또는 저장소가 다릅니다.')
        result = subprocess.run([sys.executable, '-B', str(staging/'distribution.py'), 'check'],
            capture_output=True, text=True, encoding='utf-8', timeout=180,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            write_json(ROOT/'updates/last_failure.json', {'version': info['version'],
                'reason': '환경·패키지 검사 실패', 'details': result.stdout[-8000:] + result.stderr[-2000:]})
            raise ValueError('새 버전 환경 검사가 실패했습니다. 기존 버전 유지. updates/last_failure.json 확인')
        # Diagnostic scratch is outside the version via UAV_STUDIO_DATA.
        os.replace(staging, target)
    write_json(ROOT/'updates/pending.json', {'version': info['version'], 'previous': info['current']})
    return target


def activate(version):
    if not VERSION.fullmatch(version):
        raise ValueError('잘못된 버전')
    target = ROOT/'versions'/version
    verify_package(target)
    active = ROOT/'settings/active.json'
    old = json.loads(active.read_text(encoding='utf-8-sig'))
    write_json(ROOT/'updates/backups/previous.json', old)
    write_json(active, {'version': version})


def install_codex():
    config = json.loads((ROOT/'downloads.json').read_text(encoding='utf-8'))['codex']
    target = ROOT/'tools/codex'/config['version']
    expected_name = 'codex-x86_64-pc-windows-msvc.exe'
    if not (target/expected_name).is_file():
        archive = ROOT/'updates/downloads'/('codex-'+config['version']+'.zip')
        download(config['url'], archive, config['sha256'], 400 * 1024 * 1024)
        safe_extract(archive, target, max_bytes=900 * 1024 * 1024)
    executable = target/expected_name
    if not executable.is_file():
        raise ValueError('다운로드에 Codex 실행 파일이 없습니다.')
    result = subprocess.run([str(executable), '--version'], capture_output=True, text=True,
        timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('Codex 실행 검증 실패')
    write_json(ROOT/'settings/codex.json', {'executable': str(executable), 'version': result.stdout.strip()})
    return result.stdout.strip()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--activate')
    parser.add_argument('--wait-pid', type=int)
    args = parser.parse_args()
    if args.wait_pid:
        import psutil
        try:
            psutil.Process(args.wait_pid).wait(timeout=120)
        except psutil.NoSuchProcess:
            pass
    if args.activate:
        activate(args.activate)
        subprocess.Popen([str(ROOT/'UAV_Studio.exe')], cwd=ROOT)