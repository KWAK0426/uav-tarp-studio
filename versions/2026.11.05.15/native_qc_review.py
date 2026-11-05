"""Open an updated diagnostic window without closing a live map or its selection."""
import argparse
import tkinter as tk
import distribution
distribution.configure()
from legacy_studio import apply_studio_theme
from uav_pipeline.tarp_popup import LocalApi
from uav_pipeline.tarp_native_qc_ui import NativeTarpQcPopup

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--url',required=True);parser.add_argument('--item',required=True)
    parser.add_argument('--geometry',default='1420x900+2040+60')
    args=parser.parse_args()
    if not args.url.startswith('http://127.0.0.1:'):raise ValueError('Local Studio only')
    api=LocalApi(args.url);context=api.get('/context')
    row=next(r for r in api.get('/state')['candidates'] if r['id']==args.item)
    root=tk.Tk();root.withdraw();apply_studio_theme(root)
    popup=NativeTarpQcPopup(root,args.url,args.item,row['shot'],context['tarps'])
    popup.transient('')
    popup.geometry(args.geometry)
    popup.deiconify()
    popup.bind('<Destroy>',lambda event:root.destroy() if event.widget is popup else None)
    root.mainloop()
