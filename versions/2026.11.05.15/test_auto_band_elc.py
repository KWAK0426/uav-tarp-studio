"""Native selected-image workflow: synthetic saturation, real TIFFs, real independent validation."""
import copy
import csv
import json
import tempfile
from unittest.mock import patch
from pathlib import Path
from test_general_e2e import fixture, d
from uav_pipeline.config import load_config, save_config
from uav_pipeline.easy_elc import auto_selected
from uav_pipeline.preparation import locations
from uav_pipeline.roi_studio_bridge import Session, digest


def prepare_fixture(folder, failed_registration=False, extra_target=False):
    config, records, raw = fixture(folder, {
        ('IMG_0001', 475): 'AB', ('IMG_0002', 531): 'AB', ('IMG_0002', 650): 'C'},extra_target=extra_target)
    session = Session(config)
    for shot in session.shots:
        session.images[shot] = copy.deepcopy(records)
    for row in session.images['IMG_0002']:
        row['role'] = 'validation'
    session.reference = 'IMG_0001'
    session.save_draft()
    tool = session._transforms()
    transform = tool.band_transform
    def tested_transform(shot,suffix,*args):
        if failed_registration and (shot,suffix)==('IMG_0002',4):
            raise ValueError(f'{shot} band {suffix}: 정합 후보 모두 실패 (테스트)')
        return transform(shot,suffix,*args)
    with patch.object(tool,'band_transform',tested_transform):
        manifest = session.publish()
    cfg = load_config(config)
    cfg['preparation']['approved_roi_manifest'] = str(manifest)
    save_config(config, cfg)
    return config, raw


def run():
    from osgeo import gdal
    folder = d.STATE / 'e2e'
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='auto_bands_', dir=folder) as directory:
        config, raw = prepare_fixture(Path(directory), failed_registration=True)
        cfg = load_config(config)
        protected = [config, Path(cfg['preparation']['approved_roi_manifest']), *raw.glob('*.tif')]
        before = {p:digest(p) for p in protected}
        done = auto_selected(cfg)
        assert done['status'] == 'complete' and not done['full']
        root = Path(done['config']).parent
        scope = json.loads((root/'band_decision.json').read_text(encoding='utf-8-sig'))['scope']
        assert scope['excluded'].keys() == {'2','3','10'} and len(scope['included']) == 7, scope
        assert '정합' in scope['excluded']['10']
        work = load_config(done['config'])
        paths = locations(work)
        outputs = list(paths['ref'].glob('*.tif'))
        assert len(outputs) == 14
        assert not any(p.stem.split('_')[-1] in ('1','7','4') for p in outputs)
        for p in outputs:
            ds = gdal.Open(str(p))
            assert ds.GetRasterBand(1).DataType == gdal.GDT_Float32
            assert abs(float(ds.ReadAsArray(80,80,1,1)[0,0])-.1) < .0001
            ds = None
        with (paths['coef']/'elc_priority_reference_validation.csv').open(encoding='utf-8-sig') as f:
            rows=list(csv.DictReader(f))
        independent=[r for r in rows if r['role']=='independent_validation']
        assert {int(r['band']) for r in independent} == set(scope['included'])
        assert all(abs(float(r['bias'])) < .0001 for r in independent)
        assert any(r['role']=='excluded_saturated_independent_validation' for r in rows)
        assert all(digest(p)==sha for p,sha in before.items())
        with patch('uav_pipeline.tarp_tool.code.saturation_qc.scan_roi', return_value={'saturated_pct':100.0}):
            try:auto_selected(cfg)
            except ValueError as exc:assert '산출 가능한 밴드가 없습니다' in str(exc)
            else:raise AssertionError('all-bands-failed decision must stop')
        assert list(root.parent.glob('Easy_ELC_*'))==[root]
        print('PASS: saturation + registration excluded; 7/10 bands, 14 float TIFFs; no-fit blocks; source/ROI unchanged.')


if __name__=='__main__':
    run()
