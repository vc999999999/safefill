"""Synthetic-only CLI window driver. Exercises real Tk widgets, never bypasses decide."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import tkinter as tk
from tkinter import ttk
import collection

original = tk.Tk

def scripted_window(*args, **kwargs):
    root = original(*args, **kwargs)
    def walk(parent):
        for child in parent.winfo_children():
            yield child
            yield from walk(child)
    def exercise():
        widgets = list(walk(root))
        buttons = {w.cget('text'): w for w in widgets if isinstance(w, ttk.Button)}
        buttons['提交人工确认'].invoke()  # Must remain open until checks/pages complete.
        assert root.winfo_exists()
        for _ in range(3):
            buttons['下一页'].invoke()
        for label in ('放大', '缩小', '旋转'):
            buttons[label].invoke()
        for widget in widgets:
            if isinstance(widget, ttk.Checkbutton):
                widget.invoke()
        buttons['提交人工确认'].invoke()
    root.after(350, exercise)
    return root

tk.Tk = scripted_window
collection.main()
