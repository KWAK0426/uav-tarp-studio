import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import distribution
distribution.configure()
from uav_pipeline import completed_result as results


class CompletedResultTests(unittest.TestCase):
    def test_connect_and_recheck_never_changes_project(self):
        with tempfile.TemporaryDirectory() as folder:
            project=Path(folder)/'project.json';project.write_text('{}')
            cfg={'_config_path':str(project)};before=copy.deepcopy(cfg)
            record={'run':folder,'files':{'result.tif':'hash'},'scientific_approval':False}
            with patch.object(results,'inspect',return_value=record):
                results.connect(cfg,folder)
                self.assertEqual(results.load(cfg),record)
            with patch.object(results,'inspect',return_value={**record,'files':{'result.tif':'changed'}}):
                with self.assertRaisesRegex(ValueError,'바뀌었습니다'):results.load(cfg)
            self.assertEqual(cfg,before);self.assertEqual(project.read_text(),'{}')

    def test_changed_basis_is_rejected(self):
        cfg={'preparation':{'gain_csv':'new-gain','tarp_reflectance_csv':'new-tarp'}}
        saved={'preparation':{'gain_csv':'old-gain','tarp_reflectance_csv':'old-tarp'}}
        with patch.object(results,'digest',return_value='same'),patch.object(results,'input_signature',return_value='same'):
            matched=results.matching_basis(cfg,saved)
            self.assertEqual(matched,saved);self.assertNotEqual(cfg,saved)
        with patch.object(results,'digest',side_effect=['new','old']):
            with self.assertRaisesRegex(ValueError,'교정 자료'):results.matching_basis(cfg,saved)
        with patch.object(results,'digest',return_value='same'),patch.object(results,'input_signature',side_effect=['new','old']):
            with self.assertRaisesRegex(ValueError,'날짜'):results.matching_basis(cfg,saved)

    def test_no_partial_result_implies_full_approval(self):
        result=dict(shots=7,bands=[4,6],excluded={'1':'포화'},reflectance_files=14,summary=[{'rmse_pp':1.2},{'rmse_pp':2.0}])
        text=results.label(result,{'1':'444nm','4':'560nm','6':'668nm'})
        self.assertIn('7장 × 2밴드 = 14개',text)
        self.assertIn('전체 촬영분 처리는 별도',text)


if __name__=='__main__':unittest.main()
