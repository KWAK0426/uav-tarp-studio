"""One visible next-action border per window, without changing button commands or focus."""
import tkinter as tk
from tkinter import ttk


class ButtonGuide:
    def __init__(self, window):
        self.window = window
        self.current = None
        self.original = ''
        self.style = ttk.Style(window)
        root = window._root()
        if 'guideborder' not in self.style.element_names():
            image = tk.PhotoImage(master=root, width=9, height=9)
            image.put('#ff555d', to=(0, 0, 9, 9))
            image.put('#2b3239', to=(3, 3, 6, 6))
            root._guide_border_image = image
            self.style.element_create('guideborder', 'image', image, border=3, padding=3, sticky='nswe')

    def show(self, button):
        if button is not None and (not button.winfo_exists() or button.instate(['disabled'])):
            button = None
        if button is self.current:
            return
        if self.current is not None and self.current.winfo_exists():
            self.current.configure(style=self.original)
        self.current = button
        if button is None:
            return
        self.original = str(button.cget('style'))
        base = self.original or 'TButton'
        highlighted = 'NextAction.' + base
        # The border image supplies a charcoal fill even for mint accent buttons.
        self.style.configure(highlighted, foreground='#f4f6f8')
        self.style.map(highlighted, foreground=[('disabled', '#8b949e'), ('!disabled', '#f4f6f8')])
        self.style.layout(highlighted, [('guideborder', {
            'sticky':'nswe', 'children':self.style.layout(base)})])
        button.configure(style=highlighted)

    def watch(self, refresh):
        handle = None
        def update():
            nonlocal handle
            refresh()
            handle = self.window.after(350, update)
        def stop(event):
            if event.widget is self.window and handle is not None:
                self.window.after_cancel(handle)
        self.window.bind('<Destroy>', stop, add='+')
        handle = self.window.after_idle(update)
