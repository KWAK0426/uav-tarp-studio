import csv
import json
from pathlib import Path
import shutil
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import distribution as d
d.configure()
from uav_pipeline import calibration_presets as presets
from uav_pipeline.config import load_config, save_config


class PresetTests(unittest.TestCase):
    def test_small_set_and_additional_tarp_instances(self):
        from uav_pipeline.elc_fit import _library, _library_key
        base=['PNU_s_3','PNU_s_36','PNU_s_56']
        self.assertEqual(presets.expand_tarp_names('PNU_s'),base)
        expanded=presets.expand_tarp_names('PNU_s, PNU_s_56, PNU_s_56')
        self.assertEqual(expanded,base+['PNU_s_56__2','PNU_s_56__3'])
        self.assertEqual(presets.expand_tarp_names(expanded),expanded)
        self.assertEqual(presets.expand_tarp_names('PNU_s_56'),['PNU_s_56'])
        library=_library(Path(presets.preset_path('tarp')))
        self.assertEqual(library[_library_key(expanded[-1])],library['PNU_s_56'])
        self.assertEqual(len({_library_key(n) for n in expanded}),3)
        with self.assertRaises(ValueError): presets.expand_tarp_names('NIAS_20,NIAS_20')

    def test_map_expands_set_without_rewriting_saved_inputs(self):
        from uav_pipeline.general_tarp_review import map_basis
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); mosaic=root/'mosaic.tif';mosaic.write_bytes(b'fixture')
            photos=root/'photos';photos.mkdir();(photos/'sample.tif').write_bytes(b'fixture')
            cfg={'tarp_workflow':{'reference_mosaic':str(mosaic),'photos_override':str(photos),'tarp_names':'PNU_s, PNU_s_56'},
                 'preparation':{'tarp_reflectance_csv':presets.preset_path('tarp')}}
            with patch('uav_pipeline.general_tarp_review.source_signature',return_value={}):
                self.assertEqual(map_basis(cfg)['tarps'],['PNU_s_3','PNU_s_36','PNU_s_56','PNU_s_56__2'])
            self.assertEqual(cfg['tarp_workflow']['tarp_names'],'PNU_s, PNU_s_56')

    def test_duplicate_white_is_not_two_validation_types(self):
        from uav_pipeline.general_tarp_review import raw_tarp_scope
        names=['PNU_s_3','PNU_s_36','PNU_s_56','PNU_s_56__2']
        cfg=d.preparation_template()
        rows=[]
        for shot in ('reference','validation'):
            chosen=names if shot=='reference' else names[-2:]
            for name in cfg['band_names'].values():
                wave=float(name.rsplit('_',1)[-1][:-2])
                rows.append(dict(shot=shot,wavelength_nm=wave,source_path='fixture',native_image_size=[2,2],
                    native_rois={n:[] for n in chosen},native_outer_rois={n:[] for n in chosen}))
        with patch('uav_pipeline.elc_priority.approved_shots',return_value=('unused',{'transforms_json':'fixture'},'reference',['reference','validation'])), \
             patch('uav_pipeline.general_tarp_review.read_json',return_value=rows), \
             patch('uav_pipeline.roi_studio_bridge.is_fit_roi',return_value=True), \
             patch('uav_pipeline.tarp_tool.code.saturation_qc.scan_roi',return_value={'saturated_pct':0}):
            with self.assertRaisesRegex(ValueError,'독립 검증 타프 1종'):
                raw_tarp_scope(cfg)

    def test_values_and_portable_resolution(self):
        with tempfile.TemporaryDirectory() as folder:
            project = d.create_project(folder, '2027-06-10', 'flight1', 'Test')
            cfg = load_config(project)
            with Path(cfg['preparation']['gain_csv']).open(encoding='utf-8-sig') as f:
                gains = {int(r['band']): float(r['gain']) for r in csv.DictReader(f)}
            self.assertEqual(list(gains.values()), [1.10,1.17,1.11,1.07,1.14,1.06,1.18,1.08,1.03,1.11])
            moved = Path(folder) / 'another_pc' / 'defaults'
            moved.mkdir(parents=True)
            for asset in presets.PROFILES.values():
                shutil.copy2(presets.DEFAULTS / asset['file'], moved / asset['file'])
            with patch.object(presets, 'DEFAULTS', moved):
                relocated = load_config(project)
                self.assertEqual(Path(relocated['preparation']['gain_csv']).parent, moved)
                self.assertEqual(Path(relocated['preparation']['tarp_reflectance_csv']).parent, moved)
                (moved / presets.PROFILES['gain']['file']).write_text('band,gain\n1,99\n')
                with self.assertRaisesRegex(ValueError, '내장 교정'):
                    load_config(project)

    def test_combined_and_legacy_tarp_libraries(self):
        with Path(presets.preset_path('tarp')).open(encoding='utf-8-sig') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual({r['key'] for r in rows}, {'PNU_03', 'PNU_36', 'PNU_56', 'PNU_s_3', 'PNU_s_36', 'PNU_s_56', 'NIAS_2.5', 'NIAS_20', 'NIAS_30', 'NIAS_50'})
        cfg = {'preparation': {'tarp_profile': presets.LEGACY_TARP['id']}}
        presets.resolve(cfg)
        with Path(cfg['preparation']['tarp_reflectance_csv']).open(encoding='utf-8-sig') as stream:
            legacy = list(csv.DictReader(stream))
        for old, key in zip(legacy, ('PNU_s_3', 'PNU_s_36', 'PNU_s_56')):
            current = next(r for r in rows if r['key'] == key)
            for band in range(1,11):
                self.assertEqual(old[f'B{band:02d}'], current[f'B{band:02d}'])
        self.assertEqual([r['key'] for r in legacy], ['1m_Black', '1m_Gray', '1m_White'])

    def test_external_calibration_is_not_replaced(self):
        cfg = {'preparation': {'gain_profile': 'custom', 'gain_csv': 'my_gain.csv',
                               'tarp_profile': 'custom', 'tarp_reflectance_csv': 'my_tarp.csv'}}
        presets.resolve(cfg)
        self.assertEqual(cfg['preparation']['gain_csv'], 'my_gain.csv')
        self.assertEqual(cfg['preparation']['tarp_reflectance_csv'], 'my_tarp.csv')

    def test_gui_gain_locked_editable_and_restorable(self):
        from general_studio import GeneralStudio, destroy_owned_root
        with tempfile.TemporaryDirectory() as folder:
            project = d.create_project(folder, '2027-06-10', 'flight1', 'Test')
            root = tk.Tk(); root.withdraw()
            try:
                with patch('tkinter.messagebox.showerror') as errors:
                    app = GeneralStudio(root, project)
                    original = presets.preset_path('gain')
                    for entry, button in app.calibration_controls['preparation.gain_csv']:
                        self.assertTrue(entry.instate(('readonly',)))
                        self.assertTrue(button.instate(('disabled',)))
                    custom = Path(folder) / 'custom.csv'
                    custom.write_text('band,gain\n' + ''.join(f'{b},1.0\n' for b in range(1,11)))
                    # A programmatic path change cannot bypass the locked UI.
                    app._var('preparation.gain_csv').set(str(custom))
                    self.assertTrue(app.save(quiet=True))
                    self.assertEqual(load_config(project)['preparation']['gain_csv'], original)
                    app.gain_edit.set(True); app._update_calibration_controls()
                    app._var('preparation.gain_csv').set(str(custom))
                    self.assertTrue(app.save(quiet=True))
                    self.assertEqual(load_config(project)['preparation']['gain_profile'], 'custom')
                    app.load(project)
                    self.assertFalse(app.gain_edit.get())
                    app._restore_gain()
                    self.assertTrue(app.save(quiet=True))
                    self.assertEqual(load_config(project)['preparation']['gain_csv'], original)
                    for profile in (presets.LEGACY_TARP, presets.PROFILES['tarp']):
                        app.tarp_choice.set(profile['label']); app._choose_tarp_preset()
                        self.assertTrue(app.save(quiet=True))
                        self.assertTrue(app.load(project))
                        self.assertEqual(app.tarp_choice.get(), profile['label'])
                        self.assertEqual(Path(app.cfg['preparation']['tarp_reflectance_csv']).name, profile['file'])
                    app.tarp_choice.set('사용자 CSV'); app._choose_tarp_preset()
                    self.assertTrue(all(not b.instate(('disabled',)) for e,b in app.calibration_controls['preparation.tarp_reflectance_csv']))
                    self.assertFalse(errors.called, errors.call_args)
            finally:
                destroy_owned_root(root)

if __name__ == '__main__':
    unittest.main(verbosity=2)
