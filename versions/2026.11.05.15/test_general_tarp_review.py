"""Synthetic map coverage and stale/full-processing gate checks."""
import csv
import json
from pathlib import Path
import tempfile
import os
import signal
import time
import unittest

import distribution as d
d.configure()
from uav_pipeline import general_project as general
from uav_pipeline.general_tarp_review import map_context, require_location, require_full_approval
from uav_pipeline.tarp_tool.code import location_review


def reviewed_candidates(cfg, shots, reference_shot=''):
    """Fixture only: use the actual review proof writer, never bypass its consumer."""
    from osgeo import gdal, osr
    root = general.project_root(cfg)
    mosaic = root / 'synthetic_mosaic.tif'
    photos = root / 'synthetic_photos'; photos.mkdir(exist_ok=True)
    with Path(cfg['preparation']['tarp_reflectance_csv']).open(encoding='utf-8-sig') as f:
        names = [r['key'] for r in csv.DictReader(f)]
    if not mosaic.exists():
        ds = gdal.GetDriverByName('GTiff').Create(str(mosaic), 100, 100, 1, gdal.GDT_UInt16)
        ds.SetGeoTransform((500000, .1, 0, 3900000, 0, -.1))
        crs = osr.SpatialReference(); crs.ImportFromEPSG(32652); ds.SetProjection(crs.ExportToWkt())
        ds.GetRasterBand(1).Fill(1000); ds = None
    for shot in general.source_shots(cfg):
        target = photos / (shot+'_6.tif')
        if not target.exists():
            ds = gdal.Translate(str(target), str(mosaic)); ds = None
    cfg.setdefault('tarp_workflow', {}).update(reference_mosaic=str(mosaic), photos_override=str(photos), tarp_names=names)
    path = general.save_candidates(cfg, shots, reference_shot)
    if not shots:
        return path
    context = map_context(cfg)
    context.update(identity=path.parent.parent.name, output=str(path.parent.parent))
    (path.parent.parent/'context.json').write_text(json.dumps(context), encoding='utf-8')
    selection = json.loads(path.read_text())
    selection['regions'] = {n:[[.3,.3],[.4,.3],[.4,.4],[.3,.4]] for n in names[:2]}
    review = location_review.begin(context, selection)
    location_review.confirm(context, selection, review, path)
    return path


class MapGateTests(unittest.TestCase):
    def test_real_map_server_filters_nonoverlap_and_confirms(self):
        import numpy as np
        import tifffile
        from osgeo import gdal
        from uav_pipeline.config import load_config
        from uav_pipeline.tarp_workflow import launch
        from uav_pipeline.tarp_popup import LocalApi
        with tempfile.TemporaryDirectory() as folder:
            cfg = load_config(d.create_project(folder, '2030-01-01', 'flight1', 'Test'))
            root = general.project_root(cfg); raw = root/'raw'; raw.mkdir()
            shots = ['IMG_0001', 'IMG_0002', 'IMG_0003']
            for shot in shots:
                for band, wave in enumerate((475,560,668,842,717,444,531,650,705,740),1):
                    xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                           f'<rdf:Description xmlns:Camera="http://micasense.com/Camera/1.0/" Camera:CentralWavelength="{wave}"/>'
                           '</rdf:RDF></x:xmpmeta>').encode()
                    tifffile.imwrite(raw/f'{shot}_{band}.tif', np.ones((20,20),np.uint16)*1000,
                                     extratags=[(700,'B',len(xmp),xmp,False)])
            cfg['preparation']['raw_directory'] = str(raw)
            general.save_shot_list(cfg, shots)
            reviewed_candidates(cfg, shots)
            photo = Path(cfg['tarp_workflow']['photos_override'])/'IMG_0003_6.tif'
            ds = gdal.Open(str(photo), gdal.GA_Update)
            ds.SetGeoTransform((501000,.1,0,3900000,0,-.1)); ds = None
            context = map_context(cfg)
            api = LocalApi(launch(cfg))
            pid = json.loads((Path(context['output'])/'server.json').read_text())['pid']
            try:
                regions = {cfg['tarp_workflow']['tarp_names'][i]: [[x,.3],[x+.1,.3],[x+.1,.4],[x,.4]]
                           for i,x in enumerate((.3,.6))}
                api.post('/search', {'regions':regions})
                deadline = time.monotonic()+20
                while time.monotonic()<deadline:
                    state = api.get('/state')
                    if not state['running']: break
                    time.sleep(.1)
                self.assertFalse(state['running'])
                self.assertFalse(state['errors'], state['errors'])
                self.assertEqual({r['shot'] for r in state['candidates']}, {'IMG_0001','IMG_0002'})
                self.assertTrue(all(r['fit_coverage_complete'] for r in state['candidates']))
                selected = state['candidates'][0]
                result = api.post('/save', {'ids':[selected['id']], 'regions':regions, 'method':'manual_centers'})
                require_location(cfg, result['saved'])
                from uav_pipeline.preparation import latest_candidate_selection
                self.assertEqual(latest_candidate_selection(cfg)[2], {selected['shot']})
            finally:
                os.kill(pid, signal.SIGTERM)
                time.sleep(.2)

    def test_manual_candidates_cannot_bypass_location_review(self):
        with tempfile.TemporaryDirectory() as folder:
            from uav_pipeline.config import load_config
            cfg = load_config(d.create_project(folder, '2030-01-01', 'flight1', 'Test'))
            root = general.project_root(cfg); raw = root/'raw'; raw.mkdir()
            for band in range(1,11): (raw/f'IMG_0001_{band}.tif').write_bytes(b'synthetic')
            cfg['preparation']['raw_directory'] = str(raw)
            general.save_shot_list(cfg, ['IMG_0001'])
            manual = general.save_candidates(cfg, ['IMG_0001'])
            from uav_pipeline.preparation import latest_candidate_selection
            with self.assertRaisesRegex(ValueError, '02'):
                latest_candidate_selection(cfg)
            path = reviewed_candidates(cfg, ['IMG_0001'])
            require_location(cfg, path)
            self.assertEqual(latest_candidate_selection(cfg)[2], {'IMG_0001'})
            with self.assertRaisesRegex(ValueError, '전체 처리 잠금'):
                require_full_approval(cfg)
            context = json.loads((path.parent.parent/'context.json').read_text())
            location_review.begin(context)
            with self.assertRaisesRegex(ValueError, '02'):
                require_location(cfg, path)

    def test_photos_change_invalidates_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            from uav_pipeline.config import load_config
            cfg = load_config(d.create_project(folder, '2030-01-01', 'flight1', 'Test'))
            root = general.project_root(cfg); raw = root/'raw'; raw.mkdir()
            for band in range(1,11): (raw/f'IMG_0001_{band}.tif').write_bytes(b'synthetic')
            cfg['preparation']['raw_directory'] = str(raw)
            general.save_shot_list(cfg, ['IMG_0001'])
            path = reviewed_candidates(cfg, ['IMG_0001'])
            photo = Path(cfg['tarp_workflow']['photos_override'])/'IMG_0001_6.tif'
            photo.touch()
            with self.assertRaisesRegex(ValueError, 'Photos'):
                require_location(cfg, path)


if __name__ == '__main__':
    unittest.main(verbosity=2)
