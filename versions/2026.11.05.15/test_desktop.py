"""Portable startup/update trust-boundary checks; no network or research data."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import studio_update as updates


def manifest(folder, names):
    value={'files':[{'path':name,'bytes':(folder/name).stat().st_size,
        'sha256':hashlib.sha256((folder/name).read_bytes()).hexdigest()} for name in names]}
    (folder/'PACKAGE_MANIFEST.json').write_text(json.dumps(value),encoding='utf-8')


class PortableTests(unittest.TestCase):
    def test_native_tarp_qc_popup_is_packaged(self):
        import distribution
        distribution.configure()
        from uav_pipeline.tarp_native_qc_ui import NativeTarpQcPopup
        self.assertTrue(callable(NativeTarpQcPopup.run))

    def test_native_proposals_require_review_and_preserve_manual_regions(self):
        import tkinter as tk
        import distribution
        distribution.configure()
        from PIL import Image
        from uav_pipeline import tarp_native_qc_ui as ui
        root=tk.Tk();root.withdraw()
        try:
            with patch.object(ui,'_async'):
                window=ui.NativeTarpQcPopup(root,'http://127.0.0.1:1','IMG_0001_6','IMG_0001',['PNU_s_3','PNU_s_36'])
                a=[[.1,.1],[.3,.1],[.3,.3],[.1,.3]]
                b=[[.5,.5],[.7,.5],[.7,.7],[.5,.7]]
                window.view.set_image(Image.new('RGB',(100,100)))
                window.proposals=[a,b];window.reviewed.set(False)
                window.clicked(20,20);window.active.set('PNU_s_36');window.clicked(20,20)
                self.assertNotIn('PNU_s_36',window.regions)
                window.clicked(60,60);window.refresh()
                self.assertTrue(window.run_button.instate(['disabled']))
                window.reviewed.set(True);window.refresh()
                self.assertFalse(window.run_button.instate(['disabled']))
                before=json.dumps(window.regions)
                window.propose()
                self.assertEqual(before,json.dumps(window.regions))
        finally:
            from desktop import destroy_owned_root
            root.update_idletasks()
            destroy_owned_root(root)

    def test_zip_traversal_and_duplicate_case_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for index,name in enumerate(('../outside','C:/outside','..\\outside','/outside','folder./file')):
                archive=root/f'{index}.zip'
                with zipfile.ZipFile(archive,'w') as out: out.writestr(name,'blocked')
                with self.assertRaises(ValueError): updates.safe_extract(archive,root/f'd{index}')
            archive=root/'dup.zip'
            with zipfile.ZipFile(archive,'w') as out:
                out.writestr('A.py','a'); out.writestr('a.py','b')
            with self.assertRaises(ValueError): updates.safe_extract(archive,root/'dup')
            self.assertFalse((root.parent/'outside').exists())

    def test_manifest_rejects_corruption_extra_and_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'app.py').write_text('pass')
            manifest(root,['app.py']); updates.verify_package(root)
            (root/'research.csv').write_text('private')
            with self.assertRaises(ValueError): updates.verify_package(root)
            (root/'research.csv').unlink()
            (root/'app.py').write_text('evil')
            with self.assertRaises(ValueError): updates.verify_package(root)
            manifest(root,['app.py'])
            value=json.loads((root/'PACKAGE_MANIFEST.json').read_text())
            value['files'][0]['path']='../app.py'
            (root/'PACKAGE_MANIFEST.json').write_text(json.dumps(value))
            with self.assertRaises(ValueError): updates.verify_package(root)

    def test_activation_preserves_projects_and_previous_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for version in ('2026.11.03.1','2026.11.03.2'):
                folder=root/'versions'/version; folder.mkdir(parents=True)
                (folder/'app.py').write_text(version); manifest(folder,['app.py'])
            updates.write_json(root/'settings/active.json',{'version':'2026.11.03.1'})
            updates.write_json(root/'settings/workspace/project.json',{'approved':False})
            before=(root/'settings/workspace/project.json').read_bytes()
            with patch.object(updates,'ROOT',root): updates.activate('2026.11.03.2')
            self.assertEqual(json.loads((root/'settings/active.json').read_text())['version'],'2026.11.03.2')
            self.assertEqual(json.loads((root/'updates/backups/previous.json').read_text())['version'],'2026.11.03.1')
            self.assertEqual((root/'settings/workspace/project.json').read_bytes(),before)

    def test_release_digest_and_origin_required(self):
        release={'tag_name':'v2026.11.03.2','assets':[{'name':'studio-update.zip','size':5,
            'browser_download_url':'https://github.com/KWAK0426/uav-tarp-studio/releases/download/v2026.11.03.2/studio-update.zip',
            'digest':'sha256:'+'a'*64}],'html_url':'https://github.com/KWAK0426/uav-tarp-studio/releases'}
        with patch.object(updates,'get_json',return_value=release), patch.object(updates,'channel',return_value={
                'repository':'KWAK0426/uav-tarp-studio','asset':'studio-update.zip','version':'2026.11.03.1'}):
            self.assertTrue(updates.check_update()['newer'])
            release['assets'][0]['digest']=None
            with self.assertRaises(ValueError): updates.check_update()
            release['assets'][0]['digest']='sha256:'+'a'*64
            release['assets'][0]['browser_download_url']='https://example.com/unsafe.zip'
            with self.assertRaises(ValueError): updates.check_update()

    def test_raw_date_uses_exif_and_duplicate_is_not_chosen(self):
        import desktop
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for shot in ('IMG_0001','IMG_0002'):
                for band in range(1,11): (root/f'{shot}_{band}.tif').write_bytes(b'fixture')
            with patch('exiftool.ExifToolHelper') as helper:
                helper.return_value.__enter__.return_value.get_metadata.return_value=[
                    {'EXIF:DateTimeOriginal':'2031:01:01 16:01:00'},
                    {'EXIF:DateTimeOriginal':'2031:01:01 16:02:00'}]
                report=desktop.inspect_input(str(root),'UTC')
                self.assertEqual(report['date'],'2031-01-02')
                self.assertEqual(len(report['complete']),2)
                with self.assertRaises(ValueError): desktop.create_from_raw(report,str(root/'results'),'Site','Session01',report['date'])
            (root/'duplicate').mkdir(); (root/'duplicate/IMG_0001_1.tif').write_bytes(b'fixture')
            self.assertTrue(desktop.inspect_input(str(root))['duplicates'])

    def test_inventory_guidance_distinguishes_panel_overlap_and_other_duplicates(self):
        import desktop
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = dict(complete=['IMG_0002'], incomplete=[], duplicates=[
                ('IMG_0001', 1, [root/'01_Observation/000/IMG_0001_1.tif',
                                  root/'02_Panel_After/000/IMG_0001_1.tif'])])
            text = desktop.inventory_guidance(report, root)
            self.assertIn('01_Observation', text)
            self.assertIn('패널 영상', text)
            report['duplicates'] = [('IMG_0001', 1, [root/'a/IMG_0001_1.tif', root/'b/IMG_0001_1.tif'])]
            self.assertNotIn('패널 영상', desktop.inventory_guidance(report, root))
            report['duplicates'] = []
            report['incomplete'] = [('IMG_0003', [10])]
            self.assertIn('대상에서 제외', desktop.inventory_guidance(report, root))

    def test_scan_warning_once_and_details_toggle(self):
        import desktop
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            app = desktop.StartWindow(root)
            app.raw.set('raw')
            report = dict(report='long paths', summary='원인과 조치', complete=[], duplicates=True, date='', incomplete_count=0)
            app.events.put(('raw', app.tz.get(), report, None))
            with patch.object(desktop.messagebox, 'showwarning') as warning:
                app.poll()
                app.poll()
                warning.assert_called_once()
                self.assertIn('원인과 조치', app.details.get('1.0', 'end'))
                self.assertTrue(app.start_button.instate(['disabled']))
                app.toggle_scan_details()
                self.assertIn('long paths', app.details.get('1.0', 'end'))
                app.events.put(('previous-folder', app.tz.get(), report, None))
                app.poll()
                warning.assert_called_once()
        finally:
            desktop.destroy_owned_root(root)

    def test_folder_date_and_manual_override_keep_exif_gate(self):
        import desktop
        import tkinter as tk
        self.assertEqual(desktop.folder_date('data/2026-09-08/01_260908_Multispectral'), '2026-09-08')
        self.assertEqual(desktop.folder_date('data/20260908/Observation'), '2026-09-08')
        self.assertEqual(desktop.folder_date('data/260908/Observation'), '2026-09-08')
        self.assertEqual(desktop.folder_date('data/2026-02-30/Observation'), '')
        self.assertEqual(desktop.folder_date('data/2026-09-08/2026-09-09'), '')
        root = tk.Tk(); root.withdraw()
        try:
            app = desktop.StartWindow(root)
            app.raw.set('data/2026-09-08/Observation')
            self.assertEqual(app.date.get(), '2026-09-08')
            self.assertTrue(app.date_entry.instate(['readonly']))
            app.report = {'date': '2026-09-09'}
            app.refresh_date()
            self.assertEqual(app.date.get(), '2026-09-09')
            self.assertIn('폴더 날짜와 달라', app.date_hint.get())
            app.auto_date.set(False); app.refresh_date()
            app.date.set('2026-09-10'); app.refresh_date()
            self.assertEqual(app.date.get(), '2026-09-10')
            self.assertFalse(app.date_entry.instate(['readonly']))
            app.raw.set('data/undated/Observation')
            self.assertEqual(app.date.get(), '2026-09-10')
            app.auto_date.set(True); app.refresh_date()
            self.assertEqual(app.date.get(), '')
            with tempfile.TemporaryDirectory() as temp:
                report = dict(raw=str(Path(temp)/'raw'), complete=['IMG_0001'], duplicates=False, date='2026-09-09')
                with self.assertRaisesRegex(ValueError, 'EXIF'):
                    desktop.create_from_raw(report, str(Path(temp)/'output'), 'Site', 'Session01', '2026-09-08')
                self.assertFalse((Path(temp)/'output').exists())
        finally:
            desktop.destroy_owned_root(root)

    def test_ai_only_offers_registered_elc_actions(self):
        import distribution
        distribution.configure()
        from uav_pipeline import assistant as ai
        reply, actions=ai.parse_proposals(json.dumps({'reply':'ok','actions':[
            {'type':'run_stage','stage':'shell'}, {'type':'run_stage','stage':'brdf'},
            {'type':'run_stage','stage':'prepare_elc'}]}),[])
        self.assertEqual(actions,[{'type':'run_stage','stage':'prepare_elc'}])


if __name__=='__main__': unittest.main(verbosity=2)
