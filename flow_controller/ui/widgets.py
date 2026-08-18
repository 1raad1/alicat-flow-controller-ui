import tkinter as tk


class RoundedPanel(tk.Frame):
    def __init__(self, master, *, text='', bg='#111110',
                 fg='#efe9dc', font=('Yu Gothic UI', 11),
                 border='#3a3a34', radius=5, padx=10, pady=10,
                 parent_bg=None, collapsible=False, collapsed=False, **kwargs):
        kwargs.pop('relief', None)
        kwargs.pop('bd', None)
        if parent_bg is None:
            try:
                parent_bg = master.cget('bg')
            except tk.TclError:
                parent_bg = '#111110'
        super().__init__(master, bg=parent_bg, **kwargs)
        self._card_bg   = bg
        self._parent_bg = parent_bg
        self._border    = border
        self._radius    = radius
        self._padx      = padx
        self._pady      = pady
        self._text      = text
        self._collapsible = bool(collapsible and text)
        self._collapsed = False
        self._canvas = tk.Canvas(self, bg=parent_bg, highlightthickness=0, bd=0)
        self._canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._canvas.bind('<Configure>', self._redraw)
        if text:
            self._title_row = tk.Frame(
                self, bg=bg, cursor='hand2' if self._collapsible else '')
            self._title_row.pack(fill='x', padx=padx, pady=(pady, 2))
            self._title_lbl = tk.Label(
                self._title_row, text=text, bg=bg, fg=fg, font=font,
                cursor='hand2' if self._collapsible else '')
            self._title_lbl.pack(side='left', anchor='w')
            if self._collapsible:
                self._collapse_lbl = tk.Label(
                    self._title_row, text='\u25be', bg=bg, fg='#8a8a84',
                    font=('Yu Gothic UI', 10, 'bold'), cursor='hand2')
                self._collapse_lbl.pack(side='right')
                for widget in (self._title_row, self._title_lbl, self._collapse_lbl):
                    widget.bind('<Button-1>', self.toggle_collapsed)
        self.body = tk.Frame(self, bg=bg)
        self._body_pack_options = dict(
            fill='both', expand=True, padx=padx,
            pady=(0 if text else 1, pady))
        self.body.pack(**self._body_pack_options)
        self.body.tkraise()
        if hasattr(self, '_title_row'):
            self._title_row.tkraise()
        if collapsed and self._collapsible:
            self.set_collapsed(True)
        elif hasattr(self, '_title_lbl'):
            self._title_lbl.tkraise()

    def set_title(self, text):
        if hasattr(self, '_title_lbl'):
            self._title_lbl.configure(text=text)

    def set_border(self, color):
        self._border = color
        self._redraw()

    def toggle_collapsed(self, _event=None):
        if self._collapsible:
            self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed):
        if not self._collapsible:
            return
        self._collapsed = bool(collapsed)
        if self._collapsed:
            self.body.pack_forget()
            self._collapse_lbl.configure(text='\u25b8')
        else:
            self.body.pack(**self._body_pack_options)
            self.body.tkraise()
            self._collapse_lbl.configure(text='\u25be')
        self.after_idle(self._redraw)

    def set_card_bg(self, bg):
        self._card_bg = bg
        self.body.configure(bg=bg)
        if hasattr(self, '_title_row'):
            self._title_row.configure(bg=bg)
        if hasattr(self, '_title_lbl'):
            self._title_lbl.configure(bg=bg)
        if hasattr(self, '_collapse_lbl'):
            self._collapse_lbl.configure(bg=bg)
        self._redraw()

    def _redraw(self, _event=None):
        c = self._canvas
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 2 or h <= 2:
            return
        c.delete('all')
        r = min(self._radius, w // 2, h // 2)
        fill = self._card_bg
        out  = self._border
        c.create_rectangle(r, 0, w - r, h, fill=fill, outline='')
        c.create_rectangle(0, r, w, h - r, fill=fill, outline='')
        c.create_oval(0, 0, 2 * r, 2 * r, fill=fill, outline='')
        c.create_oval(w - 2 * r, 0, w, 2 * r, fill=fill, outline='')
        c.create_oval(0, h - 2 * r, 2 * r, h, fill=fill, outline='')
        c.create_oval(w - 2 * r, h - 2 * r, w, h, fill=fill, outline='')
        c.create_arc(0, 0, 2 * r, 2 * r, start=90, extent=90, style='arc', outline=out, width=1)
        c.create_arc(w - 2 * r - 1, 0, w - 1, 2 * r, start=0, extent=90, style='arc', outline=out, width=1)
        c.create_arc(0, h - 2 * r - 1, 2 * r, h - 1, start=180, extent=90, style='arc', outline=out, width=1)
        c.create_arc(w - 2 * r - 1, h - 2 * r - 1, w - 1, h - 1, start=270, extent=90, style='arc', outline=out, width=1)
        c.create_line(r, 0, w - r, 0, fill=out, width=1)
        c.create_line(r, h - 1, w - r, h - 1, fill=out, width=1)
        c.create_line(0, r, 0, h - r, fill=out, width=1)
        c.create_line(w - 1, r, w - 1, h - r, fill=out, width=1)


class ZenTabs(tk.Frame):
    def __init__(self, master, *,
                 bg='#111110', tab_bg='#1a1a17', tab_active='#22221f',
                 tab_fg='#8a8a84', tab_active_fg='#efe9dc',
                 accent='#f25d38', border='#3a3a34',
                 font=('Atpos', 11), radius=3,
                 tab_pad_x=18, tab_pad_y=9, tab_gap=6,
                 strip_pad=(10, 10, 10, 0), **kw):
        super().__init__(master, bg=bg, **kw)
        self._bg = bg; self._tab_bg = tab_bg; self._tab_active = tab_active
        self._tab_fg = tab_fg; self._tab_active_fg = tab_active_fg
        self._accent = accent; self._border = border; self._font = font
        self._radius = radius; self._tpx = tab_pad_x; self._tpy = tab_pad_y
        self._gap = tab_gap; self._tabs = []; self._current = -1
        spx1, spy1, spx2, spy2 = strip_pad
        self._strip = tk.Frame(self, bg=bg)
        self._strip.pack(fill='x', padx=(spx1, spx2), pady=(spy1, spy2))
        self._content = tk.Frame(self, bg=bg)
        self._content.pack(fill='both', expand=True)

    def add(self, frame, *, text):
        idx = len(self._tabs)
        import tkinter.font as tkfont
        f = tkfont.Font(font=self._font)
        tw = f.measure(text) + self._tpx * 2
        th = f.metrics('linespace') + self._tpy * 2
        canvas = tk.Canvas(self._strip, width=tw, height=th,
                           bg=self._bg, highlightthickness=0, bd=0, cursor='hand2')
        canvas.pack(side='left', padx=(0, self._gap))
        rec = dict(
            canvas=canvas, frame=frame, label=text, w=tw, h=th,
            hover=False, flare=False, flare_phase=0.0)
        self._tabs.append(rec)
        canvas.bind('<Button-1>', lambda _e, i=idx: self.select(i))
        canvas.bind('<Enter>',    lambda _e, i=idx: self._set_hover(i, True))
        canvas.bind('<Leave>',    lambda _e, i=idx: self._set_hover(i, False))
        frame.place(in_=self._content, x=0, y=0, relwidth=1, relheight=1)
        frame.lower()
        self._draw(idx)
        if self._current < 0:
            self.select(0)
        return idx

    def select(self, index):
        if index == self._current or not (0 <= index < len(self._tabs)):
            return
        prev = self._current
        self._current = index
        if prev >= 0:
            self._draw(prev)
        self._draw(index)
        self._tabs[index]['frame'].tkraise()
        self.event_generate('<<TabChanged>>')

    def index(self, which='current'):
        if which == 'current':
            return self._current
        raise ValueError('only "current" supported')

    def _set_hover(self, i, on):
        self._tabs[i]['hover'] = on
        self._draw(i)

    def set_flare(self, index, enabled, phase=0.0):
        if not 0 <= index < len(self._tabs):
            return
        self._tabs[index]['flare'] = bool(enabled)
        self._tabs[index]['flare_phase'] = float(phase) % 1.0
        self._draw(index)

    def _draw(self, i):
        rec = self._tabs[i]
        c = rec['canvas']
        c.delete('all')
        w, h = rec['w'], rec['h']
        r = self._radius
        active = (i == self._current)
        hover  = rec['hover']
        flare = rec['flare']
        if flare:
            fill = '#12351f' if active else '#10291a'
            fg = '#bbf7d0'; outline = '#4ade80'; width = 2
        elif active:
            fill = self._tab_active; fg = self._tab_active_fg
            outline = self._accent; width = 2
        elif hover:
            fill = '#1f1f1c'; fg = '#bfb9ac'; outline = ''; width = 1
        else:
            fill = self._tab_bg; fg = self._tab_fg; outline = ''; width = 1
        rr = min(r, w // 2, h // 2)
        c.create_rectangle(rr, 0, w - rr, h, fill=fill, outline='')
        c.create_rectangle(0, rr, w, h - rr, fill=fill, outline='')
        c.create_oval(0, 0, 2 * rr, 2 * rr, fill=fill, outline='')
        c.create_oval(w - 2 * rr, 0, w, 2 * rr, fill=fill, outline='')
        c.create_oval(0, h - 2 * rr, 2 * rr, h, fill=fill, outline='')
        c.create_oval(w - 2 * rr, h - 2 * rr, w, h, fill=fill, outline='')
        if outline:
            c.create_arc(0, 0, 2 * rr, 2 * rr, start=90, extent=90, style='arc', outline=outline, width=width)
            c.create_arc(w - 2 * rr - 1, 0, w - 1, 2 * rr, start=0, extent=90, style='arc', outline=outline, width=width)
            c.create_arc(0, h - 2 * rr - 1, 2 * rr, h - 1, start=180, extent=90, style='arc', outline=outline, width=width)
            c.create_arc(w - 2 * rr - 1, h - 2 * rr - 1, w - 1, h - 1, start=270, extent=90, style='arc', outline=outline, width=width)
            c.create_line(rr, 0, w - rr, 0, fill=outline, width=width)
            c.create_line(rr, h - 1, w - rr, h - 1, fill=outline, width=width)
            c.create_line(0, rr, 0, h - rr, fill=outline, width=width)
            c.create_line(w - 1, rr, w - 1, h - rr, fill=outline, width=width)
        c.create_text(w / 2, h / 2, text=rec['label'], fill=fg, font=self._font)
        if flare:
            phase = rec['flare_phase']
            span = max(22, int(w * 0.32))
            start = int(-span + (w + span) * phase)
            c.create_line(
                max(0, start), h - 2, min(w, start + span), h - 2,
                fill='#86efac', width=3)
            c.create_oval(7, h // 2 - 3, 13, h // 2 + 3,
                          fill='#4ade80', outline='')
