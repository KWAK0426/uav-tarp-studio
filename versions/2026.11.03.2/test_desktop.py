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

    def test_ai_only_offers_registered_elc_actions(self):
        import distribution
        distribution.configure()
        from uav_pipeline import assistant as ai
        reply, actions=ai.parse_proposals(json.dumps({'reply':'ok','actions':[
            {'type':'run_stage','stage':'shell'}, {'type':'run_stage','stage':'brdf'},
            {'type':'run_stage','stage':'prepare_elc'}]}),[])
        self.assertEqual(actions,[{'type':'run_stage','stage':'prepare_elc'}])


if __name__=='__main__': unittest.main(verbosity=2)
