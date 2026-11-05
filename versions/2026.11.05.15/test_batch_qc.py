import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch,Mock
import distribution
distribution.configure()
from uav_pipeline import tarp_native_qc_ui as ui
from uav_pipeline.tarp_batch_qc import action,signature


class BatchTests(unittest.TestCase):
    def test_nearby_reference_fallback_keeps_named_geometry_and_quality_gate(self):
        import numpy as np
        from uav_pipeline import roi_auto_prepare as auto
        regions={name:[[x,.1],[x+.1,.1],[x+.1,.2],[x,.2]] for name,x in [('black',.1),('gray',.4),('white',.7)]}
        refs=[dict(shot=shot,image=shot,regions=regions,approved=True,names_require_review=False) for shot in ('IMG_0208','IMG_0226','IMG_0241')]
        with patch.object(auto,'track',side_effect=[({'black':regions['black']},{'reason':'partial'}),(regions,{'reason':'passed'})]) as call:
            found,diagnostics,unconfirmed_names=auto.track_references(refs,np.zeros((100,100)),'path','IMG_0225')
        self.assertEqual([c.args[0] for c in call.call_args_list],['IMG_0226','IMG_0241'])
        self.assertEqual(found,regions);self.assertFalse(unconfirmed_names)
        self.assertEqual(diagnostics['reference_shot'],'IMG_0241')
        self.assertEqual(len(diagnostics['attempts']),2)
        with patch.object(auto,'track',return_value=({'black':regions['black']},{})):
            found,diagnostics,_=auto.track_references(refs,np.zeros((100,100)),'path','IMG_0225')
        self.assertEqual(found,{})
        self.assertEqual(len(diagnostics['attempts']),3)

    def test_two_reference_consensus_requires_independent_close_nonoverlapping_quads(self):
        import numpy as np
        from uav_pipeline.roi_auto_prepare import consensus_regions
        a=[[.1,.1],[.2,.1],[.2,.2],[.1,.2]]
        b=(np.asarray(a)+[.01,0]).tolist()
        self.assertTrue(consensus_regions({'black':[('A',a),('B',b)]},['black'],(100,100))[0])
        self.assertFalse(consensus_regions({'black':[('A',a),('A',b)]},['black'],(100,100))[0])
        far=(np.asarray(a)+[.04,0]).tolist()
        self.assertFalse(consensus_regions({'black':[('A',a),('B',far)]},['black'],(100,100))[0])
        same={'black':[('A',a),('B',b)],'white':[('A',a),('B',b)]}
        self.assertFalse(consensus_regions(same,['black','white'],(100,100))[0])

    def test_confirmed_selection_runs_existing_detection_qc_and_trim_import_without_approval(self):
        import numpy as np
        import sys
        from uav_pipeline import roi_auto_prepare as auto
        sys.path.insert(0,str(Path(auto.__file__).parent/'tarp_tool/code'))
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);rows=[];signatures={}
            for shot in ('A','B'):
                paths=[]
                for band in range(1,11):
                    path=folder/f'{shot}_{band}.tif';path.write_bytes(b'fixture');paths.append(str(path))
                    signatures[f'{shot}_{band}']=signature([path])[0]
                rows.append(dict(id=shot,shot=shot,source_files=paths))
            selection=folder/'selected.json';selection.write_text(json.dumps(dict(selected=rows)))
            context=folder/'context.json';context.write_text('{}')
            session=Mock(unsafe=True)
            session.context=dict(tarps=['black','gray'],output=str(folder));session.context_path=context
            session.selection=selection;session.selection_hash=auto.digest(selection);session.signatures=signatures
            session.library_hash='library';session.draft_path=folder/'draft.json';session.images={};session.shots=['A','B']
            regions={'black':[[.1,.1],[.2,.1],[.2,.2],[.1,.2]],'gray':[[.6,.6],[.8,.6],[.8,.8],[.6,.8]]}
            def inspect(paths,polygons,names,cache):
                return dict(regions=polygons,source_signature=signature(paths),summary='test',rows=[dict(wavelength_nm=560,usable_tarps=2)])
            with patch.dict(sys.modules,{'review_statistics':Mock(),'quick_tarp_qc':Mock()}),patch.object(auto,'Session',return_value=session),patch('review_statistics.read',return_value=np.zeros((100,100))),patch('uav_pipeline.tarp_native_proposal.propose',return_value=dict(regions=regions)) as detect,patch.object(auto,'track',return_value=(regions,{})) as track,patch('quick_tarp_qc.inspect',side_effect=inspect) as qc,patch('uav_pipeline.roi_batch_import.import_proposals',return_value=(0,{})) as trim:
                result=auto.prepare('config')
                self.assertEqual(detect.call_count,1);self.assertEqual(track.call_count,1)
                self.assertEqual(qc.call_count,2);trim.assert_called_once_with(session)
                self.assertEqual([e['shot'] for e in result.auto_result['entries']],['A','B'])
                self.assertFalse(any(e['reviewed'] for e in result.auto_result['entries']))
                auto.prepare('config')
                self.assertEqual(qc.call_count,2)  # Exact repeat reuses finished results.

    def test_results_first_does_not_open_an_image_or_approve_anything(self):
        import project_ui
        root=Mock()
        with patch.object(project_ui,'Session') as session, patch.object(project_ui.tk,'StringVar'), patch.object(project_ui.App,'__init__',lambda obj,r:setattr(obj,'root',r)):
            app=project_ui.ProjectApp(root,'unused')
            root.withdraw.assert_called_once()
            root.after.assert_called_once_with(100,app.show_review_queue)
            session.return_value.save_draft.assert_not_called()
            session.return_value.publish.assert_not_called()

    def test_result_rejection_revokes_old_approval_and_keeps_missing_frame_held(self):
        import project_ui
        app=project_ui.ProjectApp.__new__(project_ui.ProjectApp)
        app.busy=False;app.loaded_shot='A';app.review_window=Mock()
        app.session=Mock(unsafe=True);app.session.shots=['A','B']
        app.session.images={'A':[dict(tarp_id='black',reviewed=True,pixel_reviewed=True)]}
        app.proposal_rejections={'A':Mock(get=lambda:True)};app.proposal_snapshot={}
        app.session.exclude_rejected.side_effect=ValueError('two valid frames required')
        for name in ('refresh_list','redraw','refresh_review_queue','note','save'):
            setattr(app,name,Mock())
        with patch.object(project_ui.messagebox,'showinfo'):
            with self.assertRaises(ValueError):app.accept_proposals()
        self.assertFalse(app.session.images['A'][0]['reviewed'])
        self.assertFalse(app.session.images['A'][0]['pixel_reviewed'])
        self.assertNotIn('B',app.session.images)
        app.session.save_draft.assert_called_once();app.save.assert_not_called()

    def test_preview_skips_registry_scan_but_still_detects_changed_source(self):
        from uav_pipeline import roi_studio_bridge as bridge
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'raw.tif';source.write_bytes(b'original')
            selection=root/'selected.json';selection.write_text('{}')
            library=root/'library.csv';library.write_text('header')
            cfg={'dataset':{},'preparation':{'raw_directory':d,'tarp_reflectance_csv':str(library)}}
            config=root/'project.json';config.write_text(json.dumps(cfg))
            session=bridge.Session.__new__(bridge.Session)
            session.config_path=config;session.cfg=cfg;session.selection=selection;session.selection_hash=bridge.digest(selection)
            session.provenance={'identity':'test'};session.library_hash=bridge.digest(library)
            session.shots=['IMG'];session.files={'IMG_6':source};session.signatures={'IMG_6':signature([source])[0]}
            with patch.object(bridge,'is_general',return_value=True),patch.object(bridge,'_general_provenance',return_value=session.provenance),patch.object(bridge,'latest_candidate_selection',return_value=(selection,{},[])),patch.object(bridge,'scan_raw',return_value={('IMG',6):source}) as scan:
                session.assert_current(scan_registry=False);scan.assert_not_called()
                session.assert_current();scan.assert_called_once()
                source.write_bytes(b'changed input pixels')
                with self.assertRaises(ValueError):session.assert_current(scan_registry=False)

    def test_shadow_proposals_ignore_clipped_bands_and_require_agreement(self):
        import numpy as np
        from uav_pipeline.roi_batch_import import dark_pixels
        a=np.full((20,20),1000.,dtype='float32');a[8:10,3:17]=300
        core=np.ones(a.shape,bool)
        mask,meta=dark_pixels([a,a],[core,core],core)
        self.assertEqual(int(mask.sum()),28);self.assertFalse(meta['approved'])
        clipped=np.full(a.shape,65520.)
        mask,meta=dark_pixels([a,clipped],[core,core],core)
        self.assertEqual(int(mask.sum()),0);self.assertEqual(meta['status'],'review_required')
        invalid=core.copy();invalid[8:10,3:17]=False
        mask,_=dark_pixels([a,a],[invalid,invalid],core)
        self.assertEqual(int(mask.sum()),0)

    def test_reopen_restores_only_explicit_current_location(self):
        from types import SimpleNamespace
        from uav_pipeline.tarp_popup import CandidatePopup
        names=['tarp_a','tarp_b']
        context=dict(tarps=names,mosaic_role='same_flight',label='current',mosaic='mosaic.tif')
        def window():
            return SimpleNamespace(**{k:Mock() for k in ('tarp_box','header_detail','view','radius','use_regions','two_locations','active_location','outlying_tarp','status','_toggle_location','_region_mode','_show_mosaic','_show_candidates')},radii=[12,8],candidates=[1,2])
        w=window();state=dict(location_review_id='explicit',locations=[dict(point=dict(u=.2,v=.3),radius_m=2,tarps=names)],selected_ids=['A'])
        CandidatePopup._loaded(w,(context,None,state,{}))
        self.assertEqual(w.points,[(.2,.3),None]);self.assertEqual(w.radii[0],2)
        w._show_candidates.assert_called_once_with(state)
        w=window();CandidatePopup._loaded(w,(context,None,{},{}));w._show_candidates.assert_not_called()

    def test_gallery_never_offers_failed_proposal_for_bulk_accept(self):
        import tkinter as tk
        from PIL import Image
        import desktop
        from uav_pipeline import tarp_batch_qc_ui as gallery
        root=tk.Tk();root.withdraw()
        try:
            with tempfile.TemporaryDirectory() as d, patch.object(gallery,'_async'):
                p=Path(d)/'batch.json'
                a=dict(id='A',shot='A');b=dict(id='B',shot='B',error='no boundary')
                p.write_text(json.dumps({'entries':[a,b]}),encoding='utf-8')
                view=gallery.BatchQcReview(root,'http://127.0.0.1:1',p,['black','gray']);view.withdraw()
                view.show_previews([(a,Image.new('RGB',(200,170)),None),(b,None,'no boundary')])
                self.assertEqual(view.available,{'A'})
                self.assertEqual(set(view.rejected),{'A'})
                self.assertFalse(view.accept_button.instate(['disabled']))
        finally:
            desktop.destroy_owned_root(root);root.update_idletasks()

    def test_bulk_yes_no_preserves_scientific_approval_and_blocks_stale_source(self):
        from uav_pipeline.tarp_batch_qc_ui import record_decisions
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'raw';source.write_bytes(b'raw pixels')
            entries=[dict(id=n,shot=n,source_files=[str(source)],source_signature=signature([source]),
                          result_path='qc.json',roi_approved=False,reviewed=False) for n in ['A','B','C']]
            value={'entries':entries,'roi_approved':False};entries[2]['error']='failed registration'
            record_decisions(value,{'A'},{'B'})
            self.assertTrue(entries[0]['reviewed']);self.assertFalse(entries[1]['reviewed'])
            self.assertFalse(any(e['roi_approved'] for e in entries));self.assertFalse(value['roi_approved'])
            self.assertEqual(entries[1]['user_boundary_decision'],'no')
            with self.assertRaises(ValueError):record_decisions(value,{'C'},set())
            before=json.dumps(value);source.write_bytes(b'changed pixels')
            with self.assertRaises(ValueError):record_decisions(value,{'B'},set())
            self.assertEqual(before,json.dumps(value))

    def test_next_action_never_treats_registration_error_as_pass(self):
        self.assertIn('보류',action(dict(error='bad transform',usable_tarps=3)))
        self.assertIn('사용 불가',action(dict(usable_tarps=1)))
        self.assertIn('경계 확인',action(dict(usable_tarps=2)))

    def test_signature_detects_source_change(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'source';p.write_bytes(b'original')
            before=signature([p]);p.write_bytes(b'changed pixels')
            self.assertNotEqual(before,signature([p]))

    def test_batch_uses_only_finalized_selection_and_keeps_regions(self):
        import tkinter as tk
        from PIL import Image
        import desktop
        root=tk.Tk();root.withdraw()
        try:
            with patch.object(ui,'_async') as async_call, tempfile.TemporaryDirectory() as d:
                window=ui.NativeTarpQcPopup(root,'http://127.0.0.1:1','A','A',['black','gray'])
                window.withdraw();window.view.set_image(Image.new('RGB',(100,100)))
                regions={'black':[[.1,.1],[.2,.1],[.2,.2],[.1,.2]],'gray':[[.5,.5],[.6,.5],[.6,.6],[.5,.6]]}
                window.regions=regions;window.reviewed.set(True)
                window.result={'source_signature':[['source',1,2]]}
                rows=[dict(id='A',shot='A',source_files=['source']),dict(id='B',shot='B',source_files=['excluded'])]
                cache=Path(d)/'cache';cache.mkdir()
                window.api.get=Mock(side_effect=[dict(candidates=rows,selected_ids=['A']),dict(output=d)])
                window.run_batch()
                request=json.loads(next(cache.glob('*.request.json')).read_text(encoding='utf-8'))
                self.assertEqual([r['id'] for r in request['candidates']],['A'])
                self.assertEqual(request['regions'],regions)
                self.assertEqual(window.regions,regions)
                window.busy=False
                count=async_call.call_count
                window.api.get=Mock(side_effect=[dict(candidates=rows,selected_ids=[]),dict(output=d)])
                window.run_batch()
                self.assertEqual(async_call.call_count,count)
                self.assertIn('체크',window.status.get())
                window.chosen_ids=['B']
                window.api.get=Mock(side_effect=[dict(candidates=rows,selected_ids=['A']),dict(output=d)])
                window.run_batch()
                requests=[json.loads(p.read_text(encoding='utf-8')) for p in cache.glob('*.request.json')]
                self.assertTrue(any([r['id'] for r in v['candidates']]==['B'] for v in requests))
        finally:
            desktop.destroy_owned_root(root)
            root.update_idletasks()

if __name__=='__main__':unittest.main(verbosity=2)
