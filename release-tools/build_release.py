"""Build source-only, allowlisted app packages. Never archive a research directory."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

ROOT=Path(__file__).resolve().parents[1]
ROOT_FILES=['assets/studio.ico','tools/create_shortcuts.ps1','UAV_Studio.exe','bootstrap.ps1','downloads.json','README.md','MAINTENANCE.md',
            'VALIDATION.md','rollback.ps1','이전_버전_복원.bat',
            'tools/runtime_probe.py','tools/UAV_Studio.cs','tools/ExifToolShim.cs']
APPROVED_CALIBRATION = {'UAV_Preprocessing_Studio_20260924/engine/defaults/mx_dual_sensor_intercalibration_gain.csv': '302c81465eea20a52898418e767518630f0ebd320673b281acaa4cd54ebd51fa', 'UAV_Preprocessing_Studio_20260924/engine/defaults/tarp_reflectance_library.csv': '752bf977af4fa2d756627afba130dbca3308840d479617b7d1fcd39853a23dc4'}
APPROVED_CALIBRATION['UAV_Preprocessing_Studio_20260924/engine/defaults/tarp_1m_261026_legacy.csv'] = 'cf2910f16b0e62abf8876b2f8c0ea253dd4612265168c57abf2a2149ead41022'
APP_EXTRA=['UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_native_qc_ui.py','location_mosaic.py','test_location_mosaic.py','ed_import.py','test_ed_import.py','app_identity.py','assets/studio.ico','assets/studio.png','desktop.py','studio_update.py','channel.json','test_desktop.py','UPSTREAM_MANIFEST.json', 'test_calibration_presets.py',
            'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/calibration_presets.py', 'UAV_Preprocessing_Studio_20260924/engine/defaults/tarp_1m_261026_legacy.csv']

APP_EXTRA += ['UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_popup.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_regions_ui.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_workflow.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_tool/code/context.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_tool/code/server.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_tool/code/date_locations.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_tool/code/location_review.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_tool/code/saturation_qc.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_tool/code/quick_tarp_qc.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/general_tarp_review.py', 'test_general_tarp_review.py']


APP_EXTRA += ['native_qc_review.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_native_proposal.py']
APP_EXTRA += ['UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_batch_qc.py', 'UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/tarp_batch_qc_ui.py', 'test_batch_qc.py']
APP_EXTRA += ['UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/roi_batch_import.py']
APP_EXTRA += ['UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/roi_auto_prepare.py']
APP_EXTRA += ['UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/progress_ui.py']
APP_EXTRA += ['test_progress_ui.py']
APP_EXTRA += ['completed_result.py', 'test_completed_result.py']
APP_EXTRA += ['UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/band_scope.py','test_auto_band_elc.py']
APP_EXTRA += ['UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/button_guide.py']
APP_EXTRA += ['UAV_Preprocessing_Studio_20260924/engine/uav_pipeline/completed_result.py']


def app_files(app):
    upstream=json.loads((app/'UPSTREAM_MANIFEST.json').read_text(encoding='utf-8'))
    names=sorted(set([row['path'] for row in upstream['files']]+APP_EXTRA))
    for name in names:
        path=(app/name).resolve();path.relative_to(app.resolve())
        if not path.is_file() or path.is_symlink(): raise ValueError('Missing source: '+name)
        if path.stat().st_size>2_000_000: raise ValueError('Unexpected source size: '+name)
        raw=path.read_bytes()
        if re.search(rb'(?i)([A-Z]:[/\\]+Users[/\\]+|[JF]:[/\\]|sk-proj-|BEGIN (?:RSA )?PRIVATE KEY|ghp_[A-Za-z0-9]{20})',raw):
            raise ValueError('Private path/credential pattern: '+name)
        if path.suffix=='.csv':
            defaults = {'UAV_Preprocessing_Studio_20260924/engine/defaults/mx_dual_sensor_intercalibration_gain.csv',
                        'UAV_Preprocessing_Studio_20260924/engine/defaults/tarp_reflectance_library.csv'}
            if not name.startswith('templates/') and name not in APPROVED_CALIBRATION: raise ValueError('CSV outside templates')
            with path.open(encoding='utf-8-sig',newline='') as stream:
                rows=list(csv.DictReader(stream))
            if name in APPROVED_CALIBRATION and rows:
                if hashlib.sha256(raw).hexdigest() != APPROVED_CALIBRATION[name]:
                    raise ValueError('Unapproved built-in calibration contents: '+name)
            elif name.endswith('sensor_gain.csv'):
                if any(row.get('gain','').strip() for row in rows): raise ValueError('Measured gains must not ship')
            elif rows: raise ValueError('Measured rows must not ship')
    return names


def write_manifest(app,names,version):
    records=[{'path':name,'bytes':(app/name).stat().st_size,
              'sha256':hashlib.sha256((app/name).read_bytes()).hexdigest()} for name in names]
    (app/'PACKAGE_MANIFEST.json').write_text(json.dumps({'release':version,'edition':'Native tarp reflectance desktop',
        'included_calibration':'User-approved MX Dual gains and PNU small/large and NIAS tarp measurements; legacy IDs preserved',
        'excluded':['imagery','flight Ed measurements','ROI','coefficients','results','credentials','local settings','logs'],
        'files':records},ensure_ascii=False,indent=2),encoding='utf-8')


def build(version,source):
    if not re.fullmatch(r'\d{4}\.\d{2}\.\d{2}\.\d+',version): raise ValueError('Use YYYY.MM.DD.N')
    names=app_files(source)
    output=ROOT/'release-output'/version
    if output.exists(): raise FileExistsError('Release output already exists; use a new version or a reviewed clean destination.')
    payload=output/'payload'; payload.mkdir(parents=True)
    for name in names:
        target=payload/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/name,target)
    config=json.loads((payload/'channel.json').read_text(encoding='utf-8'));config['version']=version
    (payload/'channel.json').write_text(json.dumps(config,indent=2),encoding='utf-8')
    write_manifest(payload,names,version)
    update=output/'studio-update.zip'
    with zipfile.ZipFile(update,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in names+['PACKAGE_MANIFEST.json']: archive.write(payload/name,name)
    install=output/'UAV-Tarp-Studio-Windows.zip'
    with zipfile.ZipFile(install,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in ROOT_FILES: archive.write(ROOT/name,'UAV-Tarp-Studio/'+name)
        for name in names+['PACKAGE_MANIFEST.json']:
            archive.write(payload/name,'UAV-Tarp-Studio/versions/'+version+'/'+name)
        archive.writestr('UAV-Tarp-Studio/settings/active.json',json.dumps({'version':version}))
        archive.writestr('UAV-Tarp-Studio/updates/README.txt','Downloads, staging and previous version records. Research data is never included.\n')
        for name in ('downloads','staging','backups'): archive.writestr('UAV-Tarp-Studio/updates/'+name+'/',b'')
    sums=''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in (install,update))
    (output/'SHA256SUMS.txt').write_text(sums,encoding='ascii')
    print(json.dumps({'version':version,'files':len(names)+1,'installer_bytes':install.stat().st_size,
        'update_bytes':update.stat().st_size,'output':str(output)},ensure_ascii=False))
    return output


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--version',required=True);p.add_argument('--source',type=Path)
    a=p.parse_args()
    source=a.source or ROOT/'versions'/json.loads((ROOT/'settings/active.json').read_text(encoding='utf-8-sig'))['version']
    build(a.version,source.resolve())
