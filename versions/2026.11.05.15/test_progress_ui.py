import unittest
from unittest.mock import Mock,patch
import tkinter as tk
import distribution
distribution.configure()
from uav_pipeline.progress_ui import begin,update,finish,count_from_text,remaining_seconds,center_window


class ProgressTests(unittest.TestCase):
    def test_exclusion_uses_public_context_and_request_coordinates(self):
        import io,json,copy
        from pathlib import Path
        from types import SimpleNamespace
        from uav_pipeline import roi_studio_bridge as bridge
        session=bridge.Session.__new__(bridge.Session)
        session.shots=['IMG_0001','IMG_0002','IMG_0003'];session.reference='IMG_0001'
        session.images={s:[{'reviewed':True,'pixel_reviewed':True,'role':'calibration' if s==session.reference else 'validation'}] for s in session.shots}
        expected=copy.deepcopy({s:r for s,r in session.images.items() if s!='IMG_0002'})
        keys=('identity','label','mosaic','mosaic_role','tarps','source_kind','candidate_preview_kind','output','reference_timing','code_revision')
        public={k:k for k in keys};session.context=dict(public,raw='private raw registry',photos_signature='signature')
        session.selection=Path('selected.json');session.draft_path=Path('draft.json');session.folder=Path('test-output');session.config_path=Path('project.json')
        session.signatures={s:[s,1,2] for s in session.shots};session.assert_current=Mock()
        selection={'selected':[{'id':i,'shot':s} for i,s in enumerate(session.shots)],
                   'locations':[{'id':'A','point':{'u':.3,'v':.4},'radius_m':2,'tarps':['black','gray']}]}
        def read(path):
            return selection if path==session.selection else {'url':'http://127.0.0.1:12345'} if Path(path).name=='server.json' else {'images':session.images}
        updated=SimpleNamespace(signatures={s:session.signatures[s] for s in expected},selection=Path('new.json'),save_draft=Mock())
        with patch.object(bridge,'read_json',side_effect=read),patch.object(bridge,'atomic_json') as audit,patch.object(bridge,'Session',return_value=updated),patch('urllib.request.urlopen') as request:
            wrong=dict(public,identity='other flight')
            request.return_value=io.BytesIO(json.dumps(wrong).encode())
            with self.assertRaisesRegex(ValueError,'연결이 변경'):session.exclude_rejected({'IMG_0002'},reason='registration failed')
            self.assertEqual(request.call_count,1);audit.assert_not_called()
            request.reset_mock();request.side_effect=[io.BytesIO(json.dumps(public).encode()),io.BytesIO(b'{}')]
            session.exclude_rejected({'IMG_0002'},reason='registration failed')
            payload=json.loads(request.call_args_list[1].args[0].data)
            self.assertEqual(payload['ids'],[0,2])
            self.assertEqual(payload['locations'],[{'u':.3,'v':.4,'radius':2,'tarps':['black','gray']}])
            self.assertEqual(session.images,expected)
            updated.save_draft.assert_called_once()
            self.assertEqual(audit.call_args.args[1]['excluded_shots'],['IMG_0002'])

    def test_center_geometry(self):
        window=Mock();parent=Mock()
        center_window(window,parent,460,210,'1100x900+2040+40')
        window.geometry.assert_called_once_with('460x210+2360+385')

    def test_nested_popup_centers_on_studio_and_keeps_manual_move(self):
        import tkinter as tk
        from uav_pipeline.progress_ui import center_studio_popups
        root=tk.Tk();root.geometry('600x420+100+100')
        try:
            center_studio_popups(root);root.update()
            outer=tk.Toplevel(root);outer.geometry('400x240+20+20');root.update()
            child=tk.Toplevel(outer);child.geometry('220x120+30+30');root.update()
            self.assertLessEqual(abs(child.winfo_rootx()+child.winfo_width()/2-root.winfo_rootx()-root.winfo_width()/2),2)
            # Windows decoration height is separate from client geometry.
            self.assertLessEqual(abs(child.winfo_rooty()+child.winfo_height()/2-root.winfo_rooty()-root.winfo_height()/2),40)
            child.geometry('+70+80');root.update();before=child.geometry()
            child.withdraw();child.deiconify();root.update()
            self.assertEqual(child.geometry(),before)
        finally: root.destroy()

    def test_remaining_time_uses_observed_work_only(self):
        self.assertIsNone(remaining_seconds(1,100,0,20))
        self.assertIsNone(remaining_seconds(10,100,0,1))
        self.assertEqual(remaining_seconds(20,100,10,10),80)
        self.assertEqual(remaining_seconds(100,100,0,20),0)
        self.assertGreater(remaining_seconds(20,100,10,30),remaining_seconds(20,100,10,10))

    def test_registration_exclusion_guards(self):
        from uav_pipeline.roi_studio_bridge import Session
        session=Session.__new__(Session)
        session.shots=['IMG_0001','IMG_0002','IMG_0003'];session.reference='IMG_0001'
        session.images={s:[{'reviewed':True,'pixel_reviewed':True}] for s in session.shots}
        self.assertEqual(session.registration_exclusion_reason('IMG_0002'),'')
        self.assertTrue(session.registration_exclusion_reason('IMG_0001'))
        self.assertTrue(session.registration_exclusion_reason('IMG_9999'))
        session.images['IMG_0003'][0]['pixel_reviewed']=False
        self.assertTrue(session.registration_exclusion_reason('IMG_0002'))
        session.shots.pop()
        self.assertTrue(session.registration_exclusion_reason('IMG_0002'))

    def test_real_counts_and_unknown_work(self):
        self.assertEqual(count_from_text('밴드 정합 37/150 완료 · IMG_0209 원본 밴드 4'),(37,150))
        self.assertIsNone(count_from_text('준비 중'))
        self.assertIsNone(count_from_text('0/0'))
        self.assertIsNone(count_from_text('16/15'))

    def test_single_window_progress_reset_and_cleanup(self):
        root=tk.Tk();root.withdraw()
        try:
            first=begin(root,'처리 표시 테스트',anchor='1100x900+2040+40')
            first.update_idletasks()
            self.assertIs(first,begin(root,'다음 처리'))
            update(root,'밴드 정합',37,150)
            self.assertIn('24.7%',first.percent.get())
            self.assertEqual(float(first.bar['value']),37)
            self.assertEqual(first.phase_done,37)
            update(root,'밴드 정합',39,150)
            self.assertEqual(first.phase_done,37)
            update(root,'다음 단계',0,150)
            self.assertEqual(first.phase_done,0)
            update(root,'결과 저장 중')
            self.assertIsNone(first.counts)
            self.assertEqual(first.percent.get(),'진행 중')
            self.assertEqual(str(first.bar['mode']),'indeterminate')
            finish(root);finish(root)
            self.assertIsNone(root._processing_popup)
            self.assertIsNone(first.timer)
            self.assertFalse(first.winfo_exists())
        finally:root.destroy()


if __name__=='__main__':unittest.main()
