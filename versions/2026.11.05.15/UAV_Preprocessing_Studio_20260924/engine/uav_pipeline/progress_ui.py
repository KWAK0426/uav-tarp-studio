"""One reusable, non-blocking progress window per task owner; no invented percentages."""
import re
import time
from datetime import datetime,timedelta
import tkinter as tk
from tkinter import ttk

COUNT=re.compile(r'(?<![\d/])(\d+)\s*/\s*(\d+)(?![\d/])')


def center_window(window,parent,width,height,anchor=None):
    parent.update_idletasks()
    match=re.fullmatch(r'(\d+)x(\d+)([+-]\d+)([+-]\d+)',anchor or '')
    if match:
        pw,ph,x,y=map(int,match.groups())
    else:
        pw,ph=parent.winfo_width(),parent.winfo_height()
        x,y=parent.winfo_rootx(),parent.winfo_rooty()
    window.geometry(f'{width}x{height}{x+(pw-width)//2:+d}{y+(ph-height)//2:+d}')


def center_studio_popups(root):
    """Center each newly opened Tk dialog on Studio, including nested dialogs."""
    if getattr(root, '_studio_center_binding', None): return
    def mapped(event):
        window = event.widget
        if (not isinstance(window, tk.Toplevel) or window._root() is not root
                or window.overrideredirect() or getattr(window, '_studio_centered', False)):
            return
        window._studio_centered = True
        def position():
            if window.winfo_exists() and root.winfo_exists():
                root.update_idletasks()
                width,height=window.winfo_width(),window.winfo_height()
                border_x=window.winfo_rootx()-window.winfo_x()
                border_y=window.winfo_rooty()-window.winfo_y()
                x=root.winfo_rootx()+(root.winfo_width()-width)//2-border_x
                y=root.winfo_rooty()+(root.winfo_height()-height)//2-border_y
                window.geometry(f'{width}x{height}{x:+d}{y:+d}')
        root.after_idle(position)
    root._studio_center_binding = root.bind_all('<Map>', mapped, add='+')


def count_from_text(text):
    match=COUNT.search(str(text))
    if match:
        done,total=map(int,match.groups())
        if total>0 and 0<=done<=total:return done,total
    return None


def remaining_seconds(done,total,baseline,seconds):
    if done>=total:return 0
    if done-baseline<2 or seconds<3:return None
    return seconds*(total-done)/(done-baseline)


