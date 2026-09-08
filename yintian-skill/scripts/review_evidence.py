"""Human-only in-memory evidence viewer. No exports, external viewers or temporary files."""
import io


def confirm_evidence(values, attachments, issues, *, agent_candidates=None):
    try:
        import tkinter as tk
        from tkinter import ttk
        from PIL import Image, ImageTk
    except ImportError:
        raise RuntimeError('GUI_UNAVAILABLE: 人工确认需要 Tk 和 Pillow；也可退回重填') from None
    root = tk.Tk()
    root.title('隐填 · 本地证据复核')
    root.geometry('1000x780')
    result = False
    documents, pages, visited, timer = [], [], set(), None
    view = {'index': 0, 'zoom': 1.0, 'rotation': 0, 'image': None, 'photo': None}

    def close():
        view.clear()
        root.destroy()

    def activity(event=None):
        nonlocal timer
        if timer is not None:
            root.after_cancel(timer)
        timer = root.after(300000, close)

    try:
        for item in attachments:
            if item['type'] == 'application/pdf':
                import pypdfium2 as pdfium
                document = pdfium.PdfDocument(item['data'])
                documents.append(document)
                if len(document) > 20:
                    raise RuntimeError('EVIDENCE_LIMIT: PDF 超过人工窗口页数上限')
                for number in range(len(document)):
                    pages.append((item['field_id'], document, number))
            else:
                pages.append((item['field_id'], item['data'], None))
        if not pages:
            raise RuntimeError('EVIDENCE_MISSING: 没有可供人工核对的附件')
        ttk.Label(root, text='逐页核对原件与填写值，再逐项确认。空闲五分钟自动关闭。').pack()
        text = tk.Text(root, height=5, wrap='word')
        text.insert('1.0', '\n'.join(f'{key}: {value!r}' for key, value in values.items()))
        if agent_candidates:
            text.insert('end', '\n宿主 Agent 候选（未经独立验证，请对照原件）：\n')
            for record in agent_candidates:
                for key, candidates in record['candidates'].items():
                    text.insert('end', f"{record['field_id']} → {key}: {candidates!r}\n")
        text.configure(state='disabled')
        text.pack(fill='x')
        title = ttk.Label(root)
        title.pack()
        canvas = tk.Canvas(root, background='#252525')
        canvas.pack(fill='both', expand=True)
        bar = ttk.Frame(root)
        bar.pack(fill='x')

        def show(delta=0, zoom=1.0, rotate=0):
            view['index'] = (view['index'] + delta) % len(pages)
            view['zoom'] = max(0.25, min(3.0, view['zoom'] * zoom))
            view['rotation'] = (view['rotation'] + rotate) % 360
            field, source, number = pages[view['index']]
            if number is None:
                with Image.open(io.BytesIO(source)) as opened:
                    original = opened.convert('RGB')
            else:
                page = source[number]
                try:
                    width, height = page.get_size()
                    if width * height > 40000000:
                        raise RuntimeError('EVIDENCE_LIMIT: 页面尺寸超过上限')
                    bitmap = page.render(scale=min(1.5, (40000000 / (width * height)) ** 0.5))
                    try:
                        original = bitmap.to_pil().copy()
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            original.thumbnail((2500, 2500))
            rotated = original.rotate(view['rotation'], expand=True)
            factor = min(900 / rotated.width, 420 / rotated.height) * view['zoom']
            resized = rotated.resize((max(1, int(rotated.width * factor)), max(1, int(rotated.height * factor))))
            view['photo'] = ImageTk.PhotoImage(resized)
            canvas.delete('all')
            canvas.create_image(0, 0, anchor='nw', image=view['photo'])
            canvas.configure(scrollregion=canvas.bbox('all'))
            canvas.bind('<ButtonPress-1>', lambda event: canvas.scan_mark(event.x, event.y))
            canvas.bind('<B1-Motion>', lambda event: canvas.scan_dragto(event.x, event.y, gain=1))
            visited.add(view['index'])
            title.configure(text=f'{field} · 第 {view["index"] + 1}/{len(pages)} 页；可拖动图片')

        for label, action in [('上一页', lambda: show(-1)), ('下一页', lambda: show(1)),
                              ('放大', lambda: show(zoom=1.25)), ('缩小', lambda: show(zoom=0.8)),
                              ('旋转', lambda: show(rotate=90))]:
            ttk.Button(bar, text=label, command=action).pack(side='left')
        checks = []
        for issue in issues:
            checked = tk.BooleanVar(value=False)
            ttk.Checkbutton(root, text='已核对并裁定：' + issue, variable=checked).pack(anchor='w')
            checks.append(checked)
        message = ttk.Label(root)
        message.pack()

        def accept():
            nonlocal result
            if len(visited) != len(pages) or not all(item.get() for item in checks):
                message.configure(text='请查看所有页面，并逐项勾选确认。')
                return
            result = True
            close()

        ttk.Button(root, text='提交人工确认', command=accept).pack()
        root.protocol('WM_DELETE_WINDOW', close)
        root.bind_all('<KeyPress>', activity)
        root.bind_all('<ButtonPress>', activity, add='+')
        activity()
        show()
        root.mainloop()
        return result
    finally:
        for document in documents:
            document.close()
        view.clear()
        try:
            root.destroy()
        except tk.TclError:
            pass
