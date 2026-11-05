import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ed_import as importer
import distribution

BANDS = distribution.preparation_template()['band_names']


def spectrum(path, stamp, value=100, unit='mW/(m^2 nm)'):
    path.parent.mkdir(parents=True, exist_ok=True)
    waves = [int(name.split('_')[-1][:-2]) for name in BANDS.values()]
    path.write_text(f'Unit2 = {unit}\nDateTime = {stamp}\n[DATA]\n'+
                    '\n'.join(f'{wave} {value}' for wave in waves), encoding='utf-8')


class ImportTests(unittest.TestCase):
    def test_project_folder_button_converts_and_connects_only_current_date(self):
        import tkinter as tk
        import json
        import time
        from desktop import DesktopStudio
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp); raw = folder/'raw'
            spectrum(raw/'a.dat','2026-10-07 10:30:00')
            spectrum(raw/'b.dat','2026-10-08 10:30:00')
            with patch.object(distribution,'STATE',folder/'state'):
                config = distribution.create_project(folder/'projects','2026-10-07','1st','Test')
                root = tk.Tk(); root.withdraw()
                app = DesktopStudio(root,config)
                root.withdraw()
                failures = []
                def run_folder():
                    with patch('desktop.filedialog.askdirectory',return_value=str(raw)):
                        app.import_ed_folder()
                    deadline = time.monotonic()+20
                    def finished():
                        if not app._busy() or time.monotonic()>deadline: root.quit()
                        else: root.after(50,finished)
                    root.after(50,finished)
                    root.mainloop()
                    self.assertFalse(app._busy())
                try:
                    with patch('desktop.messagebox.showerror',side_effect=lambda *a,**k:failures.append(a)):
                        run_folder()
                        self.assertFalse(failures)
                        path = Path(app._value('preparation.ed_csv'))
                        with path.open(encoding='utf-8-sig') as stream:
                            rows = list(csv.DictReader(stream))
                        self.assertEqual(len(rows),1)
                        self.assertEqual(rows[0]['datetime_kst'],'2026-10-07 10:30:00')
                        self.assertEqual(float(rows[0]['Band1_Blue_444nm']),100)
                        self.assertEqual(json.loads(config.read_text(encoding='utf-8'))['preparation']['ed_csv'],str(path))
                        saved = path.read_bytes()
                        # A different date must not replace the successfully connected CSV.
                        (raw/'a.dat').unlink()
                        run_folder()
                        self.assertIn('2026-10-07',failures[-1][1])
                        self.assertEqual(app._value('preparation.ed_csv'),str(path))
                        self.assertEqual(path.read_bytes(),saved)
                finally:
                    root.destroy()

    def test_current_date_rejects_changed_csv_and_conversion_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); raw=folder/'raw'
            spectrum(raw/'a.dat','2026-10-07 10:30:00')
            progress=[]
            result=importer.convert_folder(raw,folder/'out','Asia/Seoul',BANDS,
                                           progress=lambda *values:progress.append(values))
            entry=importer.converted_date(result,'2026-10-07')
            self.assertEqual(progress[-1],(1,1))
            Path(entry['csv']).write_text('changed',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'변경'):
                importer.converted_date(result,'2026-10-07')
            result['errors']=['conflicting measurement']
            with self.assertRaisesRegex(ValueError,'conflicting'):
                importer.converted_date(result,'2026-10-07')

    def test_dates_sort_timezone_copies_and_radiance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); raw = root/'raw'
            spectrum(raw/'b.dat', '2026-09-08 15:00:00')
            spectrum(raw/'a.DAT', '2026-09-08 14:59:59')
            spectrum(raw/'c.dat', '2026-09-08 15:00:01')
            (raw/'copy').mkdir(); (raw/'copy/b.dat').write_bytes((raw/'b.dat').read_bytes())
            spectrum(raw/'rad.dat', '2026-09-08 15:00:00', unit='mW/(m^2 nm sr)')
            before = {p:p.read_bytes() for p in raw.rglob('*') if p.is_file()}
            result = importer.convert_folder(raw, root/'out', 'UTC', BANDS)
            self.assertFalse(result['errors'])
            self.assertEqual([r['date'] for r in result['dates']], ['2026-09-08','2026-09-09'])
            self.assertEqual(result['identical_copies'], 1)
            self.assertEqual(result['skipped_radiance'], 1)
            day = result['dates'][1]
            self.assertEqual(day['start_kst'], '2026-09-09 00:00:00')
            with Path(day['csv']).open(encoding='utf-8-sig') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertEqual(float(rows[0]['Band1_Blue_444nm']), 100)
            self.assertEqual(before, {p:p.read_bytes() for p in before})
            again = importer.convert_folder(raw, root/'out', 'UTC', BANDS)
            self.assertNotEqual(result['folder'], again['folder'])

    def test_conflicting_time_blocks_output_and_keeps_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); raw = root/'raw'
            spectrum(raw/'a.dat', '2026-09-08 12:00:00')
            spectrum(raw/'b.dat', '2026-09-08 12:00:00', value=200)
            result = importer.convert_folder(raw, root/'out', 'Asia/Seoul', BANDS)
            self.assertTrue(result['errors'])
            self.assertFalse(result['dates'])
            self.assertTrue((Path(result['folder'])/'source_audit.csv').exists())
            self.assertIn('같은 시각', result['errors'][0])

    def test_invalid_inputs_cannot_silently_produce_csv(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); raw = root/'raw'; raw.mkdir()
            with self.assertRaises(ValueError):
                importer.convert_folder(raw, root/'out', '', BANDS)
            with self.assertRaises(ValueError):
                importer.convert_folder(raw, root/'out', 'Asia/Seoul', BANDS)
            spectrum(raw/'bad.dat', '2026-09-08 12:00:00', value=0)
            result = importer.convert_folder(raw, root/'out', 'Asia/Seoul', BANDS)
            self.assertTrue(result['errors']); self.assertFalse(result['dates'])

    def test_explicit_time_window_excludes_conflict_outside_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); raw = root/'raw'
            spectrum(raw/'a.dat', '2026-09-08 11:00:00')
            spectrum(raw/'b.dat', '2026-09-08 11:00:00', value=200)
            spectrum(raw/'c.dat', '2026-09-08 12:00:00')
            result = importer.convert_folder(raw, root/'out', 'Asia/Seoul', BANDS,
                                             '2026-09-08 12:00:00', '2026-09-08 12:01:00')
            self.assertFalse(result['errors']); self.assertEqual(result['outside_window'], 2)
            self.assertEqual(result['dates'][0]['count'], 1)
            (raw/'bad.dat').write_text('bad format')
            result = importer.convert_folder(raw, root/'out', 'Asia/Seoul', BANDS)
            self.assertTrue(result['errors']); self.assertFalse(result['dates'])

    def test_gui_does_not_connect_other_date_or_edited_csv(self):
        import tkinter as tk
        root = tk.Tk(); root.withdraw()
        calls = []
        window = importer.EdImportWindow(root, 'unused', BANDS, lambda path:calls.append(path), '2026-09-08')
        window.root.withdraw()
        try:
            window.result = {'dates':[dict(date='2026-09-09', csv='missing.csv', sha256='x')]}
            window.table.insert('', 'end', iid='2026-09-09')
            window.table.selection_set('2026-09-09')
            with patch.object(importer.messagebox, 'showwarning') as warning:
                window.attach(); warning.assert_called_once()
            window.date = '2026-09-09'
            with patch.object(importer.messagebox, 'showerror') as error:
                window.attach(); error.assert_called_once()
            self.assertFalse(calls)
        finally:
            window.root.destroy(); root.destroy()


if __name__ == '__main__': unittest.main()