class ProgressPopup(tk.Toplevel):
    def __init__(self,parent,title,anchor=None):
        super().__init__(parent)
        self.title('UAV Studio · 처리 진행 중')
        self.resizable(False,False)
        if parent.winfo_viewable():self.transient(parent)
        self.configure(background='#14171b')
        center_window(self,parent,460,210,anchor)
        self.started=time.monotonic();self.timer=None
        self.counts=None;self.phase_started=None;self.phase_done=0
        self.heading=tk.StringVar(value=title);self.status=tk.StringVar(value='준비 중 · 작업량 확인 중')
        self.percent=tk.StringVar(value='준비 중');self.elapsed=tk.StringVar()
        for variable,font in ((self.heading,('Malgun Gothic',11,'bold')),(self.percent,('Malgun Gothic',21,'bold'))):
            tk.Label(self,textvariable=variable,font=font,bg='#14171b',fg='#75d5b6',wraplength=428,justify='left').pack(anchor='w',padx=16,pady=(8,0))
        style=ttk.Style(self)
        for element in ('trough','pbar'):
            name='StudioProgress.'+element
            if name not in style.element_names():
                style.element_create(name,'from','clam','Horizontal.Progressbar.'+element)
        style.layout('StudioProgress.Horizontal.TProgressbar',[
            ('StudioProgress.trough',{'sticky':'nswe','children':[
                ('StudioProgress.pbar',{'side':'left','sticky':'ns'})]})])
        style.configure('StudioProgress.Horizontal.TProgressbar',troughcolor='#2b3239',
                        background='#75d5b6',bordercolor='#2b3239',lightcolor='#75d5b6',darkcolor='#75d5b6',borderwidth=0,thickness=10)
        self.bar=ttk.Progressbar(self,style='StudioProgress.Horizontal.TProgressbar',mode='indeterminate')
        self.bar.pack(fill='x',padx=16,pady=10)
        tk.Label(self,textvariable=self.status,font=('Malgun Gothic',10),bg='#14171b',fg='#edf2f7',wraplength=428,justify='left').pack(anchor='w',padx=16)
        tk.Label(self,textvariable=self.elapsed,font=('Malgun Gothic',9),bg='#14171b',fg='#b5bec8',justify='left',wraplength=428).pack(anchor='w',padx=16,pady=6)
        self.protocol('WM_DELETE_WINDOW',lambda:None)
        self.bar.start(15);self.tick();self.lift()
        self.update_idletasks()
        self.dark_titlebar()

    def dark_titlebar(self):
        import os
        if os.name!='nt':return
        import ctypes
        from ctypes import wintypes
        user=ctypes.windll.user32
        user.GetParent.argtypes=[wintypes.HWND];user.GetParent.restype=wintypes.HWND
        hwnd=user.GetParent(self.winfo_id())
        apply=ctypes.windll.dwmapi.DwmSetWindowAttribute
        apply.argtypes=[wintypes.HWND,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD]
        for attribute,value in ((20,1),(35,0x1b1714),(36,0xf7f2ed)):
            setting=ctypes.c_int(value)
            apply(hwnd,attribute,ctypes.byref(setting),ctypes.sizeof(setting))

    def tick(self):
        now=time.monotonic();seconds=int(now-self.started)
        estimate='예상 남은 시간·완료 시각 계산 중'
        if self.counts is not None:
            remaining=remaining_seconds(*self.counts,self.phase_done,now-self.phase_started)
            if remaining==0:
                estimate='현재 단계 처리 완료 · 다음 작업 확인 중'
            elif remaining is not None:
                minutes=max(1,int((remaining+59)//60))
                finish_at=datetime.now().astimezone()+timedelta(seconds=remaining)
                duration=f'{minutes//60}시간 {minutes%60}분' if minutes>=60 else f'{minutes}분'
                estimate=f'약 {duration} 남음 · 예상 완료 {finish_at:%m/%d %H:%M}'
        self.elapsed.set(f'경과 {seconds//60:02d}:{seconds%60:02d} · 현재 단계 기준\n{estimate}')
        self.timer=self.after(1000,self.tick)

    def update_progress(self,text,done=None,total=None):
        self.status.set(str(text)[:250])
        counts=(done,total) if done is not None and total else count_from_text(text)
        self.bar.stop()
        if counts and counts[1]>0 and 0<=counts[0]<=counts[1]:
            done,total=counts;value=100*done/total
            if self.counts is None or total!=self.counts[1] or done<self.counts[0]:
                self.phase_started=time.monotonic();self.phase_done=done
            self.counts=(done,total)
            self.bar.configure(mode='determinate',maximum=total,value=done)
            self.percent.set(f'{value:.1f}%  ·  {done:,} / {total:,}')
        else:
            self.counts=None;self.phase_started=None
            self.bar.configure(mode='indeterminate');self.bar.start(15)
            self.percent.set('진행 중')

    def destroy(self):
        if self.timer:
            self.after_cancel(self.timer);self.timer=None
        self.bar.stop()
        super().destroy()


def begin(owner,title,parent=None,anchor=None):
    popup=getattr(owner,'_processing_popup',None)
    if popup is not None and popup.winfo_exists():
        popup.heading.set(title);popup.update_progress('준비 중');popup.lift();return popup
    parent=parent or getattr(owner,'root',owner)
    popup=ProgressPopup(parent,title,anchor)
    owner._processing_popup=popup
    return popup


def update(owner,text,done=None,total=None):
    popup=getattr(owner,'_processing_popup',None)
    if popup is not None and popup.winfo_exists():popup.update_progress(text,done,total)


def finish(owner):
    popup=getattr(owner,'_processing_popup',None)
    if popup is not None and popup.winfo_exists():popup.destroy()
    owner._processing_popup=None
