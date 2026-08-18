import asyncio
import tkinter as tk
from tkinter import ttk, messagebox
from alicat import FlowController
import threading
from datetime import datetime
from queue import Queue
import csv
import serial
import math
import socket
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from collections import deque

from .domain.combustion import CombustionCalculator
from .domain.assignments import assess_autocalc
from .domain.graphing import padded_limits, parse_axis_limits, should_rescale
from .domain.safety import ZeroRequest, select_zero_units
from .infrastructure.alicat_protocol import AlicatProtocol
from .infrastructure.serial_worker import SerialIOWorker
from .services.discovery import DiscoveryService
from .ui.widgets import RoundedPanel, ZenTabs

from . import APP_VERSION


class AlicatDetectorUI:
    SCAN_UNITS = tuple(chr(code) for code in range(ord('A'), ord('Z') + 1))
    # The driver needs a short timeout to decide that an unused unit ID did
    # not answer. There are no additional sleeps or per-unit connection waits.
    SCAN_RESPONSE_TIMEOUT_S = 0.15
    ROLES = [
        ('nh3_rich',  'NH3 Stage 1'), ('h2_rich',   'H2 Stage 1'),
        ('nh3_lean',  'NH3 Stage 2'), ('h2_lean',   'H2 Stage 2'),
        ('ch4_pilot', 'CH4 Pilot'),   ('rich_air',  'Air Stage 1'),
        ('lean_air',  'Air Stage 2'),
    ]
    GRID_POS = {
        'nh3_rich': (0,0), 'h2_rich': (0,1), 'ch4_pilot': (0,2),
        'nh3_lean': (1,0), 'h2_lean': (1,1), 'rich_air':  (2,0),
        'lean_air': (2,1),
    }
    LHV_NH3 = 18.6; LHV_H2 = 120.0; RHO_NH3 = 0.7069; RHO_H2 = 0.0827
    FUEL_KEYS = {'nh3_rich', 'h2_rich', 'nh3_lean', 'h2_lean', 'ch4_pilot'}
    AIR_KEYS  = {'rich_air', 'lean_air'}
    RAMP_KEYS = {'ch4_pilot', 'rich_air', 'lean_air'}
    BASE_GAS_TYPES = ['Air', 'NH3', 'H2', 'CH4']
    ZONE_OPTIONS   = ['-- unassigned --', 'Zone 1', 'Zone 2', 'Pilot', 'General']
    ROLE_MAP = {
        ('NH3','Zone 1'): 'nh3_rich', ('H2','Zone 1'):  'h2_rich',
        ('Air','Zone 1'): 'rich_air', ('NH3','Zone 2'): 'nh3_lean',
        ('H2','Zone 2'):  'h2_lean',  ('Air','Zone 2'): 'lean_air',
        ('CH4','Pilot'):  'ch4_pilot',
    }
    GRAPH_METRICS = {
        'flow': ('Flow', 'flow', 'SLPM', 'flow', '-'),
        'sp': ('Setpoint', 'flow', 'SLPM', 'sp', '--'),
        'press': ('Pressure', 'pressure', 'psia', 'press', '-'),
        'temp': ('Temperature', 'temperature', '°C', 'temp', '-'),
        'internal_error': ('SP Error', 'error', 'SLPM', 'internal_error', ':'),
        'valve_drive': ('Valve Drive', 'valve', '%', 'valve_drive', '-.'),
    }
    GRAPH_GROUP_LABELS = {
        'flow': 'Flow & Setpoint',
        'pressure': 'Pressure',
        'temperature': 'Temperature',
        'error': 'Internal Setpoint Error',
        'valve': 'Valve Drive',
    }
    # Auto-scaling forces a full repaint, so limits are only reconsidered
    # every Nth rendered frame rather than on every one.
    GRAPH_LIMIT_CHECK_FRAMES = 5

    def __init__(self, root):
        self.root = root
        self.root.title(f"Alicat Flow Controller v{APP_VERSION}")
        self.root.geometry("1700x950")
        self.root.configure(bg='#111110')
        self._is_maximized    = False
        self._normal_geometry = None
        self._drag_offset     = (0, 0)
        self._remove_native_titlebar()
        self.calc = CombustionCalculator()
        self.is_scanning = False; self.is_monitoring = False
        self.controllers_connected = False
        self.is_connecting = False
        self.detected_controllers = []; self.controller_instances = {}
        self.setpoint_queue = Queue()
        self._zero_request_queue = Queue()
        self._zero_action_active = False
        self._active_zero_request = None
        self._zero_locked_units = set()
        self._ui_callback_queue = Queue()
        self._ui_log_queue = Queue()
        self._alicat_protocol = AlicatProtocol(logger=self._log_conn)
        self._discovery_service = DiscoveryService(self._alicat_protocol)
        self._serial_worker = SerialIOWorker()
        self._connection_future = None
        self._monitor_future = None
        self._restart_reason = None
        self._reconnect_active = False
        self._emergency_stop_active = False
        self._closing = False
        self.assignments = {key: None for key, _ in self.ROLES}
        self._custom_assignments = {}
        self._autocalc_available = True; self._autocalc_config = None
        self.cards_tab1 = {}; self.cards_tab2 = {}
        self.ignition_state = "IDLE"; self.target_flows = {}
        self.pre_fuel_scale = 0.8; self.pre_air_scale = 0.8
        self.logging = False; self.log_file = None; self.log_writer = None
        self._logging_source = None
        self._log_destination = str(
            Path(__file__).resolve().parent.parent / 'Logs' / 'flow_log.csv')
        self._labview_flare_active = False
        self._labview_flare_phase = 0.0
        self._labview_flare_after_id = None
        self._udp_host = '127.0.0.1'; self._udp_port = 5056
        self._udp_socket = None; self._udp_stop_event = threading.Event()
        self._udp_thread = None; self._udp_restart_token = 0
        self._restart_pending = False
        self._disc_enabled = True; self._disc_popup_open = False
        self._disc_ignore_until = {}; self._flash_jobs = {}
        self._flash_state = {}; self._pending_after_ids = set()
        self._last_sp = {}; self._ramp_active = {}
        # Configured delay after each complete multi-controller polling pass.
        # The monitor thread reads this plain float, never a Tk variable.
        self.poll_interval_s = 0.0
        # Serial acquisition may run much faster than Tk can redraw.  Refresh
        # cards/graphs on one bounded timer instead of queuing callbacks from
        # every polling pass.
        self._live_ui_refresh_ms = 100
        self._live_ui_after_id = None
        # The selected value only changes this application's serial
        # connection; it does not reconfigure the instruments, so every device
        # on the port must already match it.  Alicat ships at 19200; the rig
        # this build drives is set to 57600, so that is the default here.
        self.serial_baudrate = 57600
        self._monitor_port = None
        self._monitor_baudrate = None
        self._poll_rate_queue = Queue()
        # Latest values read directly from each controller.  The internal
        # setpoint error and valve-drive values are deliberately kept here
        # instead of being reconstructed from rounded UI labels.
        self._live_samples = {}
        self._telemetry_support = {}
        self._telemetry_generation = 0
        self._last_graphed_generation = -1
        self._latest_sample_timestamp = None
        self._latest_graph_samples = {}
        HISTORY = 600
        self._log_time = deque(maxlen=HISTORY); self._log_t0 = None
        self._graph_history_limit = HISTORY
        self._graph_history = {}
        self._graph_unit_meta = {}
        self._graph_series_vars = {}
        self._graph_axis_settings = {}
        self._graph_axis_controls = {}
        self._graph_rebuild_after_id = None
        # Graph rendering is lazy: the figure is only built, and the render
        # loop only runs, while the Logging & Graphs tab is on screen AND the
        # operator has ticked at least one series.  History keeps accumulating
        # either way, so switching to the tab mid-run shows the full trace.
        self._graph_tab_index = None
        self._graph_tab_visible = False
        self._graph_rendering = False
        self._graph_render_after_id = None
        self._graph_render_ms = 200
        self._graph_frame_index = 0
        self._graph_background = None
        self._graph_needs_full_redraw = True
        self._graph_axes = {}
        self._graph_lines = {}
        self._fig = None
        self._canvas = None
        self._log_units = []  # ordered list of units to log, set at _start_logging
        self._setup_styles()
        self.root.option_add('*TCombobox*Listbox.background',       '#181714')
        self.root.option_add('*TCombobox*Listbox.foreground',       '#efe9dc')
        self.root.option_add('*TCombobox*Listbox.selectBackground', '#2c2c28')
        self.root.option_add('*TCombobox*Listbox.selectForeground', '#efe9dc')
        self.root.option_add('*TCombobox*Listbox.font',             ('Consolas', 9))
        self.root.option_add('*TCombobox*Listbox.relief',           'flat')
        self._build_ui()
        self._start_udp_listener()
        self.root.after(25, self._drain_ui_queues)
        self.root.after(250, self._drain_poll_rate_queue)
        self.root.after_idle(self._win_toggle_max)

    # ------------------------------------------------------------------ #
    #  Frameless window                                                    #
    # ------------------------------------------------------------------ #
    def _remove_native_titlebar(self):
        import sys
        self._frameless = True; self._hwnd = None
        self._old_wndproc = None; self._new_wndproc = None
        self._snap_zone = None; self._snap_preview = None
        self._drag_offset = (0, 0); self._drag_pending = False
        self._drag_target = None
        if sys.platform != 'win32':
            try: self.root.overrideredirect(True)
            except Exception: self._frameless = False
            return
        try:
            import ctypes
            from ctypes import wintypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            self._hwnd = hwnd
            GWL_STYLE = -16; GWLP_WNDPROC = -4
            WS_CAPTION = 0x00C00000; WS_THICKFRAME = 0x00040000
            WS_MINIMIZEBOX = 0x00020000; WS_MAXIMIZEBOX = 0x00010000; WS_SYSMENU = 0x00080000
            get_long = getattr(ctypes.windll.user32, 'GetWindowLongPtrW', ctypes.windll.user32.GetWindowLongW)
            set_long = getattr(ctypes.windll.user32, 'SetWindowLongPtrW', ctypes.windll.user32.SetWindowLongW)
            get_long.restype = ctypes.c_ssize_t
            get_long.argtypes = [wintypes.HWND, ctypes.c_int]
            set_long.restype = ctypes.c_ssize_t
            set_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            style = get_long(hwnd, GWL_STYLE)
            style |= (WS_CAPTION | WS_SYSMENU | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX)
            set_long(hwnd, GWL_STYLE, style)
            GWL_EXSTYLE = -20; WS_EX_APPWINDOW = 0x00040000; WS_EX_TOOLWINDOW = 0x00000080
            ex = get_long(hwnd, GWL_EXSTYLE)
            ex = (ex & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
            set_long(hwnd, GWL_EXSTYLE, ex)
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            WM_NCCALCSIZE = 0x0083; WM_NCHITTEST = 0x0084; WM_NCACTIVATE = 0x0086
            HTCLIENT=1; HTLEFT=10; HTRIGHT=11; HTTOP=12; HTTOPLEFT=13; HTTOPRIGHT=14
            HTBOTTOM=15; HTBOTTOMLEFT=16; HTBOTTOMRIGHT=17; BORDER=6
            CallWindowProcW = ctypes.windll.user32.CallWindowProcW
            CallWindowProcW.restype = ctypes.c_ssize_t
            CallWindowProcW.argtypes = [ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            GetCursorPos = ctypes.windll.user32.GetCursorPos
            GetWindowRect = ctypes.windll.user32.GetWindowRect
            old_proc = [None]
            GetSystemMetrics = ctypes.windll.user32.GetSystemMetrics
            IsZoomed = ctypes.windll.user32.IsZoomed

            def py_wndproc(h, msg, wp, lp):
                if msg == WM_NCCALCSIZE and wp:
                    if IsZoomed(h):
                        SM_CXSIZEFRAME=32; SM_CYSIZEFRAME=33; SM_CXPADDEDBORDER=92
                        bx = GetSystemMetrics(SM_CXSIZEFRAME) + GetSystemMetrics(SM_CXPADDEDBORDER)
                        by = GetSystemMetrics(SM_CYSIZEFRAME) + GetSystemMetrics(SM_CXPADDEDBORDER)
                        r = ctypes.cast(lp, ctypes.POINTER(wintypes.RECT))
                        r[0].left += bx; r[0].right -= bx; r[0].top += by; r[0].bottom -= by
                    return 0
                if msg == WM_NCHITTEST:
                    pt = wintypes.POINT(); GetCursorPos(ctypes.byref(pt))
                    r = wintypes.RECT(); GetWindowRect(h, ctypes.byref(r))
                    x, y = pt.x, pt.y
                    L = x < r.left + BORDER; R = x >= r.right - BORDER
                    T = y < r.top + BORDER;  B = y >= r.bottom - BORDER
                    if T and L: return HTTOPLEFT
                    if T and R: return HTTOPRIGHT
                    if B and L: return HTBOTTOMLEFT
                    if B and R: return HTBOTTOMRIGHT
                    if L: return HTLEFT
                    if R: return HTRIGHT
                    if T: return HTTOP
                    if B: return HTBOTTOM
                    return HTCLIENT
                if msg == WM_NCACTIVATE:
                    return CallWindowProcW(old_proc[0], h, msg, wp, -1)
                return CallWindowProcW(old_proc[0], h, msg, wp, lp)

            self._new_wndproc = WNDPROC(py_wndproc)
            old = set_long(hwnd, GWLP_WNDPROC, ctypes.cast(self._new_wndproc, ctypes.c_void_p).value)
            old_proc[0] = old; self._old_wndproc = old
            SWP_NOSIZE=0x0001; SWP_NOMOVE=0x0002; SWP_NOZORDER=0x0004; SWP_FRAMECHANGED=0x0020
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOSIZE|SWP_NOMOVE|SWP_NOZORDER|SWP_FRAMECHANGED)
            GWLP_HWNDPARENT = -8
            try: set_long(hwnd, GWLP_HWNDPARENT, 0)
            except Exception: pass
            SW_HIDE=0; SW_SHOW=5
            ctypes.windll.user32.ShowWindow(hwnd, SW_HIDE)
            ctypes.windll.user32.ShowWindow(hwnd, SW_SHOW)
            try:
                DWMWA_WINDOW_CORNER_PREFERENCE=33; DWMWCP_DONOTROUND=1
                pref = ctypes.c_int(DWMWCP_DONOTROUND)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(pref), ctypes.sizeof(pref))
            except Exception: pass
            try:
                class MARGINS(ctypes.Structure):
                    _fields_ = [('cxLeftWidth',ctypes.c_int),('cxRightWidth',ctypes.c_int),('cyTopHeight',ctypes.c_int),('cyBottomHeight',ctypes.c_int)]
                m = MARGINS(0,0,0,0)
                ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
            except Exception: pass
        except Exception:
            try: self.root.overrideredirect(True)
            except Exception: self._frameless = False

    def _build_window_controls(self, parent):
        controls = tk.Frame(parent, bg='#181714')
        controls.pack(side='right', fill='y')
        cfg = dict(bg='#181714', fg='#8a8a84', bd=0, relief='flat',
                   activebackground='#22221f', activeforeground='#ffffff',
                   font=('Yu Gothic UI', 10), cursor='hand2', takefocus=0)
        def _hover(b, bg_):
            b.bind('<Enter>', lambda e: b.config(bg=bg_, fg='#ffffff'))
            b.bind('<Leave>', lambda e: b.config(bg='#181714', fg='#8a8a84'))
        def _square(text, cmd, hover_bg):
            w = tk.Frame(controls, bg='#181714', width=40, height=40)
            w.pack(side='left', fill='y'); w.pack_propagate(False)
            b = tk.Button(w, text=text, command=cmd, **cfg)
            b.pack(fill='both', expand=True); _hover(b, hover_bg)
            return b
        _square('\u2013', self._win_minimize, '#2c2c28')
        self._max_btn = _square('\u25a2', self._win_toggle_max, '#2c2c28')
        _square('\u2715', self._win_close, '#c42b1c')
        def _square_up(event):
            h = event.height
            if h <= 0: return
            for child in controls.winfo_children():
                if isinstance(child, tk.Frame): child.config(width=h)
        controls.bind('<Configure>', _square_up)

    def _make_titlebar_draggable(self, widget):
        def _recurse(w):
            if not isinstance(w, tk.Button):
                w.bind('<ButtonPress-1>',   self._on_title_press)
                w.bind('<B1-Motion>',       self._on_title_drag)
                w.bind('<ButtonRelease-1>', self._on_title_release)
                w.bind('<Double-Button-1>', lambda e: self._win_toggle_max())
            for c in w.winfo_children(): _recurse(c)
        _recurse(widget)
        self.root.after(50, self._install_resize_grips)

    def _install_resize_grips(self):
        GRIP = 2
        self._grip_frames = {}
        for name, cursor in [('n','sb_v_double_arrow'),('s','sb_v_double_arrow'),
                              ('w','sb_h_double_arrow'),('e','sb_h_double_arrow')]:
            f = tk.Frame(self.root, bg='#111110', width=GRIP, height=GRIP, cursor=cursor)
            f.place_forget(); self._grip_frames[name] = f
        for name, cursor in [('nw','size_nw_se'),('ne','size_ne_sw'),('sw','size_ne_sw'),('se','size_nw_se')]:
            f = tk.Frame(self.root, bg='#181714', width=GRIP, height=GRIP, cursor=cursor)
            self._grip_frames[name] = f
        def _layout(_=None):
            W = self.root.winfo_width(); H = self.root.winfo_height(); G = GRIP
            try:
                import ctypes as _ct
                hwnd = getattr(self, '_hwnd', None)
                zoomed = bool(hwnd and _ct.windll.user32.IsZoomed(hwnd))
            except Exception: zoomed = False
            if zoomed:
                for f in self._grip_frames.values(): f.place_forget()
                return
            self._grip_frames['n'].place(x=G,y=0,width=W-2*G,height=G)
            self._grip_frames['s'].place(x=G,y=H-G,width=W-2*G,height=G)
            self._grip_frames['w'].place(x=0,y=G,width=G,height=H-2*G)
            self._grip_frames['e'].place(x=W-G,y=G,width=G,height=H-2*G)
            self._grip_frames['nw'].place(x=0,y=0,width=G,height=G)
            self._grip_frames['ne'].place(x=W-G,y=0,width=G,height=G)
            self._grip_frames['sw'].place(x=0,y=H-G,width=G,height=G)
            self._grip_frames['se'].place(x=W-G,y=H-G,width=G,height=G)
            for f in self._grip_frames.values(): f.lift()
        self.root.bind('<Configure>', _layout, add='+'); _layout()
        for name, f in self._grip_frames.items():
            f.bind('<ButtonPress-1>', lambda e, n=name: self._on_resize_press(e, n))
            f.bind('<B1-Motion>',     lambda e, n=name: self._on_resize_drag(e, n))

    _HT_BY_EDGE = {'n':12,'s':15,'w':10,'e':11,'nw':13,'ne':14,'sw':16,'se':17}

    def _on_resize_press(self, event, edge):
        import sys
        if sys.platform == 'win32':
            try:
                import ctypes
                GA_ROOT=2; WM_NCLBUTTONDOWN=0x00A1; ht=self._HT_BY_EDGE.get(edge)
                hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
                if not hwnd: hwnd = getattr(self,'_hwnd',None)
                if hwnd and ht is not None:
                    lparam = (event.y_root&0xFFFF)<<16|(event.x_root&0xFFFF)
                    ctypes.windll.user32.ReleaseCapture()
                    ctypes.windll.user32.PostMessageW(hwnd, WM_NCLBUTTONDOWN, ht, lparam)
                    return
            except Exception: pass
        self._resize_start = (event.x_root,event.y_root,self.root.winfo_x(),self.root.winfo_y(),self.root.winfo_width(),self.root.winfo_height())
        self._resize_pending = False; self._resize_target = None

    def _on_resize_drag(self, event, edge):
        import sys
        if sys.platform == 'win32': return
        sx,sy,ox,oy,ow,oh = self._resize_start
        dx=event.x_root-sx; dy=event.y_root-sy; x,y,w,h=ox,oy,ow,oh; MIN_W,MIN_H=600,400
        if 'e' in edge: w=max(MIN_W,ow+dx)
        if 's' in edge: h=max(MIN_H,oh+dy)
        if 'w' in edge: new_w=max(MIN_W,ow-dx); x=ox+(ow-new_w); w=new_w
        if 'n' in edge: new_h=max(MIN_H,oh-dy); y=oy+(oh-new_h); h=new_h
        self._resize_target=(w,h,x,y)
        if not self._resize_pending: self._resize_pending=True; self.root.after_idle(self._apply_resize)

    def _apply_resize(self):
        self._resize_pending = False
        if self._resize_target is None: return
        w,h,x,y = self._resize_target
        try: self.root.geometry(f'{w}x{h}+{x}+{y}')
        except Exception: pass

    def _on_title_press(self, event):
        import sys
        if sys.platform == 'win32':
            try:
                import ctypes
                GA_ROOT=2; WM_NCLBUTTONDOWN=0x00A1; HTCAPTION=2
                hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), GA_ROOT)
                if not hwnd: hwnd = getattr(self,'_hwnd',None)
                if hwnd:
                    lparam=(event.y_root&0xFFFF)<<16|(event.x_root&0xFFFF)
                    ctypes.windll.user32.ReleaseCapture()
                    ctypes.windll.user32.PostMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, lparam)
                    return
            except Exception: pass
        self._drag_offset=(event.x_root-self.root.winfo_x(), event.y_root-self.root.winfo_y())
        self._drag_pending=False; self._drag_target=None

    def _on_title_drag(self, event):
        import sys
        if sys.platform == 'win32' and getattr(self,'_hwnd',None): return
        if self._is_maximized: self._win_toggle_max(); w=self.root.winfo_width(); self._drag_offset=(w//2,20)
        dx,dy=self._drag_offset; self._drag_target=(event.x_root-dx, event.y_root-dy)
        if not self._drag_pending: self._drag_pending=True; self.root.after_idle(self._apply_drag)

    def _apply_drag(self):
        self._drag_pending=False
        if self._drag_target is None: return
        x,y=self._drag_target
        try: self.root.geometry(f'+{x}+{y}')
        except Exception: pass

    def _on_title_release(self, event):
        zone=getattr(self,'_snap_zone',None)
        if getattr(self,'_snap_preview',None) is not None:
            try: self._snap_preview.destroy()
            except Exception: pass
            self._snap_preview=None
        if zone is None: return
        left,top,right,bottom=self._get_work_area(); w=right-left; h=bottom-top
        if zone=='max':
            self._normal_geometry=self.root.geometry(); self.root.geometry(f'{w}x{h}+{left}+{top}')
            self._is_maximized=True
            try: self._max_btn.config(text='\u2750')
            except Exception: pass
        elif zone=='left': self.root.geometry(f'{w//2}x{h}+{left}+{top}')
        elif zone=='right': self.root.geometry(f'{w//2}x{h}+{left+w//2}+{top}')
        self._snap_zone=None

    def _get_work_area(self):
        import sys
        if sys.platform=='win32' and getattr(self,'_hwnd',None):
            try:
                import ctypes
                from ctypes import wintypes
                class MONITORINFO(ctypes.Structure):
                    _fields_=[('cbSize',wintypes.DWORD),('rcMonitor',wintypes.RECT),('rcWork',wintypes.RECT),('dwFlags',wintypes.DWORD)]
                MONITOR_DEFAULTTONEAREST=2
                hmon=ctypes.windll.user32.MonitorFromWindow(self._hwnd,MONITOR_DEFAULTTONEAREST)
                info=MONITORINFO(); info.cbSize=ctypes.sizeof(MONITORINFO)
                if ctypes.windll.user32.GetMonitorInfoW(hmon,ctypes.byref(info)):
                    r=info.rcWork; return (r.left,r.top,r.right,r.bottom)
            except Exception: pass
        return (0,0,self.root.winfo_screenwidth(),self.root.winfo_screenheight())

    def _win_minimize(self):
        import sys
        if sys.platform=='win32':
            try:
                import ctypes
                hwnd=getattr(self,'_hwnd',None) or ctypes.windll.user32.GetParent(self.root.winfo_id())
                ctypes.windll.user32.ShowWindow(hwnd,6); return
            except Exception: pass
        try: self.root.iconify()
        except Exception: pass

    def _win_toggle_max(self):
        import sys
        if sys.platform=='win32':
            try:
                import ctypes
                hwnd=getattr(self,'_hwnd',None) or ctypes.windll.user32.GetParent(self.root.winfo_id())
                SW_MAXIMIZE=3; SW_RESTORE=9
                ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE if self._is_maximized else SW_MAXIMIZE)
                self._is_maximized=not self._is_maximized
                try: self._max_btn.config(text='\u25a2' if self._is_maximized else '\u2750')
                except Exception: pass
                return
            except Exception: pass
        if self._is_maximized:
            if self._normal_geometry: self.root.geometry(self._normal_geometry)
            self._is_maximized=False
            try: self._max_btn.config(text='\u25a2')
            except Exception: pass
            return
        self._normal_geometry=self.root.geometry()
        sw=self.root.winfo_screenwidth(); sh=self.root.winfo_screenheight()
        self.root.geometry(f'{sw}x{sh}+0+0'); self._is_maximized=True
        try: self._max_btn.config(text='\u2750')
        except Exception: pass

    def _win_close(self):
        self._closing = True
        self._stop_udp_listener()
        self.is_monitoring = False
        self._restart_pending = False
        self._dispose_graph_figure()
        if self.logging:
            self._stop_logging(source="Application exit")
        elif self._labview_flare_active:
            self._stop_labview_tab_flare()
        self._serial_worker.shutdown()
        try: self.root.destroy()
        except Exception: pass

    # ------------------------------------------------------------------ #
    #  Styles                                                              #
    # ------------------------------------------------------------------ #
    def _setup_styles(self):
        s = ttk.Style()
        s.theme_use('clam')
        s.configure('TButton', background='#efe9dc', foreground='#1a1a18',
                    borderwidth=0, font=('Yu Gothic UI', 9), padding=(14, 8), relief='flat')
        s.map('TButton', background=[('active','#e1dac8'),('disabled','#2c2c28')],
              foreground=[('disabled','#555555')])
        s.configure('Dark.TButton', background='#22221f', foreground='#efe9dc',
                    borderwidth=0, font=('Yu Gothic UI', 9), padding=(14, 8))
        s.map('Dark.TButton', background=[('active','#2c2c28'),('disabled','#1a1a18')],
              foreground=[('disabled','#555555')])
        s.configure('Scan.TButton', background='#f25d38', foreground='#ffffff',
                    borderwidth=0, font=('Yu Gothic UI', 9, 'bold'), padding=(14, 8))
        s.map('Scan.TButton', background=[('active','#d94f2c'),('disabled','#2c2c28')],
              foreground=[('disabled','#555')])
        s.configure('Stop.TButton', background='#b91c1c', foreground='white',
                    borderwidth=0, font=('Yu Gothic UI', 9, 'bold'), padding=8)
        s.map('Stop.TButton', background=[('active','#991b1b')])
        s.configure('Monitor.TButton', background='#15803d', foreground='white',
                    borderwidth=0, font=('Yu Gothic UI', 9, 'bold'), padding=8)
        s.map('Monitor.TButton', background=[('active','#166534')])
        s.configure('Set.TButton', background='#b45309', foreground='white',
                    borderwidth=0, font=('Yu Gothic UI', 8, 'bold'), padding=(6, 3))
        s.map('Set.TButton', background=[('active','#92400e')])
        s.configure('Accent.TButton', background='#f25d38', foreground='#ffffff',
                    borderwidth=0, font=('Yu Gothic UI', 9, 'bold'), padding=(14, 8))
        s.map('Accent.TButton', background=[('active','#d94f2c'),('disabled','#2c2c28')],
              foreground=[('disabled','#555')])
        s.configure('Secondary.TButton', background='#2c2c28', foreground='#efe9dc',
                    borderwidth=0, font=('Yu Gothic UI', 9, 'bold'), padding=(12, 7))
        s.map('Secondary.TButton',
              background=[('active','#3a3a34'),('disabled','#1a1a18')],
              foreground=[('disabled','#555555')])
        s.configure('FuelZero.TButton', background='#9a3412', foreground='white',
                    borderwidth=0, font=('Yu Gothic UI', 9, 'bold'), padding=(12, 7))
        s.map('FuelZero.TButton',
              background=[('active','#c2410c'),('disabled','#2c2c28')],
              foreground=[('disabled','#555555')])
        s.configure('Danger.TButton', background='#7f1d1d', foreground='white',
                    borderwidth=0, font=('Yu Gothic UI', 9, 'bold'), padding=(12, 7))
        s.map('Danger.TButton',
              background=[('active','#b91c1c'),('disabled','#2c2c28')],
              foreground=[('disabled','#555555')])
        s.configure('Compact.TButton', background='#2c2c28', foreground='#efe9dc',
                    borderwidth=0, font=('Yu Gothic UI', 8), padding=(8, 5))
        s.map('Compact.TButton', background=[('active','#3a3a34')])
        s.configure('Graph.TCheckbutton', background='#111110', foreground='#d8d2c3',
                    font=('Yu Gothic UI', 8), indicatorcolor='#22221f', padding=(3,2))
        s.map('Graph.TCheckbutton',
              background=[('active','#111110'),('disabled','#111110')],
              foreground=[('disabled','#555555')],
              indicatorcolor=[('selected','#f25d38'),('!selected','#22221f')])
        s.configure('Ready.TButton', background='#b45309', foreground='white',
                    borderwidth=0, font=('Yu Gothic UI', 9, 'bold'), padding=8)
        s.map('Ready.TButton', background=[('active','#92400e'),('disabled','#2c2c28')],
              foreground=[('disabled','#555')])
        s.configure('Ignite.TButton', background='#dc2626', foreground='white',
                    borderwidth=0, font=('Yu Gothic UI', 9, 'bold'), padding=8)
        s.map('Ignite.TButton', background=[('active','#b91c1c'),('disabled','#2c2c28')],
              foreground=[('disabled','#555')])
        s.configure('TEntry', fieldbackground='#1a1a18', foreground='#efe9dc',
                    insertcolor='#f25d38', borderwidth=0, relief='flat', padding=6)
        s.map('TEntry', fieldbackground=[('focus','#22221f'),('disabled','#181714')],
              foreground=[('disabled','#555555')])
        s.configure('TCombobox', fieldbackground='#1a1a18', foreground='#efe9dc',
                    background='#1a1a18', selectbackground='#22221f',
                    selectforeground='#efe9dc', borderwidth=0, relief='flat',
                    padding=6, arrowcolor='#8a8a84')
        s.map('TCombobox', fieldbackground=[('readonly','#1a1a18'),('disabled','#181714')],
              foreground=[('readonly','#efe9dc'),('disabled','#555555')],
              selectbackground=[('readonly','#1a1a18')])
        s.layout('Vertical.TScrollbar',[('Vertical.Scrollbar.trough',{'sticky':'ns','children':[('Vertical.Scrollbar.thumb',{'expand':'1','sticky':'nswe'})]})])
        s.layout('Horizontal.TScrollbar',[('Horizontal.Scrollbar.trough',{'sticky':'we','children':[('Horizontal.Scrollbar.thumb',{'expand':'1','sticky':'nswe'})]})])
        s.configure('TScrollbar', background='#3a3a34', troughcolor='#0d0d0d',
                    borderwidth=0, relief='flat', gripcount=0)
        s.map('TScrollbar', background=[('active','#555550'),('pressed','#666660')])
        s.configure('TProgressbar', background='#f25d38', troughcolor='#22221f',
                    borderwidth=0, relief='flat')
        s.configure('TPanedwindow', background='#111110')
        s.configure('Sash', sashcolor='#3a3a34', sashthickness=5, handlesize=0)

    # ------------------------------------------------------------------ #
    #  UI helpers                                                          #
    # ------------------------------------------------------------------ #
    _BG_PANEL = '#111110'

    def _font(self, size=8, family='Yu Gothic UI', bold=False, italic=False):
        mods = []
        if bold:   mods.append('bold')
        if italic: mods.append('italic')
        return (family, size, *mods) if mods else (family, size)

    def _dlabel(self, parent, text, *, fg='#d8d2c3', bg=None,
                size=8, family='Yu Gothic UI', bold=False, italic=False, **kw):
        return tk.Label(parent, text=text,
                        bg=self._BG_PANEL if bg is None else bg, fg=fg,
                        font=self._font(size, family, bold, italic), **kw)

    def _drow(self, parent, *, bg=None, **frame_kw):
        return tk.Frame(parent, bg=self._BG_PANEL if bg is None else bg, **frame_kw)

    def _scrollable_column(self, parent):
        """Return a vertically scrollable frame sized to its containing pane."""
        shell = tk.Frame(parent, bg=self._BG_PANEL)
        canvas = tk.Canvas(shell, bg=self._BG_PANEL, highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(shell, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        inner = tk.Frame(canvas, bg=self._BG_PANEL)
        window_id = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind(
            '<Configure>',
            lambda _event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind(
            '<Configure>',
            lambda event: canvas.itemconfigure(window_id, width=event.width))

        def _wheel(event):
            if not event.delta:
                return
            steps = int(-event.delta / 120)
            if steps == 0:
                steps = -1 if event.delta > 0 else 1
            canvas.yview_scroll(steps, 'units')
            return 'break'

        def _bind_wheel_tree(widget):
            # Text widgets retain their own independent scrolling behaviour.
            if not isinstance(widget, tk.Text):
                widget.bind('<MouseWheel>', _wheel, add='+')
            for child in widget.winfo_children():
                _bind_wheel_tree(child)

        self.root.after_idle(lambda: _bind_wheel_tree(inner))
        return shell, inner

    def _field_grid(self, parent, fields, *, bg=None, entry_width=7,
                    label_fg='#999999', label_size=8, entry_font=('Consolas', 9)):
        bg = self._BG_PANEL if bg is None else bg
        for label, attr, default, r, c in fields:
            tk.Label(parent, text=label, bg=bg, fg=label_fg,
                     font=self._font(label_size)).grid(row=r, column=c, sticky='e', padx=(4,2), pady=2)
            e = ttk.Entry(parent, width=entry_width, font=entry_font)
            e.insert(0, default)
            e.grid(row=r, column=c+1, sticky='w', padx=(0,6), pady=2)
            setattr(self, attr, e)

    def _scrolled_text(self, parent, *, height=5, wrap='word',
                       bg='#0d0d0d', fg='#00cc44', font=('Courier', 8),
                       insertbackground=None):
        vsb = ttk.Scrollbar(parent, orient='vertical')
        vsb.pack(side='right', fill='y')
        opts = dict(height=height, font=font, wrap=wrap, yscrollcommand=vsb.set,
                    bg=bg, fg=fg, relief='flat', borderwidth=0)
        if insertbackground is not None:
            opts['insertbackground'] = insertbackground
        txt = tk.Text(parent, **opts)
        txt.pack(side='left', fill='both', expand=True)
        vsb.config(command=txt.yview)
        return txt

    def _estop_btn(self, parent, *, height=1, **extra):
        return tk.Button(parent, text="ZERO ALL FLOWS",
                         command=self._zero_all,
                         bg='#7f0000', fg='white',
                         activebackground='#cc0000', activeforeground='white',
                         font=('Yu Gothic UI', 10, 'bold'),
                         relief='flat', height=height, **extra)

    # ------------------------------------------------------------------ #
    #  Top-level UI                                                        #
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        hdr = tk.Frame(self.root, bg='#181714')
        hdr.pack(fill='x')
        self._titlebar = hdr
        self._build_window_controls(hdr)
        tk.Label(hdr, text=f"Alicat Flow Controller  v{APP_VERSION}", bg='#181714', fg='#efe9dc',
                 font=('Yu Gothic UI', 12, 'normal')).pack(side='left', padx=16, pady=8)
        tk.Label(hdr, text="Multi-Gas Control  \u2022  Live Monitoring  \u2022  Logging",
                 bg='#181714', fg='#6e6e68', font=('Yu Gothic UI', 9)).pack(side='left', padx=6, pady=8)
        self._make_titlebar_draggable(hdr)
        self.notebook = ZenTabs(self.root, bg='#111110', font=('Yu Gothic UI', 11),
                                tab_pad_x=24, tab_gap=8, strip_pad=(12, 8, 12, 0))
        self.notebook.pack(fill='both', expand=True)
        self.tab_conn = tk.Frame(self.notebook, bg='#111110')
        self.tab_op   = tk.Frame(self.notebook, bg='#111110')
        self.tab_log  = tk.Frame(self.notebook, bg='#111110')
        self.notebook.add(self.tab_conn, text='Connection & Assignment')
        self.notebook.add(self.tab_op,   text='Operation & Monitoring')
        self._graph_tab_index = self.notebook.add(
            self.tab_log, text='Logging & Graphs')
        self.notebook.bind('<<TabChanged>>', self._on_tab_changed, add='+')
        safety_button = dict(
            fg='white', activeforeground='white', disabledforeground='#555550',
            font=('Yu Gothic UI', 9, 'bold'), relief='flat', bd=0,
            state='disabled', cursor='arrow')
        self._strip_zero_all = tk.Button(
            self.notebook._strip, text="ZERO ALL", command=self._zero_all,
            bg='#2a2a26', activebackground='#b91c1c', **safety_button)
        self._strip_zero_all.pack(
            side='right', padx=(4, 12), pady=6, ipadx=12, ipady=4)
        self._strip_zero_fuel = tk.Button(
            self.notebook._strip, text="ZERO FUEL", command=self._zero_fuel,
            bg='#2a2a26', activebackground='#c2410c', **safety_button)
        self._strip_zero_fuel.pack(
            side='right', padx=4, pady=6, ipadx=12, ipady=4)
        self._strip_reconnect = tk.Button(
            self.notebook._strip, text="\u27f3 Reconnect Flow Meters",
            command=self._restart_connection, bg='#2a2a26', fg='#555550',
            activebackground='#3a3a34', activeforeground='white',
            disabledforeground='#555550', font=('Yu Gothic UI', 8, 'bold'),
            relief='flat', bd=0, state='disabled', cursor='arrow')
        self._strip_reconnect.pack(
            side='right', padx=(4, 8), pady=6, ipadx=8, ipady=4)
        self._build_connection_tab()
        self._build_operation_tab()
        self._build_logging_tab()

    # ================================================================== #
    #  TAB 1 — Connection & Assignment                                    #
    # ================================================================== #
    def _build_connection_tab(self):
        paned = tk.PanedWindow(self.tab_conn, orient='horizontal',
                               bg='#3a3a34', sashwidth=5, sashrelief='flat',
                               sashpad=0, opaqueresize=False, borderwidth=0, relief='flat')
        paned.pack(fill='both', expand=True, padx=8, pady=8)
        left_shell, left = self._scrollable_column(paned)
        right = tk.Frame(paned, bg='#111110')
        paned.add(left_shell, stretch='never'); paned.add(right, stretch='always')
        def _sash_once_t1(event, _done=[False]):
            if _done[0] or event.width <= 1: return
            _done[0] = True; self._set_sashpos(paned, 0, 1/3)
        paned.bind('<Configure>', _sash_once_t1)
        self._build_step1(left); self._build_step2(left)
        self._build_step3(left); self._build_step4(left)
        mon = RoundedPanel(right, text="Live Monitor (Tab 1)", padx=8, pady=8, bg='#111110')
        mon.pack(fill='both', expand=True, padx=8, pady=8)
        self.grid_container_tab1 = self._drow(mon.body)
        self.grid_container_tab1.pack(fill='both', expand=True)
        self._dlabel(self.grid_container_tab1, "Connect and start monitoring\nto view live readings.",
                     fg='#444444', family='Consolas', size=11, justify='center').pack(pady=80)

    def _set_estop_armed(self, armed):
        state = 'normal' if armed else 'disabled'
        cursor = 'hand2' if armed else 'arrow'
        self._strip_zero_fuel.config(
            bg='#9a3412' if armed else '#2a2a26',
            fg='white' if armed else '#555550', state=state, cursor=cursor)
        self._strip_zero_all.config(
            bg='#7f1d1d' if armed else '#2a2a26',
            fg='white' if armed else '#555550', state=state, cursor=cursor)
        self._strip_reconnect.config(
            bg='#2c2c28' if armed else '#2a2a26',
            fg='#efe9dc' if armed else '#555550', state=state, cursor=cursor)
        if hasattr(self, 'reconnect_btn'):
            self.reconnect_btn.config(state=state)

    def _set_sashpos(self, paned, idx, pos):
        try:
            if isinstance(pos, float): pos = int(paned.winfo_width() * pos)
            paned.sash_place(idx, pos, 0)
        except tk.TclError:
            self.root.after(50, lambda: self._set_sashpos(paned, idx, pos))

    def _build_step1(self, p):
        f = RoundedPanel(p, text="Step 1: COM Port", padx=12, pady=12, bg='#111110')
        f.pack(fill='x', padx=8, pady=8)
        row = self._drow(f.body); row.pack(fill='x')
        self._dlabel(row, "Port:", size=9, bold=True).pack(side='left', padx=5)
        self.port_combo = ttk.Combobox(row, width=10, font=('Consolas', 9))
        self.port_combo.pack(side='left', padx=5); self.port_combo.set("COM3")
        ttk.Button(row, text="Refresh", command=self._refresh_ports).pack(side='left', padx=8)
        self._dlabel(row, "Baud:", size=9, bold=True).pack(side='left', padx=(18,5))
        self.baud_combo = ttk.Combobox(
            row, width=9, state='readonly', font=('Consolas', 9),
            values=('2400','9600','19200','38400','57600','115200'))
        self.baud_combo.set(str(self.serial_baudrate))
        self.baud_combo.pack(side='left', padx=5)
        self.baud_combo.bind('<<ComboboxSelected>>', self._on_baud_changed)
        self._dlabel(row, "must match all devices", fg='#777777', italic=True).pack(side='left', padx=5)

    def _build_step2(self, p):
        f = RoundedPanel(p, text="Step 2: Scan Units A\u2013Z", padx=12, pady=12, bg='#111110')
        f.pack(fill='x', padx=8, pady=4)
        row = self._drow(f.body); row.pack(fill='x', pady=4)
        self.scan_btn = ttk.Button(row, text="Scan A\u2013Z", command=self._start_scan, style='Scan.TButton')
        self.scan_btn.pack(side='left', padx=4)
        self.scan_status = self._dlabel(row, "Ready to scan", fg='#666666')
        self.scan_status.pack(side='left', padx=12)
        self.progress = ttk.Progressbar(
            f.body, mode='determinate', maximum=len(self.SCAN_UNITS))
        self.progress.pack(fill='x', pady=4)
        self._dlabel(f.body, "Detected Controllers:", fg='#aaaaaa', bold=True).pack(anchor='w', pady=(6,2))
        rc = self._drow(f.body); rc.pack(fill='both', expand=True)
        self.scan_log = self._scrolled_text(rc, height=7, wrap='none', insertbackground='white')
        self.scan_log.insert('1.0', "No scan yet. Click 'Scan A\u2013Z' to begin.")
        self.scan_log.config(state='disabled')

    def _build_step3(self, p):
        f = RoundedPanel(p, text="Step 3: Assign Controllers", padx=12, pady=12, bg="#111110")
        f.pack(fill="x", padx=8, pady=4)
        selector_row = self._drow(f.body); selector_row.pack(fill="x", pady=(0,6))
        self._dlabel(
            selector_row,
            "Click a controller row to include/exclude it; excluded rows are greyed out.",
            fg="#d97706", italic=True).pack(side="left")
        self.clear_selection_btn = ttk.Button(
            selector_row, text="Clear", command=lambda: self._set_all_controller_selection(False))
        self.clear_selection_btn.pack(side="right", padx=(4,0))
        self.select_all_btn = ttk.Button(
            selector_row, text="Select All", command=lambda: self._set_all_controller_selection(True))
        self.select_all_btn.pack(side="right", padx=(4,0))
        self.assign_frame = self._drow(f.body); self.assign_frame.pack(fill="x")
        for col, hdr in enumerate(("Unit","Gas (scan)","Flow (scan)","Gas Type","Zone")):
            self._dlabel(self.assign_frame, hdr, fg="#888888", bold=True).grid(
                row=0, column=col, padx=8, pady=3, sticky="w")
        self.assign_combos = {}; self.assign_info = {}
        self.assign_row_visuals = {}; self._custom_gases = []
        self.assignment_controls_locked = False
        self._dlabel(self.assign_frame, "Scan first to see units here.",
                     fg="#444444", italic=True).grid(row=1, column=0, columnspan=5, padx=8, pady=8, sticky="w")

    def _populate_assign_rows(self):
        for w in self.assign_frame.grid_slaves():
            if int(w.grid_info()["row"]) >= 1: w.destroy()
        self.assign_combos = {}; self.assign_info = {}; self.assign_row_visuals = {}
        for idx, ctrl in enumerate(self.detected_controllers, start=1):
            unit = ctrl.unit
            gas = ctrl.active_gas
            gas_options = ctrl.gas_options()
            flow = ctrl.data.get("mass_flow",0)
            row_bg = tk.Frame(self.assign_frame, bg="#111110", cursor="hand2")
            row_bg.grid(row=idx, column=0, columnspan=5, sticky="nsew", pady=1)
            unit_lbl = self._dlabel(self.assign_frame, f"Unit {unit}", fg="#7dd3fc", family="Consolas", size=9, bold=True)
            unit_lbl.config(cursor="hand2")
            unit_lbl.grid(row=idx, column=0, padx=8, pady=3, sticky="w")
            gl = self._dlabel(self.assign_frame, gas, fg="#4ade80")
            gl.config(cursor="hand2"); gl.grid(row=idx, column=1, padx=8, pady=3, sticky="w")
            fl = self._dlabel(self.assign_frame, f"{flow:.2f} SLPM", fg="#cccccc")
            fl.config(cursor="hand2"); fl.grid(row=idx, column=2, padx=8, pady=3, sticky="w")
            gas_cb = ttk.Combobox(self.assign_frame, values=gas_options, width=14, state="readonly", font=("Consolas",9))
            gas_cb.set(gas if gas.casefold() != "unknown" else "-- select --")
            gas_cb.grid(row=idx, column=3, padx=8, pady=3)
            gas_cb.bind("<<ComboboxSelected>>", lambda e, u=unit: self._on_gas_type_change(u))
            zone_cb = ttk.Combobox(self.assign_frame, values=self.ZONE_OPTIONS, width=14, state="readonly", font=("Consolas",9))
            zone_cb.set("General"); zone_cb.grid(row=idx, column=4, padx=8, pady=3)
            zone_cb.bind("<<ComboboxSelected>>", lambda e, u=unit: self._on_zone_change(u))
            enabled_var = tk.BooleanVar(value=True)
            self.assign_combos[unit] = {
                "gas_type": gas_cb, "zone": zone_cb,
                "enabled_var": enabled_var,
            }
            self.assign_info[unit]   = {"gas": gl, "flow": fl}
            labels = [unit_lbl, gl, fl]
            self.assign_row_visuals[unit] = {
                "background": row_bg,
                "labels": [(label, label.cget("fg")) for label in labels],
                "hover_widgets": [row_bg, *labels, gas_cb, zone_cb],
                "click_widgets": [row_bg, *labels],
            }
            for widget in self.assign_row_visuals[unit]["hover_widgets"]:
                widget.bind(
                    "<Enter>", lambda _event, u=unit: self._paint_assign_row(u, True),
                    add="+")
                widget.bind(
                    "<Leave>", lambda _event, u=unit: self._paint_assign_row(u, False),
                    add="+")
            for widget in self.assign_row_visuals[unit]["click_widgets"]:
                widget.bind(
                    "<Button-1>",
                    lambda _event, u=unit: self._toggle_controller_selection(u),
                    add="+")
            row_bg.lower()
            self._paint_assign_row(unit, False)
        self._rebuild_assignments()

    def _set_all_controller_selection(self, enabled):
        for unit, controls in self.assign_combos.items():
            controls["enabled_var"].set(bool(enabled))
            self._paint_assign_row(unit, False)
        self._rebuild_assignments()

    def _toggle_controller_selection(self, unit):
        if self.assignment_controls_locked:
            return
        enabled_var = self.assign_combos[unit]["enabled_var"]
        enabled_var.set(not enabled_var.get())
        self._paint_assign_row(unit, False)
        self._rebuild_assignments()

    def _paint_assign_row(self, unit, hover=False):
        controls = self.assign_combos.get(unit)
        visuals = self.assign_row_visuals.get(unit)
        if not controls or not visuals:
            return
        enabled = controls["enabled_var"].get()
        bg = "#1e293b" if hover else ("#111110" if enabled else "#181817")
        visuals["background"].config(bg=bg)
        for label, normal_fg in visuals["labels"]:
            label.config(bg=bg, fg=normal_fg if enabled else "#666660")

    def _set_assignment_controls_locked(self, locked):
        self.assignment_controls_locked = bool(locked)
        combo_state = "disabled" if locked else "readonly"
        for controls in self.assign_combos.values():
            controls["gas_type"].config(state=combo_state)
            controls["zone"].config(state=combo_state)
        button_state = "disabled" if locked else "normal"
        self.select_all_btn.config(state=button_state)
        self.clear_selection_btn.config(state=button_state)

    def _selected_controller_rows(self):
        """Yield only assignment rows included in the active configuration."""
        for unit, controls in self.assign_combos.items():
            if controls["enabled_var"].get():
                yield unit, controls

    def _on_gas_type_change(self, unit):
        self._rebuild_assignments()

    def _on_zone_change(self, unit):
        self._rebuild_assignments()

    def _add_custom_gas(self, unit, cb):
        cb.set("-- select --")
        port = self.port_combo.get()
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Available Gases \u2014 Unit {unit}")
        dialog.configure(bg="#111110"); dialog.resizable(True,True)
        dialog.grab_set(); dialog.lift(); dialog.attributes("-topmost",True); dialog.geometry("360x440")
        hdr = tk.Frame(dialog, bg="#1e3a5f"); hdr.pack(fill="x")
        tk.Label(hdr, text=f"Unit {unit}  \u2014  Controller Gas Table", bg="#1e3a5f", fg="#7dd3fc",
                 font=("Yu Gothic UI",10,"bold")).pack(side="left", padx=12, pady=8)
        self._gas_dialog_status = tk.Label(hdr, text="Querying\u2026", bg="#1e3a5f", fg="#fbbf24",
                 font=("Yu Gothic UI",8,"italic"))
        self._gas_dialog_status.pack(side="right", padx=10)
        frow = tk.Frame(dialog, bg="#141414", pady=6); frow.pack(fill="x", padx=10)
        tk.Label(frow, text="Filter:", bg="#141414", fg="#666666", font=("Yu Gothic UI",8)).pack(side="left", padx=(0,6))
        filter_var = tk.StringVar()
        filter_entry = ttk.Entry(frow, textvariable=filter_var, width=22, font=("Consolas",9)); filter_entry.pack(side="left")
        tk.Button(frow, text="\u2715", bg="#141414", fg="#555555", relief="flat", bd=0, cursor="hand2",
                  font=("Yu Gothic UI",9), command=lambda: filter_var.set("")).pack(side="left", padx=4)
        lf = tk.Frame(dialog, bg="#111110"); lf.pack(fill="both", expand=True, padx=10, pady=(0,6))
        vsb = ttk.Scrollbar(lf, orient="vertical"); vsb.pack(side="right", fill="y")
        listbox = tk.Listbox(lf, yscrollcommand=vsb.set, bg="#0d0d0d", fg="#4ade80",
                             selectbackground="#1e3a5f", selectforeground="#7dd3fc",
                             font=("Consolas",9), relief="flat", bd=0, activestyle="none", height=16)
        listbox.pack(side="left", fill="both", expand=True); vsb.config(command=listbox.yview)
        btn_row = tk.Frame(dialog, bg="#111110"); btn_row.pack(pady=(0,10))
        select_btn = ttk.Button(btn_row, text="Select Gas", style="Accent.TButton", state="disabled")
        select_btn.pack(side="left", padx=6)
        ttk.Button(btn_row, text="Cancel", command=lambda: _cancel()).pack(side="left", padx=6)
        _all_gases = []
        def _populate(gases_dict):
            _all_gases.clear()
            for idx2, name in sorted(gases_dict.items(), key=lambda x: x[1]):
                _all_gases.append((idx2, name))
            self._gas_dialog_status.config(text=f"{len(_all_gases)} gases found", fg="#4ade80")
            _apply_filter(); filter_entry.focus()
        def _apply_filter(*_):
            query = filter_var.get().strip().lower(); listbox.delete(0, tk.END)
            for idx2, name in _all_gases:
                if not query or query in name.lower() or query in str(idx2):
                    listbox.insert(tk.END, f"  {name:<18}  idx {idx2:>3}")
            select_btn.config(state="normal" if listbox.size()>0 else "disabled")
        filter_var.trace_add("write", _apply_filter)
        def _commit():
            sel = listbox.curselection()
            if not sel: return
            name = listbox.get(sel[0]).strip().split()[0]
            if name not in self._custom_gases and name not in self.BASE_GAS_TYPES:
                self._custom_gases.append(name)
            new_opts = self.BASE_GAS_TYPES + self._custom_gases + ["+ Add new gas..."]
            for u, cbs in self.assign_combos.items(): cbs["gas_type"]["values"] = new_opts
            cb.set(name); dialog.destroy(); self._rebuild_assignments()
        def _cancel():
            cb.set("-- select --"); dialog.destroy()
        def _on_error(msg):
            self._gas_dialog_status.config(text=f"Error: {msg}", fg="#f87171")
            listbox.pack_forget(); vsb.pack_forget(); filter_entry.config(state="disabled")
            manual_frame = tk.Frame(dialog, bg="#111110"); manual_frame.pack(fill="x", padx=10, pady=8)
            tk.Label(manual_frame, text="Could not reach controller. Enter gas name manually:",
                     bg="#111110", fg="#fbbf24", font=("Yu Gothic UI",8,"italic")).pack(anchor="w")
            manual_entry = ttk.Entry(manual_frame, width=22, font=("Consolas",10))
            manual_entry.pack(pady=(4,0)); manual_entry.focus()
            def _manual_commit():
                name = manual_entry.get().strip()
                if not name: return
                if name not in self._custom_gases and name not in self.BASE_GAS_TYPES:
                    self._custom_gases.append(name)
                new_opts = self.BASE_GAS_TYPES + self._custom_gases + ["+ Add new gas..."]
                for u, cbs in self.assign_combos.items(): cbs["gas_type"]["values"] = new_opts
                cb.set(name); dialog.destroy(); self._rebuild_assignments()
            manual_entry.bind("<Return>", lambda e: _manual_commit())
            select_btn.config(command=_manual_commit, state="normal", text="Add Gas")
        select_btn.config(command=_commit)
        listbox.bind("<Double-Button-1>", lambda e: _commit())
        listbox.bind("<Return>", lambda e: _commit())
        def _finish_fetch(future):
            try:
                gases = future.result()
                if not gases: raise ValueError("No gases returned \u2014 check port/unit")
                _populate(gases)
            except Exception as exc:
                _on_error(str(exc))
        if self.is_monitoring:
            _on_error("Stop live monitoring before querying the controller gas table")
        else:
            try:
                self._submit_serial(
                    self._query_gases_async(port, unit), _finish_fetch)
            except Exception as exc:
                _on_error(str(exc))

    def _rebuild_assignments(self):
        self.assignments = {key: None for key, _ in self.ROLES}
        self._custom_assignments = {}
        for unit, cbs in self._selected_controller_rows():
            g = cbs["gas_type"].get(); z = cbs["zone"].get()
            if g in ("-- select --","") or z == "-- unassigned --": continue
            rk = self.ROLE_MAP.get((g, z))
            if rk: self.assignments[rk] = unit
            else: self._custom_assignments[unit] = f"custom_{unit}"
        if hasattr(self, '_graph_series_container'):
            self._refresh_graph_series_controls()
            self._schedule_graph_rebuild()

    def _check_autocalc_compatible(self):
        pairs = []
        for unit, cbs in self._selected_controller_rows():
            g = cbs["gas_type"].get(); z = cbs["zone"].get()
            if g not in ("-- select --","") and z != "-- unassigned --": pairs.append((g, z))
        self._autocalc_config, problems = assess_autocalc(pairs)
        return problems

    def _build_step4(self, p):
        f = RoundedPanel(p, text="Step 4: Connect Selected & Monitor", padx=12, pady=12, bg='#111110')
        f.pack(fill='x', padx=8, pady=4)
        row = self._drow(f.body); row.pack(fill='x', pady=4)
        self.connect_btn = ttk.Button(
            row, text="Connect Selected", command=self._connect_all, style='Accent.TButton')
        self.connect_btn.pack(side='left', padx=4)
        self.disconnect_btn = ttk.Button(
            row, text="Disconnect", command=self._disconnect_all, state='disabled')
        self.disconnect_btn.pack(side='left', padx=4)
        self.reconnect_btn = ttk.Button(
            row, text="Reconnect", command=self._restart_connection,
            style='Secondary.TButton', state='disabled')
        self.reconnect_btn.pack(side='left', padx=4)
        self.conn_status = self._dlabel(row, "Not connected", fg='#ff4444', size=9, bold=True)
        self.conn_status.pack(side='left', padx=16)
        self.monitor_btn = ttk.Button(f.body, text="Start Live Monitor",
                                      command=self._toggle_monitoring, style='Monitor.TButton',
                                      state='disabled')
        self.monitor_btn.pack(fill='x', pady=6)
        self._dlabel(f.body, "Connection Log:", fg='#8a8a84', bold=True).pack(anchor='w', pady=(6,2))
        lc = self._drow(f.body); lc.pack(fill='both', expand=True)
        self.conn_log = self._scrolled_text(lc, height=5, fg='#d8d2c3')


    # ================================================================== #
    #  TAB 2 — Operation & Monitoring                                     #
    # ================================================================== #
    def _build_operation_tab(self):
        paned = tk.PanedWindow(self.tab_op, orient='horizontal', bg='#3a3a34',
                               sashwidth=5, sashrelief='flat', sashpad=0,
                               opaqueresize=False, borderwidth=0, relief='flat')
        paned.pack(fill='both', expand=True, padx=8, pady=8)
        left_shell, left = self._scrollable_column(paned)
        right = tk.Frame(paned, bg='#111110')
        paned.add(left_shell, stretch='never'); paned.add(right, stretch='always')
        def _sash_once(event, _done=[False]):
            if _done[0] or event.width <= 1: return
            _done[0] = True; self._set_sashpos(paned, 0, 1/3)
        paned.bind('<Configure>', _sash_once)
        self._build_logging_control(left)
        self._build_autocalc(left); self._build_ignition(left)
        self._build_restart_connection(left); self._build_system_ctrl(left)
        self._build_syslog(left)
        self._build_live_grid_tab2(right); self._build_combustion_panel(right)

    def _build_logging_control(self, p):
        f = RoundedPanel(
            p, text="Logging & Acquisition", bg='#111110', padx=8, pady=6,
            collapsible=True)
        f.pack(fill='x', padx=8, pady=(8,4))

        path_row = self._drow(f.body); path_row.pack(fill='x', pady=(0,4))
        self._dlabel(path_row, "Log file:", fg='#999999').pack(
            side='left', padx=(0,6))
        self.log_path_entry = ttk.Entry(path_row, width=34, font=('Consolas',8))
        self.log_path_entry.insert(0, self._log_destination)
        self.log_path_entry.pack(side='left', fill='x', expand=True, padx=(0,5))
        self.log_browse_btn = ttk.Button(
            path_row, text="Browse\u2026", command=self._choose_log_file)
        self.log_browse_btn.pack(side='left')

        row = self._drow(f.body); row.pack(fill='x', pady=(0,4))
        self.start_logging_btn = ttk.Button(
            row, text="Start Logging", command=self._start_logging)
        self.start_logging_btn.pack(side='left', padx=(0,4))
        self.stop_logging_btn = ttk.Button(
            row, text="Stop Logging", command=self._stop_logging, state='disabled')
        self.stop_logging_btn.pack(side='left', padx=4)
        self.log_status_lbl = self._dlabel(row, "Logging: OFF", fg='#555555')
        self.log_status_lbl.pack(side='left', padx=8)

        udp_row = self._drow(f.body); udp_row.pack(fill='x', pady=(2,4))
        self._dlabel(udp_row, "LabVIEW UDP port:", fg='#999999').pack(
            side='left', padx=(0,6))
        self.udp_port_entry = ttk.Entry(udp_row, width=7, font=('Consolas',9))
        self.udp_port_entry.insert(0, str(self._udp_port))
        self.udp_port_entry.pack(side='left', padx=(0,5))
        self.udp_port_apply_btn = ttk.Button(
            udp_row, text="Apply", command=self._apply_udp_port)
        self.udp_port_apply_btn.pack(side='left')
        self.udp_port_entry.bind('<Return>', lambda _event: self._apply_udp_port())
        self.udp_status_lbl = self._dlabel(
            udp_row, f"LabVIEW UDP: starting {self._udp_host}:{self._udp_port}",
            fg='#fbbf24', family='Consolas', size=8)
        self.udp_status_lbl.pack(side='right', padx=(8,0))

        poll_row = self._drow(f.body); poll_row.pack(fill='x', pady=(2,4))
        self._dlabel(
            poll_row, "Extra pass delay (ms):", fg='#999999').pack(
                side='left', padx=(0,6))
        self.poll_interval_entry = ttk.Entry(
            poll_row, width=7, font=('Consolas',9))
        self.poll_interval_entry.insert(0, "0")
        self.poll_interval_entry.pack(side='left', padx=(0,5))
        ttk.Button(
            poll_row, text="Apply", command=self._apply_poll_interval).pack(
                side='left')
        self.poll_interval_entry.bind(
            '<Return>', lambda _event: self._apply_poll_interval())
        self.poll_rate_lbl = self._dlabel(
            poll_row, "Effective: stopped", fg='#60a5fa',
            family='Consolas', size=8)
        self.poll_rate_lbl.pack(side='right', padx=(8,0))
        self._dlabel(
            f.body,
            "Leave delay at 0 normally. LabVIEW logs use a timestamped copy "
            "of the selected file.",
            fg='#555555', size=7, italic=True).pack(anchor='w', pady=(0,2))

    def _build_autocalc(self, p):
        f = RoundedPanel(
            p, text="1  Auto-Calculate Flows", bg='#111110', padx=8, pady=6,
            collapsible=True)
        f.pack(fill='x', padx=8, pady=(8,4))
        ig = self._drow(f.body); ig.pack(fill='x')
        ig.columnconfigure(1, weight=1); ig.columnconfigure(3, weight=1)
        self._field_grid(ig, [
            ("Power (kW):",               "total_power_entry", "10",    0, 0),
            ("H2 Percentage (%):",        "h2_frac_entry",     "30",    0, 2),
            ("Stage 1 Power Split (%):",  "fuel_split_entry",  "99.99", 1, 0),
            ("\u03c6 Stage 1:",           "phi_rich_entry",    "1.1",   1, 2),
            ("\u03c6 Global:",            "phi_global_entry",  "0.6",   2, 0),
        ])
        self.autocalc_btn = ttk.Button(f.body,
            text="Calculate & Store Targets  (no flows sent)",
            command=self._auto_calc, style='Accent.TButton')
        self.autocalc_btn.pack(fill='x', pady=(6,2))
        self.calc_status = self._dlabel(f.body, "No targets stored yet.",
                                        fg='#666666', size=9, italic=True, anchor='w')
        self.calc_status.pack(fill='x')
        sf = self._drow(f.body); sf.pack(fill='x', pady=(4,0))
        self.stored_flow_labels = {}; self._stored_flow_cells = []
        sf_defs = [('NH3-1','nh3_rich'),('H2-1','h2_rich'),('NH3-2','nh3_lean'),
                   ('H2-2','h2_lean'),('Air1','rich_air'),('Air2','lean_air')]
        for i, (abbr, key) in enumerate(sf_defs):
            r, c = divmod(i, 4)
            cell = RoundedPanel(sf, bg='#22221f', radius=8, padx=6, pady=3,
                                border='#3a3a34', parent_bg='#111110')
            cell.grid(row=r, column=c, padx=2, pady=2, sticky='nsew')
            sf.columnconfigure(c, weight=1)
            self._dlabel(cell.body, abbr, bg='#22221f', fg='#7a736a', size=7).pack()
            lbl = self._dlabel(cell.body, "--", bg='#22221f', fg='#d97706',
                               family='Consolas', size=10, bold=True)
            lbl.pack()
            self.stored_flow_labels[key] = lbl
            self._stored_flow_cells.append(cell)

    def _build_ignition(self, p):
        f = RoundedPanel(
            p, text="2  Ignition Sequence", bg='#111110', padx=8, pady=6,
            collapsible=True)
        f.pack(fill='x', padx=8, pady=4)
        banner_wrap = RoundedPanel(f.body, bg='#2c2c28', radius=8, padx=10, pady=6,
                                   border='#3a3a34', parent_bg='#111110')
        banner_wrap.pack(fill='x', pady=(0,6))
        self.state_banner = tk.Label(banner_wrap.body,
            text="IDLE \u2014 calculate targets first",
            bg='#2c2c28', fg='#8a8a84', font=('Consolas', 9, 'bold'), anchor='center')
        self.state_banner.pack(fill='x')
        _banner_config = self.state_banner.configure
        def _banner_sync(*args, **kw):
            res = _banner_config(*args, **kw)
            new_bg = kw.get('bg') or kw.get('background')
            if new_bg: banner_wrap.set_card_bg(new_bg)
            return res
        self.state_banner.configure = _banner_sync
        self.state_banner.config    = _banner_sync
        pg = self._drow(f.body); pg.pack(fill='x')
        pg.columnconfigure(1, weight=1); pg.columnconfigure(3, weight=1)
        self._field_grid(pg, [
            ("Fuel pre-ign (%):", "pre_fuel_entry", "80",  0, 0),
            ("Air  pre-ign (%):", "pre_air_entry",  "80",  0, 2),
            ("Ramp steps:",       "ramp_steps",     "10",  1, 0),
            ("Step interval(s):", "ramp_interval",  "0.5", 1, 2),
        ], entry_width=6)
        info_row = self._drow(f.body); info_row.pack(fill='x', pady=(2,6))
        self.phi_preview = self._dlabel(info_row, "\u03c6 ratio: 1.00x", fg='#4ecdc4')
        self.phi_preview.pack(side='left', padx=4)
        self.ramp_time_lbl = self._dlabel(info_row, "total: 5.0 s", fg='#666666')
        self.ramp_time_lbl.pack(side='right', padx=4)
        self.pre_fuel_entry.bind('<KeyRelease>', self._update_phi_preview)
        self.pre_air_entry.bind( '<KeyRelease>', self._update_phi_preview)
        self.ramp_steps.bind(    '<KeyRelease>', self._update_ramp_time)
        self.ramp_interval.bind( '<KeyRelease>', self._update_ramp_time)
        self.ready_btn = ttk.Button(f.body,
            text="STEP 1 \u2014 Pre-Ignition  (set scaled flows)",
            command=self._ready_ignition, style='Ready.TButton')
        self.ready_btn.pack(fill='x', pady=(0,2))
        self.ignite_btn = ttk.Button(f.body,
            text="STEP 2 \u2014 Ignite  (ramp to target flows)",
            command=self._ignite, style='Ignite.TButton', state='disabled')
        self.ignite_btn.pack(fill='x', pady=2)

        # ---- NEW BUTTONS (correct standalone placement) ----
        # Stage stored targets into card SP entry fields without sending any flows.
        self.stage_btn = ttk.Button(f.body,
            text="Stage Targets to SP Fields  (no flows sent)",
            command=self._stage_targets_to_entries, style='Dark.TButton')
        self.stage_btn.pack(fill='x', pady=(8,2))

        # Read each card's SP entry and batch-send all setpoints together.
        self.set_all_btn = ttk.Button(f.body,
            text="Set All Flows Together  (send every card's SP)",
            command=self._set_all_flows_together, style='Accent.TButton')
        self.set_all_btn.pack(fill='x', pady=2)
        # ---- END NEW BUTTONS ----

        self.abort_btn = ttk.Button(f.body,
            text="Zero All Flows  (monitoring continues)",
            command=self._abort, style='Danger.TButton')
        self.abort_btn.pack(fill='x', pady=(2,0))
        self.state_lbl = self.state_banner

    def _build_restart_connection(self, p):
        f = RoundedPanel(
            p, text="Connection Recovery", bg='#111110', padx=8, pady=6,
            collapsible=True)
        f.pack(fill='x', padx=8, pady=4)
        self._dlabel(
            f.body,
            "Close and reopen meter connections while preserving assignments and setpoints.",
            fg='#666666', size=7, italic=True, wraplength=360,
            justify='left').pack(anchor='w', pady=(0,5))
        ttk.Button(
            f.body, text="\u27f3  Reconnect Flow Meters",
            command=self._restart_connection,
            style='Secondary.TButton').pack(fill='x')
        self.restart_status_lbl = self._dlabel(f.body, "", fg='#4ade80', family='Consolas', anchor='center')
        self.restart_status_lbl.pack(fill='x', pady=(3,0))

    def _restart_connection(self):
        if not self.controllers_connected:
            messagebox.showerror("Reconnect Flow Meters", "Connect the controllers first.")
            return
        if (self.is_connecting or self._emergency_stop_active
                or self._zero_action_active or self._restart_pending):
            messagebox.showerror(
                "Reconnect Flow Meters",
                "Wait for the current serial operation to finish.")
            return
        self._log("Reconnect Flow Meters triggered by user.")
        self.restart_status_lbl.config(text="Reconnecting\u2026", fg='#fb923c')
        self._reconnect_active = True
        self._set_estop_armed(False)
        self._restart_pending = True
        self._restart_reason = "manual"
        self.is_monitoring = False
        self._cancel_live_ui_refresh()
        if self._monitor_future is None or self._monitor_future.done():
            self._begin_serial_restart()

    def _build_system_ctrl(self, p):
        f = RoundedPanel(
            p, text="3  System Safety", bg='#111110', padx=8, pady=6,
            collapsible=True)
        f.pack(fill='x', padx=8, pady=4)
        sf = RoundedPanel(f.body, text="Safety Discrepancy", bg='#111110', padx=6, pady=4)
        sf.pack(fill='x', pady=(4,6))
        sr1 = self._drow(sf.body); sr1.pack(fill='x', pady=(0,3))
        self._disc_enabled_var = tk.BooleanVar(value=True)
        tk.Checkbutton(sr1, text="Enable discrepancy monitoring",
                       variable=self._disc_enabled_var, command=self._on_disc_toggle,
                       bg='#111110', fg='#d8d2c3', selectcolor='#2c2c28',
                       activebackground='#111110', activeforeground='#ffffff',
                       font=('Yu Gothic UI',8)).pack(side='left')
        sr2 = self._drow(sf.body); sr2.pack(fill='x')
        self._dlabel(sr2, "Max discrepancy (%):", fg='#999999').pack(side='left', padx=(0,6))
        self.disc_threshold_entry = ttk.Entry(sr2, width=6, font=('Consolas',9))
        self.disc_threshold_entry.insert(0,"5"); self.disc_threshold_entry.pack(side='left', padx=(0,10))
        self._dlabel(sr2, "Suppress repeat (s):", fg='#999999').pack(side='left', padx=(0,6))
        self.disc_suppress_entry = ttk.Entry(sr2, width=5, font=('Consolas',9))
        self.disc_suppress_entry.insert(0,"30"); self.disc_suppress_entry.pack(side='left')
        self.disc_status_lbl = self._dlabel(sf.body, "\u25cf All flows nominal",
                                            fg='#4ade80', family='Consolas', anchor='w')
        self.disc_status_lbl.pack(fill='x', pady=(4,0))

    def _build_syslog(self, p):
        f = RoundedPanel(
            p, text="System Log", bg='#111110', padx=4, pady=4,
            collapsible=True, collapsed=True)
        f.pack(fill='x', padx=8, pady=(4,8))
        self.sys_log = self._scrolled_text(f.body, height=5)
        self._log("System initialised.")

    def _build_live_grid_tab2(self, p):
        f = RoundedPanel(p, text="Live Controller Readings & Manual Control",
                          bg='#111110', padx=8, pady=6)
        f.pack(fill='both', expand=True, padx=(8,0), pady=(8,4))
        self.grid_container_tab2 = self._drow(f.body)
        self.grid_container_tab2.pack(fill='both', expand=True)
        self._dlabel(self.grid_container_tab2, "Start monitoring to view live readings.",
                     fg='#444444', family='Consolas', size=10).pack(pady=50)

    def _build_combustion_panel(self, p):
        f = RoundedPanel(p, text="Combustion  (NH3/H2/CH4 \u2014 CH4 pilot included in \u03c6)",
                          bg='#111110', padx=6, pady=4)
        f.pack(fill='x', padx=(8,0), pady=(0,8))
        row = self._drow(f.body); row.pack(fill='x')
        def _flow_cell(parent, abbrev, attr, color, *, label_fg, label_size, val_size, pack_padx, pack_ipadx):
            cell = tk.Frame(parent, bg='#232323', relief='flat')
            cell.pack(side='left', padx=pack_padx, pady=2, ipadx=pack_ipadx, ipady=3)
            self._dlabel(cell, abbrev, bg='#232323', fg=label_fg, size=label_size).pack()
            lbl = self._dlabel(cell, "0.000", bg='#232323', fg=color, family='Consolas', size=val_size, bold=True)
            lbl.pack(); setattr(self, attr, lbl)
        for abbrev, attr, color in [
            ("NH3-1",'lbl_nh3_rich','#e2e8f0'), ("H2-1",'lbl_h2_rich','#e2e8f0'),
            ("NH3-2",'lbl_nh3_lean','#e2e8f0'), ("H2-2",'lbl_h2_lean','#e2e8f0'),
            ("CH4",'lbl_ch4_pilot','#e2e8f0'),  ("Air-1",'lbl_rich_air','#93c5fd'),
            ("Air-2",'lbl_lean_air','#93c5fd'),  ("TOTAL",'lbl_total_fuel','#fbbf24'),
        ]:
            _flow_cell(row, abbrev, attr, color, label_fg='#555555', label_size=7,
                       val_size=9, pack_padx=3, pack_ipadx=6)
        tk.Frame(row, bg='#333333', width=1).pack(side='left', fill='y', padx=10, pady=2)
        for abbrev, attr, color in [
            ("\u03c6 S1",'lbl_phi_rich','#fb923c'),
            ("\u03c6 Global",'lbl_phi_global','#34d399'),
        ]:
            _flow_cell(row, abbrev, attr, color, label_fg='#666666', label_size=8,
                       val_size=15, pack_padx=6, pack_ipadx=10)


    def _make_card(self, container, key, gas_name, unit, card_store,
                   hdr_bg="#1e3a5f", hdr_fg="#7dd3fc", sec_bg="#0f1e30"):
        card = tk.Frame(container, bg='#1e1e1e', relief='flat', bd=0,
                        highlightbackground='#333333', highlightthickness=1)
        card.pack(fill='x', padx=4, pady=3)
        id_blk = tk.Frame(card, bg=hdr_bg, width=160); id_blk.pack(side='left', fill='y')
        id_blk.pack_propagate(False)
        tk.Label(id_blk, text=gas_name, bg=hdr_bg, fg=hdr_fg,
                 font=('Yu Gothic UI',9,'bold')).pack(anchor='w', padx=8, pady=(5,0))
        tk.Label(id_blk, text=f"Unit {unit}", bg=hdr_bg, fg=hdr_fg,
                 font=('Consolas',7)).pack(anchor='w', padx=8, pady=(0,5))
        widgets = {}
        readings = [
            ('Flow','flow','#4ade80',('Consolas',13,'bold'),'SLPM'),
            ('SP','setpoint','#64748b',('Consolas',9),'SLPM'),
            ('Press','pressure','#64748b',('Consolas',8),'psia'),
            ('Temp','temp','#64748b',('Consolas',8),'\u00b0C'),
        ]
        for lbl_txt, field, color, font, unit_txt in readings:
            blk = tk.Frame(card, bg='#1e1e1e'); blk.pack(side='left', padx=10, pady=6)
            tk.Label(blk, text=lbl_txt, bg='#1e1e1e', fg='#374151', font=('Yu Gothic UI',7)).pack(anchor='w')
            row_inner = tk.Frame(blk, bg='#1e1e1e'); row_inner.pack(anchor='w')
            vl = tk.Label(row_inner, text='0.000', bg='#1e1e1e', fg=color, font=font, anchor='e')
            vl.pack(side='left')
            tk.Label(row_inner, text=f" {unit_txt}", bg='#1e1e1e', fg='#374151',
                     font=('Yu Gothic UI',7)).pack(side='left', pady=(2,0))
            widgets[field] = vl
        tk.Frame(card, bg='#2c2c28', width=1).pack(side='left', fill='y', pady=4)
        sp_blk = tk.Frame(card, bg='#1c1c1c'); sp_blk.pack(side='left', padx=8, pady=4)
        tk.Label(sp_blk, text='SP', bg='#1c1c1c', fg='#d97706', font=('Yu Gothic UI',7,'bold')).pack(anchor='w')
        sp_row = tk.Frame(sp_blk, bg='#1c1c1c'); sp_row.pack()
        entry = ttk.Entry(sp_row, width=8, font=('Consolas',9)); entry.insert(0,'0'); entry.pack(side='left', padx=(0,3))
        entry.bind('<Return>', lambda event, k=key, e=entry: self._manual_set(k, e))
        ttk.Button(sp_row, text='Set', command=lambda k=key, e=entry: self._manual_set(k, e),
                   style='Set.TButton').pack(side='left')
        widgets['entry'] = entry; widgets['card'] = card; card_store[unit] = widgets
        _ramp_color_map = {
            'ch4_pilot':('#1a1a2e','#555577'), 'rich_air':('#0f1e30','#4a6a8a'),
            'lean_air':('#0a200a','#3a6a3a'),  'nh3_rich':('#2a1a1a','#c97070'),
            'h2_rich':('#2a2010','#c9a050'),   'nh3_lean':('#1a2a1a','#70c970'),
            'h2_lean':('#1a2020','#70c9c9'),
        }
        rb, rf_fg = _ramp_color_map.get(key, ('#111110','#8a8a84'))
        tk.Frame(card, bg='#2c2c28', width=1).pack(side='left', fill='y', pady=4)
        rf = tk.Frame(card, bg=rb); rf.pack(side='left', padx=8, pady=4)
        tk.Label(rf, text=f'{gas_name} Ramp', bg=rb, fg=rf_fg, font=('Yu Gothic UI',7)).pack(anchor='w')
        ramp_row = tk.Frame(rf, bg=rb); ramp_row.pack()
        tk.Label(ramp_row, text='Steps:', bg=rb, fg=rf_fg, font=('Yu Gothic UI',7)).pack(side='left')
        steps_entry = ttk.Entry(ramp_row, width=4, font=('Consolas',8)); steps_entry.insert(0,'1'); steps_entry.pack(side='left', padx=(2,6))
        tk.Label(ramp_row, text='Int(s):', bg=rb, fg=rf_fg, font=('Yu Gothic UI',7)).pack(side='left')
        interval_entry = ttk.Entry(ramp_row, width=4, font=('Consolas',8)); interval_entry.insert(0,'0.5'); interval_entry.pack(side='left', padx=(2,0))
        ramp_bar = ttk.Progressbar(rf, mode='determinate', maximum=100, length=110); ramp_bar.pack(pady=(2,0))
        widgets['ramp_steps'] = steps_entry; widgets['ramp_interval'] = interval_entry; widgets['ramp_bar'] = ramp_bar
        if key == 'ch4_pilot':
            self.ch4_ramp_steps = steps_entry; self.ch4_ramp_interval = interval_entry; self.ch4_ramp_bar = ramp_bar
        tk.Frame(card, bg='#2c2c28', width=1).pack(side='left', fill='y', pady=4)
        pid_f = tk.Frame(card, bg='#111110'); pid_f.pack(side='left', padx=6, pady=4)
        tk.Label(pid_f, text='PID', bg='#111110', fg='#444444', font=('Yu Gothic UI',7,'bold')).pack(anchor='w')
        pid_row1 = tk.Frame(pid_f, bg='#111110'); pid_row1.pack(anchor='w')
        for pid_lbl, pid_w in [('P',5),('I',5),('D',5)]:
            tk.Label(pid_row1, text=f'{pid_lbl}:', bg='#111110', fg='#444444', font=('Yu Gothic UI',7)).pack(side='left')
            e = ttk.Entry(pid_row1, width=pid_w, font=('Consolas',8)); e.insert(0,'\u2014'); e.pack(side='left', padx=(1,4))
            widgets[f'pid_{pid_lbl.lower()}'] = e
        pid_row2 = tk.Frame(pid_f, bg='#111110'); pid_row2.pack(anchor='w', pady=(2,0))
        loop_var = tk.StringVar(value='PD/PDF')
        loop_cb = ttk.Combobox(pid_row2, textvariable=loop_var, values=['PD/PDF','PD2I'],
                               width=7, state='readonly', font=('Consolas',7))
        loop_cb.pack(side='left', padx=(0,3))
        widgets['pid_loop_var'] = loop_var; widgets['pid_loop_cb'] = loop_cb
        ttk.Button(pid_row2, text='Read', command=lambda k=key, u=unit, w=widgets: self._pid_read(k,u,w),
                   style='Set.TButton').pack(side='left', padx=(0,2))
        ttk.Button(pid_row2, text='Set', command=lambda k=key, u=unit, w=widgets: self._pid_write(k,u,w),
                   style='Set.TButton').pack(side='left')

    def _ch4_start_ramp(self, target=None): self._start_ramp('ch4_pilot', target)
    def _ch4_stop_ramp(self): self._ramp_active['ch4_pilot'] = False

    def _pid_read(self, key, unit, widgets):
        if not self.is_monitoring or not unit or unit not in self.controller_instances:
            messagebox.showerror('PID', f'Unit {unit} is not connected.'); return
        for f in ('pid_p','pid_i','pid_d'):
            try: widgets[f].delete(0,tk.END); widgets[f].insert(0,'\u2026')
            except Exception: pass
        async def _read():
            return await self.controller_instances[unit].get_pid()
        def _finish_read(future):
            try:
                pid = future.result()
                widgets['pid_p'].delete(0,tk.END); widgets['pid_p'].insert(0,str(pid.get('P','')))
                widgets['pid_i'].delete(0,tk.END); widgets['pid_i'].insert(0,str(pid.get('I','')))
                widgets['pid_d'].delete(0,tk.END); widgets['pid_d'].insert(0,str(pid.get('D','')))
                lt = pid.get('loop_type','PD/PDF')
                widgets['pid_loop_var'].set(lt if lt in ('PD/PDF','PD2I') else 'PD/PDF')
                self._log(f'PID read Unit {unit}: {pid}')
            except Exception as exc:
                messagebox.showerror('PID Read Error',f'Unit {unit}: {exc}')
                for field in ('pid_p','pid_i','pid_d'):
                    widgets[field].delete(0,tk.END); widgets[field].insert(0,'err')
        try:
            self._submit_serial(_read(), _finish_read)
        except Exception as exc:
            messagebox.showerror('PID Read Error', f'Unit {unit}: {exc}')
            for field in ('pid_p','pid_i','pid_d'):
                widgets[field].delete(0,tk.END); widgets[field].insert(0,'err')

    def _pid_write(self, key, unit, widgets):
        if not self.is_monitoring or not unit or unit not in self.controller_instances:
            messagebox.showerror('PID', f'Unit {unit} is not connected.'); return
        try:
            p_str = widgets['pid_p'].get().strip(); i_str = widgets['pid_i'].get().strip(); d_str = widgets['pid_d'].get().strip()
            loop_type = widgets['pid_loop_var'].get()
            p = int(p_str) if p_str not in ('','\u2014','\u2026','err') else None
            i = int(i_str) if i_str not in ('','\u2014','\u2026','err') else None
            d = int(d_str) if d_str not in ('','\u2014','\u2026','err') else None
        except ValueError: messagebox.showerror('PID','P, I and D must be integers.'); return
        if p is None and i is None and d is None:
            messagebox.showwarning('PID','No values to write \u2014 read first or enter P/I/D.'); return
        async def _write():
            await self.controller_instances[unit].set_pid(
                p=p, i=i, d=d, loop_type=loop_type)
        def _finish_write(future):
            try:
                future.result()
                parts = []
                if p is not None: parts.append(f'P={p}')
                if i is not None: parts.append(f'I={i}')
                if d is not None: parts.append(f'D={d}')
                parts.append(f'loop={loop_type}')
                self._log(f'PID set Unit {unit}: {", ".join(parts)} \u2713')
            except Exception as exc:
                messagebox.showerror('PID Write Error',f'Unit {unit}: {exc}')
        try:
            self._submit_serial(_write(), _finish_write)
        except Exception as exc:
            messagebox.showerror('PID Write Error',f'Unit {unit}: {exc}')

    def _start_ramp(self, key, target):
        if self._ramp_active.get(key): return
        unit = self.assignments.get(key)
        if not unit and key.startswith('custom_'): unit = key[len('custom_'):]
        store_key = unit if unit else key
        steps_w = interval_w = bar_w = None
        for store in (self.cards_tab2, self.cards_tab1):
            if store_key in store:
                w = store[store_key]
                steps_w = w.get('ramp_steps'); interval_w = w.get('ramp_interval'); bar_w = w.get('ramp_bar')
                break
        try:
            steps = int(steps_w.get()) if steps_w else 1
            interval = float(interval_w.get()) if interval_w else 0.5
            if steps < 1 or interval < 0.05: raise ValueError
        except (ValueError, AttributeError):
            messagebox.showerror("Ramp","Steps \u2265 1,  Interval \u2265 0.05 s."); return
        self._ramp_active[key] = True
        if bar_w:
            try: bar_w.config(value=0)
            except Exception: pass
        _role_names = {k: v for k, v in self.ROLES}
        label = _role_names.get(key, key.replace('_',' ').title())
        self._log(f"{label} ramp \u2192 {target:.3f} SLPM  ({steps} steps \u00d7 {interval} s = {steps*interval:.1f} s total)")
        threading.Thread(target=self._ramp_thread, args=(key,unit,target,steps,interval,bar_w), daemon=True).start()

    def _ramp_thread(self, key, unit, target, steps, interval, bar_w):
        import time
        store_key = unit if unit else key; start = 0.0
        for store in (self.cards_tab2, self.cards_tab1):
            if store_key in store:
                try: start = float(store[store_key]['flow'].cget('text'))
                except Exception: pass
                break
        def _set_bar(pct):
            if bar_w:
                try: bar_w.config(value=pct)
                except Exception: pass
        try:
            for step in range(1, steps+1):
                if not self._ramp_active.get(key): break
                sp = start + (target-start)*(step/steps)
                if unit: self._queue_setpoint(unit, sp)
                self.root.after(0, lambda p=int(step/steps*100): _set_bar(p))
                time.sleep(interval)
            _role_names = {k: v for k, v in self.ROLES}
            label = _role_names.get(key, key.replace('_',' ').title())
            if self._ramp_active.get(key):
                if unit: self._queue_setpoint(unit, target)
                self._log(f"{label} ramp complete \u2192 {target:.3f} SLPM \u2713")
                self.root.after(0, lambda: _set_bar(100))
            else:
                self._log(f"{label} ramp stopped."); self.root.after(0, lambda: _set_bar(0))
        except Exception as e: self._log(f"Ramp error ({key}): {e}")
        finally: self._ramp_active[key] = False

    def _reset_ramp_active(self): self._ramp_active.clear()

    def _on_baud_changed(self, _event=None):
        """Apply the selected application baud when no connection is active."""
        try:
            selected = int(self.baud_combo.get())
        except (AttributeError, TypeError, ValueError):
            self.baud_combo.set(str(self.serial_baudrate))
            return
        if self.is_monitoring:
            self.baud_combo.set(str(self.serial_baudrate))
            messagebox.showwarning(
                "Serial Baud",
                "Stop live monitoring before changing baud rate.\n\n"
                "The selected baud must already match every Alicat on this COM port.")
            return
        self.serial_baudrate = selected
        self._log_conn(f"Application serial baud set to {selected}. Devices were not reconfigured.")

    def _refresh_ports(self):
        import serial.tools.list_ports
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo['values'] = ports
        if ports and not self.port_combo.get(): self.port_combo.set(ports[0])
        self._log_conn(f"Found {len(ports)} COM port(s).")

    def _start_scan(self):
        if self.is_scanning: return
        if (self.controllers_connected or self.is_monitoring or self.is_connecting
                or self._emergency_stop_active):
            messagebox.showerror(
                "Scan unavailable",
                "Disconnect first and wait for any connection or zero-flow work to finish.")
            return
        port = self.port_combo.get()
        if not port: messagebox.showerror("Error","Select a COM port first."); return
        self.is_scanning = True; self.detected_controllers = []
        self.scan_btn.config(state='disabled')
        self.scan_status.config(text="Scanning\u2026", fg='#d97706')
        self.progress['value'] = 0
        self.scan_log.config(state='normal'); self.scan_log.delete('1.0',tk.END)
        self.scan_log.insert('1.0',f"Scanning {port} for units A\u2013Z\u2026\n")
        self.scan_log.config(state='disabled')
        self._log_conn(f"Starting scan on {port}\u2026")
        baudrate = self.serial_baudrate
        self._log_conn(f"Scan baud: {baudrate} (must match every device on the port).")
        try:
            self._submit_serial(self._scan_async(port, baudrate))
        except Exception as exc:
            self._finish_scan(f"Could not start serial scan: {exc}")

    async def _scan_async(self, port, baudrate):
        def _on_progress(index, _unit):
            self._post_ui(self.progress.config, value=index)

        def _on_controller(controller):
            reading = controller.data
            gas = controller.active_gas
            flow = reading.get('mass_flow', 0)
            pressure = reading.get('pressure', 0)
            self._post_ui(
                self._append_scan_log,
                f"OK  {controller.unit}: {gas:<14} Flow={flow:>7.2f} SLPM  "
                f"P={pressure:>6.2f} psia\n")

        def _on_gas_progress(index, count, controller):
            self._post_ui(
                self.scan_status.config,
                text=(f"Reading gas table {index}/{count} "
                      f"(Unit {controller.unit})…"),
                fg='#d97706')

        result = await self._discovery_service.scan(
            port,
            baudrate,
            self.SCAN_UNITS,
            self.SCAN_RESPONSE_TIMEOUT_S,
            should_continue=lambda: self.is_scanning,
            on_progress=_on_progress,
            on_controller=_on_controller,
            on_gas_progress=_on_gas_progress,
        )
        self.detected_controllers = result.controllers
        for controller in result.controllers:
            supported = controller.supported_gases
            if supported:
                message = (
                    f"    Unit {controller.unit}: {len(supported)} "
                    "supported gases found.\n")
            else:
                message = (
                    f"    Unit {controller.unit}: gas table unavailable; "
                    "using the active gas only.\n")
            self._post_ui(self._append_scan_log, message)
        self._post_ui(self._finish_scan, result.error)

    def _append_scan_log(self, text):
        """Append scan output on Tk's main thread."""
        self.scan_log.config(state='normal')
        self.scan_log.insert(tk.END, text)
        self.scan_log.see(tk.END)
        self.scan_log.config(state='disabled')

    def _finish_scan(self, error=None):
        """Restore the scan UI after the worker releases the serial port."""
        found = len(self.detected_controllers)
        if error:
            self._append_scan_log(f"\nScan stopped: {error}\n")
            self.scan_status.config(text="Scan failed", fg='#f87171')
            self._log_conn(f"Scan failed after finding {found}: {error}")
        else:
            self._append_scan_log(f"\nScan complete: {found} found.\n")
            self.scan_status.config(text=f"Found {found} controllers.", fg='#4ade80')
            self._log_conn(f"Scan complete: {found} found.")
        self._populate_assign_rows()
        self.is_scanning = False
        self.scan_btn.config(state='normal')

    def _connect_all(self):
        if not self.assign_combos: messagebox.showerror("Error","No units detected. Run a scan first."); return
        if self.is_connecting:
            return
        if self.is_monitoring or self._emergency_stop_active:
            messagebox.showerror(
                "Connection unavailable",
                "Stop monitoring and wait for zero-flow work to finish first.")
            return
        self._rebuild_assignments(); problems = self._check_autocalc_compatible()
        if problems:
            bullet = chr(10).join("  "+chr(8226)+" "+p for p in problems)
            msg = ("Auto-calculation will NOT be available:\n\n"+bullet+"\n\nYou can still use manual setpoints. Continue?")
            if not messagebox.askyesno("Auto-calc Unavailable", msg, icon="warning"): return
            self._autocalc_available = False
        else: self._autocalc_available = True
        port = self.port_combo.get()
        if not port:
            messagebox.showerror("Error", "Select a COM port first.")
            return
        baudrate = self.serial_baudrate
        gas_map = {}
        for unit, cbs in self._selected_controller_rows():
            g = cbs["gas_type"].get()
            if g not in ("-- select --",""): gas_map[unit] = g
        units = self._assigned_units()
        if not units:
            messagebox.showerror(
                "Connection", "Select and assign at least one detected controller first.")
            return

        self.is_connecting = True
        self.controllers_connected = False
        self._set_assignment_controls_locked(True)
        self._set_estop_armed(False)
        self.conn_status.config(text="Connecting\u2026", fg="#fbbf24")
        self.connect_btn.config(state='disabled')
        self.disconnect_btn.config(state='disabled')
        self.monitor_btn.config(state='disabled')
        self._log_conn(
            f"Connecting to {len(units)} assigned unit(s) on {port} at {baudrate} baud\u2026")
        try:
            self._connection_future = self._submit_serial(
                self._configure_controllers_async(port, baudrate, units, gas_map),
                self._finish_connect_all)
        except Exception as exc:
            self.is_connecting = False
            self._set_assignment_controls_locked(False)
            self.connect_btn.config(state='normal')
            self.conn_status.config(text="Connection failed", fg="#f87171")
            messagebox.showerror("Connection failed", str(exc))

    def _assigned_units(self):
        units = {unit for unit in self.assignments.values() if unit}
        units.update(getattr(self, '_custom_assignments', {}).keys())
        return sorted(units)

    async def _configure_controllers_async(self, port, baudrate, units, gas_map):
        """Program requested gases, poll every unit, and return confirmed results."""
        confirmed = {}
        errors = {}
        for unit in units:
            gas_name = gas_map.get(unit)
            try:
                gas_index = None
                if gas_name:
                    supported = self._query_gases_serial(port, unit, baudrate)
                    if supported and gas_name not in supported.values():
                        supported_list = ", ".join(sorted(supported.values()))
                        raise ValueError(
                            f'"{gas_name}" is not in the gas table ({supported_list})')
                    gas_index = next(
                        (idx for idx, name in supported.items() if name == gas_name), None)
                    if gas_index is not None:
                        self._set_gas_serial(port, unit, gas_index, baudrate)

                async with FlowController(
                        address=port, unit=unit, baudrate=baudrate, timeout=0.3) as fc:
                    if gas_name and gas_index is None:
                        await asyncio.wait_for(fc.set_gas(gas_name), timeout=3.0)
                    reading = await asyncio.wait_for(fc.get(), timeout=2.0)

                actual_gas = str(reading.get('gas', '')).strip()
                if gas_name and actual_gas.casefold() != gas_name.casefold():
                    raise OSError(
                        f"gas readback mismatch: requested {gas_name}, got {actual_gas or 'unknown'}")
                confirmed[unit] = reading
            except Exception as exc:
                errors[unit] = f"{type(exc).__name__}: {exc}"
        return confirmed, errors

    def _finish_connect_all(self, future):
        self._connection_future = None
        self.is_connecting = False
        try:
            confirmed, errors = future.result()
        except Exception as exc:
            confirmed, errors = {}, {"connection": f"{type(exc).__name__}: {exc}"}

        for unit, reading in confirmed.items():
            gas = reading.get('gas', 'Unknown')
            self._log_conn(f"  Unit {unit}: communication and gas '{gas}' confirmed \u2713")
        for unit, error in errors.items():
            self._log_conn(f"  Unit {unit}: connection failed \u2014 {error}")

        if errors or len(confirmed) != len(self._assigned_units()):
            self.controllers_connected = False
            self._set_estop_armed(False)
            self.conn_status.config(text="Connection failed", fg="#f87171")
            self.connect_btn.config(state='normal')
            self.disconnect_btn.config(state='disabled')
            self.monitor_btn.config(state='disabled')
            self._set_assignment_controls_locked(False)
            details = "\n".join(f"Unit {unit}: {error}" for unit, error in errors.items())
            messagebox.showerror(
                "Connection failed",
                "Not every assigned controller could be confirmed.\n\n" + details)
            return

        self.controllers_connected = True
        self._set_estop_armed(True)
        self.conn_status.config(text="Connected (confirmed)", fg="#4ade80")
        self.connect_btn.config(state='disabled')
        self.disconnect_btn.config(state='normal')
        self.monitor_btn.config(state='normal')
        self._log_conn(f"All {len(confirmed)} selected controllers confirmed.")
        for key, unit in self.assignments.items():
            if unit:
                self._log_conn(f"  {key}: Unit {unit}")
        if hasattr(self, "autocalc_btn"):
            self.autocalc_btn.config(
                state="normal" if self._autocalc_available else "disabled")
        if hasattr(self, "calc_status"):
            if self._autocalc_available:
                mode_str = (
                    "Full RQL" if self._autocalc_config == "FULL_RQL"
                    else "Rich + quench-air")
                self.calc_status.config(
                    text=f"Ready ({mode_str}). Click Calculate to store targets.",
                    fg='#4ade80')
            else:
                self.calc_status.config(
                    text="\u26a0 Auto-calc disabled \u2014 non-standard gas config.",
                    fg="#f97316")
        messagebox.showinfo(
            "Connected",
            f"Confirmed communication with all {len(confirmed)} selected controllers.")

    async def _query_gases_async(self, port, unit, baudrate=None):
        """Run the blocking gas-table transaction on the serial owner thread."""
        return self._query_gases_serial(port, unit, baudrate)

    def _query_gases_serial(self, port, unit, baudrate=None):
        return self._alicat_protocol.query_gases(
            port, unit, baudrate or self.serial_baudrate)

    def _set_gas_serial(self, port, unit, gas_index, baudrate=None):
        self._alicat_protocol.set_gas(
            port, unit, gas_index, baudrate or self.serial_baudrate)

    def _disconnect_all(self):
        if not messagebox.askyesno("Disconnect","Disconnect all controllers?"): return
        if self.is_monitoring: self._stop_monitoring()
        self.controllers_connected = False; self._set_estop_armed(False)
        self.conn_status.config(text="Not connected", fg='#ff4444')
        self.connect_btn.config(state='normal')
        self.disconnect_btn.config(state='disabled')
        self.monitor_btn.config(state='disabled')
        self._set_assignment_controls_locked(False)
        self._log_conn("Disconnected.")

    def _log_conn(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self._ui_log_queue.put(("connection", ts, str(msg)))


    # ================================================================== #
    #  Monitoring                                                         #
    # ================================================================== #
    def _toggle_monitoring(self):
        if self.is_monitoring: self._stop_monitoring()
        else: self._start_monitoring()

    def _clear_poll_rate_queue(self):
        while not self._poll_rate_queue.empty():
            try: self._poll_rate_queue.get_nowait()
            except Exception: break

    def _drain_poll_rate_queue(self):
        """Update the Tk rate label using timing samples from the worker."""
        latest = None
        while not self._poll_rate_queue.empty():
            try: latest = self._poll_rate_queue.get_nowait()
            except Exception: break
        if latest is not None and self.is_monitoring:
            hz, period_ms = latest
            try:
                self.poll_rate_lbl.config(
                    text=f"Effective: {hz:.2f} Hz ({period_ms:.0f} ms/pass)",
                    fg='#60a5fa')
            except Exception: pass
        try: self.root.after(250, self._drain_poll_rate_queue)
        except Exception: pass

    def _schedule_live_ui_refresh(self):
        """Start one bounded Tk refresh loop for cards, calculations and graphs."""
        if self._live_ui_after_id is None:
            self._live_ui_after_id = self.root.after(0, self._refresh_live_ui)

    def _cancel_live_ui_refresh(self):
        after_id = self._live_ui_after_id
        self._live_ui_after_id = None
        if after_id is not None:
            try: self.root.after_cancel(after_id)
            except Exception: pass

    def _refresh_live_ui(self):
        """Render the newest samples without coupling display rate to serial rate."""
        self._live_ui_after_id = None
        if not self.is_monitoring:
            return
        for unit, sample in list(self._live_samples.items()):
            flow = sample.get('flow'); sp = sample.get('sp')
            press = sample.get('press'); temp = sample.get('temp')
            if any(value is None for value in (flow, sp, press, temp)):
                continue
            for store in (self.cards_tab1, self.cards_tab2):
                if unit not in store:
                    continue
                widgets = store[unit]
                try:
                    card = widgets.get('card')
                    if card and not card.winfo_exists():
                        continue
                    widgets['flow'].config(text=f"{flow:.3f}")
                    widgets['setpoint'].config(text=f"{sp:.3f}")
                    widgets['pressure'].config(text=f"{press:.2f}")
                    widgets['temp'].config(text=f"{temp:.1f}")
                except Exception:
                    pass
        try: self._update_combustion()
        except Exception: pass
        if self.is_monitoring:
            self._live_ui_after_id = self.root.after(
                self._live_ui_refresh_ms, self._refresh_live_ui)

    def _apply_poll_interval(self):
        """Validate and apply the post-pass delay without restarting monitoring."""
        try:
            interval_ms = float(self.poll_interval_entry.get().strip())
        except (TypeError, ValueError):
            messagebox.showerror("Polling Interval", "Enter a number between 0 and 5000 ms.")
            return
        if not 0.0 <= interval_ms <= 5000.0:
            messagebox.showerror("Polling Interval", "Polling delay must be between 0 and 5000 ms.")
            return
        self.poll_interval_s = interval_ms / 1000.0
        normalized = f"{interval_ms:g}"
        self.poll_interval_entry.delete(0, tk.END)
        self.poll_interval_entry.insert(0, normalized)
        self._log(f"Polling pass delay set to {normalized} ms"
                  + (" (applied live)." if self.is_monitoring else "."))

    def _start_monitoring(self):
        if not self.controllers_connected: messagebox.showerror("Error","Connect controllers first."); return
        if (self.is_connecting or self._emergency_stop_active
                or self._zero_action_active):
            messagebox.showerror("Monitor", "Wait for the current serial operation to finish.")
            return
        port = self.port_combo.get()
        if not port: messagebox.showerror("Error","Select a COM port first."); return
        self._monitor_port = port
        self._monitor_baudrate = self.serial_baudrate
        self.is_monitoring = True; self._restart_pending = False
        self.baud_combo.config(state='disabled')
        self.monitor_btn.config(text="Stop Live Monitor", style='Stop.TButton')
        self._rebuild_cards()
        self._refresh_graph_series_controls()
        self._clear_graph_history(log=False)
        self._schedule_live_ui_refresh()
        while not self.setpoint_queue.empty():
            try: self.setpoint_queue.get_nowait()
            except Exception: break
        self._clear_poll_rate_queue()
        self.poll_rate_lbl.config(text="Effective: starting…", fg='#fbbf24')
        self._log(f"Monitoring started on {port} at {self._monitor_baudrate} baud "
                  f"with {self.poll_interval_s * 1000:g} ms pass delay.")
        self._log_conn("Live monitoring started.")
        # Graphs stay dormant until the operator opens the tab and picks
        # series; history collection below is unaffected by that.
        self.root.after(50, self._sync_graph_activation)
        if not self._start_monitor_task():
            self.is_monitoring = False
            self._cancel_live_ui_refresh()
            self.baud_combo.config(state='readonly')
            self.monitor_btn.config(text="Start Live Monitor", style='Monitor.TButton')

    def _stop_monitoring(self):
        self.is_monitoring = False
        self._restart_pending = False
        self._restart_reason = None
        self._reset_ramp_active()
        self._cancel_live_ui_refresh()
        self.baud_combo.config(state='readonly')
        self.monitor_btn.config(text="Start Live Monitor", style='Monitor.TButton')
        self._clear_poll_rate_queue()
        self.poll_rate_lbl.config(text="Effective: stopped", fg='#666666')
        self._log("Monitoring stopped."); self._log_conn("Live monitoring stopped.")
        self._clear_disc_highlights()

    def _start_monitor_task(self):
        """Start monitoring on the serial owner loop if no prior task is active."""
        if self._monitor_future is not None and not self._monitor_future.done():
            self._log("Monitor start deferred: previous serial task is still closing.")
            return False
        try:
            self._monitor_future = self._submit_serial(
                self._monitor_async(), self._on_monitor_done)
            return True
        except Exception as exc:
            self._monitor_future = None
            self._log(f"ERROR: Could not start monitor: {exc}")
            messagebox.showerror("Monitor", f"Could not start serial monitoring:\n{exc}")
            return False

    def _on_monitor_done(self, future):
        if future is not self._monitor_future:
            return
        self._monitor_future = None
        error = None
        try:
            future.result()
        except Exception as exc:
            error = exc
            self._log(f"Monitor serial task ended with an error: {type(exc).__name__}: {exc}")

        if self._emergency_stop_active:
            return
        if self._zero_action_active and self._active_zero_request is not None:
            request = self._active_zero_request
            self._finish_zero_request(
                request, {}, {
                    unit: "live monitor closed before zero was confirmed"
                    for unit in request.units
                })
        if self._restart_pending:
            self._begin_serial_restart()
            return
        if self.is_monitoring:
            reconnect_failed = self._reconnect_active
            self.is_monitoring = False
            self._cancel_live_ui_refresh()
            self.baud_combo.config(state='readonly')
            self.monitor_btn.config(text="Start Live Monitor", style='Monitor.TButton')
            self.poll_rate_lbl.config(text="Effective: stopped", fg='#f87171')
            self._reconnect_active = False
            self._set_estop_armed(self.controllers_connected)
            if reconnect_failed:
                self.restart_status_lbl.config(
                    text="Reconnect failed", fg='#f87171')
            message = ("The flow meters could not be reconnected."
                       if reconnect_failed
                       else "The serial monitor stopped unexpectedly.")
            if error is not None:
                message += f"\n\n{type(error).__name__}: {error}"
            messagebox.showerror(
                "Reconnect Flow Meters" if reconnect_failed
                else "Monitoring stopped", message)

    def _begin_serial_restart(self):
        if not self._restart_pending or self._emergency_stop_active:
            return
        try:
            self._submit_serial(self._flush_serial_async(), self._resume_monitor_after_restart)
        except Exception as exc:
            self._restart_pending = False
            self._reconnect_active = False
            self._set_estop_armed(self.controllers_connected)
            self._log(f"Connection restart failed: {exc}")
            messagebox.showerror("Reconnect Flow Meters", str(exc))

    async def _flush_serial_async(self):
        self._flush_serial()

    def _resume_monitor_after_restart(self, future):
        try:
            future.result()
        except Exception as exc:
            self._log(f"Serial buffer flush failed during restart: {exc}")
            was_manual = self._restart_reason == "manual"
            self._restart_pending = False
            self._restart_reason = None
            self._reconnect_active = False
            self._set_estop_armed(self.controllers_connected)
            self.baud_combo.config(state='readonly')
            self.monitor_btn.config(
                text="Start Live Monitor", style='Monitor.TButton')
            self.restart_status_lbl.config(
                text="Reconnect failed", fg='#f87171')
            if was_manual:
                messagebox.showerror(
                    "Reconnect Flow Meters",
                    f"Could not reopen the serial port:\n\n{exc}")
            return
        if not self._restart_pending or not self.controllers_connected:
            self._reconnect_active = False
            self._set_estop_armed(self.controllers_connected)
            return
        reason = self._restart_reason
        self._restart_pending = False
        self._restart_reason = None
        self._rebuild_cards()
        self.is_monitoring = True
        self._schedule_live_ui_refresh()
        self._clear_poll_rate_queue()
        self.poll_rate_lbl.config(text="Effective: starting\u2026", fg='#fbbf24')
        self.monitor_btn.config(text="Stop Live Monitor", style='Stop.TButton')
        if not self._start_monitor_task():
            self.is_monitoring = False
            return
        if reason == "auto":
            self._log("Monitor auto-restarted successfully. \u2713")
            self.state_banner.config(
                text="  Monitor auto-restarted after COM timeout  ",
                bg='#001a0d', fg='#4ade80')
        else:
            self.restart_status_lbl.config(
                text="Reopening flow meters…", fg='#fb923c')

    def _finish_reconnect_success(self, opened_count):
        if not self._reconnect_active:
            return
        self._reconnect_active = False
        self._set_estop_armed(self.controllers_connected)
        self.restart_status_lbl.config(
            text=f"\u2713 {opened_count} flow meter(s) reconnected", fg='#4ade80')
        self._log(
            f"Flow meter reconnection confirmed on {opened_count} unit(s). \u2713")
        self.root.after(4000, lambda: self.restart_status_lbl.config(text=""))

    def _rebuild_cards(self):
        for jid in list(self._pending_after_ids):
            try: self.root.after_cancel(jid)
            except Exception: pass
        self._pending_after_ids.clear()
        for w in self.grid_container_tab1.winfo_children(): w.destroy()
        for w in self.grid_container_tab2.winfo_children(): w.destroy()
        self.cards_tab1.clear(); self.cards_tab2.clear()
        zone_buckets = {"Zone 1":[],"Zone 2":[],"Pilot":[],"General":[]}
        for unit, cbs in self._selected_controller_rows():
            g = cbs["gas_type"].get(); z = cbs["zone"].get()
            if g in ("-- select --","") or z == "-- unassigned --": continue
            rk = self.ROLE_MAP.get((g,z)) or f"custom_{unit}"
            if z in zone_buckets: zone_buckets[z].append((rk,g,z,unit))
        ZONE_STYLE = {
            "Zone 1": dict(hdr_bg="#1e3a5f",hdr_fg="#7dd3fc",sec_bg="#0f1e30"),
            "Zone 2": dict(hdr_bg="#1a3a1a",hdr_fg="#4ade80",sec_bg="#0a200a"),
            "Pilot":  dict(hdr_bg="#3a2800",hdr_fg="#fbbf24",sec_bg="#201500"),
            "General":dict(hdr_bg="#2a2a2a",hdr_fg="#94a3b8",sec_bg="#111110"),
        }
        for container in (self.grid_container_tab1, self.grid_container_tab2):
            card_store = self.cards_tab1 if container is self.grid_container_tab1 else self.cards_tab2
            row = 0
            for zone, members in zone_buckets.items():
                if not members: continue
                st = ZONE_STYLE[zone]
                sec = tk.Frame(container, bg=st["sec_bg"], highlightbackground=st["hdr_fg"], highlightthickness=1)
                sec.grid(row=row, column=0, padx=6, pady=(6,2), sticky="ew")
                container.grid_columnconfigure(0, weight=1); container.grid_rowconfigure(row, weight=0)
                banner = tk.Frame(sec, bg=st["hdr_bg"], height=24); banner.pack(fill="x"); banner.pack_propagate(False)
                tk.Label(banner, text=zone, bg=st["hdr_bg"], fg=st["hdr_fg"],
                         font=("Yu Gothic UI",9,"bold")).pack(side="left", padx=10, pady=2)
                tk.Label(banner, text=f"{len(members)} controller{'s' if len(members)!=1 else ''}",
                         bg=st["hdr_bg"], fg=st["hdr_fg"], font=("Yu Gothic UI",7)).pack(side="right", padx=8, pady=2)
                for rk, gas, zone_str, unit in members:
                    self._make_card(sec, rk, gas, unit, card_store,
                                    hdr_bg=st["hdr_bg"], hdr_fg=st["hdr_fg"], sec_bg=st["sec_bg"])
                row += 1

    def _flush_serial(self):
        port = self._monitor_port or self.port_combo.get()
        if not port:
            raise OSError("No COM port is selected")
        baudrate = self._monitor_baudrate or self.serial_baudrate
        with serial.Serial(port, baudrate=baudrate, timeout=0.15) as connection:
            connection.reset_input_buffer()
            connection.reset_output_buffer()

    def _auto_restart_monitoring(self):
        if self._restart_pending: return
        self._restart_pending = True
        self._restart_reason = "auto"
        self._log("COM timeout limit reached \u2014 stopping monitor for auto-restart\u2026")
        self.state_banner.config(text="  COM TIMEOUT \u2014 auto-restarting monitor\u2026  ",
                                 bg='#1a0a00', fg='#fb923c')
        self.is_monitoring = False
        self._cancel_live_ui_refresh()
        if self._monitor_future is None or self._monitor_future.done():
            self._begin_serial_restart()

    @staticmethod
    def _parse_alicat_numeric_response(raw, unit):
        return AlicatProtocol.parse_numeric_response(raw, unit)

    def _telemetry_state(self, unit):
        """Return one complete per-device capability cache."""
        support = self._telemetry_support.setdefault(unit, {})
        defaults = {
            'combined': None,
            'combined_failures': 0,
            'internal_error': None,
            'valve_drive': None,
            'valve_mode': None,
        }
        for key, value in defaults.items():
            support.setdefault(key, value)
        return support

    async def _read_combined_live_sample(self, fc, unit):
        """Read six live fields in one DV transaction when firmware supports it."""
        support = self._telemetry_state(unit)
        if support['combined'] is False:
            return None
        raw = None
        try:
            # Values are returned in the same order as the requested Alicat
            # statistics: pressure, temperature, mass flow, mass-flow
            # setpoint, internal error, and valve drive.
            raw = await asyncio.wait_for(
                fc._write_and_read(f"{unit}DV 1 2 3 5 37 173 13"), timeout=1.0)
            if raw and '?' in raw:
                support['combined'] = False
                if not support.get('combined_failure_logged'):
                    self._log(f"Unit {unit}: combined live telemetry is unsupported; "
                              "using compatible multi-request polling.")
                    support['combined_failure_logged'] = True
                return None
            values = self._parse_alicat_numeric_response(raw, unit)
            if (values and len(values) == 6
                    and 0.0 <= values[5] <= 100.0):
                support['combined'] = True
                support['combined_failures'] = 0
                return {
                    'press': values[0], 'temp': values[1],
                    'flow': values[2], 'sp': values[3],
                    'internal_error': values[4],
                    'valve_drives': (values[5],),
                }
        except Exception:
            # A failed optimization probe must not interrupt normal polling.
            pass

        support['combined_failures'] += 1
        if support['combined_failures'] >= 2:
            support['combined'] = False
            if not support.get('combined_failure_logged'):
                self._log(f"Unit {unit}: combined live telemetry did not return six valid "
                          f"fields (last response: {raw!r}); using compatible polling.")
                support['combined_failure_logged'] = True
        return None

    async def _read_optimized_live_sample(self, fc, unit):
        """Read all live fields, including valve drive 1, in one request."""
        sample = await self._read_combined_live_sample(fc, unit)
        if sample is None:
            return None
        support = self._telemetry_state(unit)
        if not support.get('combined_active_logged'):
            self._log(f"Unit {unit}: combined telemetry active "
                      f"(1 request/pass, valve drive 1).")
            support['combined_active_logged'] = True
        return sample

    async def _read_internal_setpoint_error(self, fc, unit):
        """Read Alicat statistic 173: mass flow minus ramp-limited setpoint."""
        support = self._telemetry_state(unit)
        if support['internal_error'] is False:
            return None
        try:
            # A one-millisecond request gives a live device sample. Statistic
            # 173 is the controller's own mass-flow setpoint error.
            raw = await asyncio.wait_for(
                fc._write_and_read(f"{unit}DV 1 173"), timeout=1.0)
            if raw and '?' in raw:
                support['internal_error'] = False
                if not support.get('internal_error_diagnostic_logged'):
                    self._log(f"Unit {unit}: internal setpoint-error telemetry is not supported "
                              f"(DV response: {raw!r}; requires compatible firmware).")
                    support['internal_error_diagnostic_logged'] = True
                return None
            values = self._parse_alicat_numeric_response(raw, unit)
            if values:
                support['internal_error'] = True
                if not support.get('internal_error_diagnostic_logged'):
                    self._log(f"Unit {unit}: internal setpoint-error telemetry active "
                              f"(DV response: {raw!r}).")
                    support['internal_error_diagnostic_logged'] = True
                return values[-1]
            if not support.get('internal_error_diagnostic_logged'):
                self._log(f"Unit {unit}: could not parse internal setpoint-error response: {raw!r}")
                support['internal_error_diagnostic_logged'] = True
        except Exception:
            # A diagnostic read must never interrupt normal flow control.
            pass
        return None

    async def _read_valve_drive(self, fc, unit):
        """Read instantaneous valve-drive 1 using Alicat's VD command."""
        support = self._telemetry_state(unit)
        if support['valve_drive'] is False or support['valve_mode'] == 'none':
            return ()
        vd_raw = None
        dv_raw = None
        try:
            if support['valve_mode'] != 'dv13':
                try:
                    vd_raw = await asyncio.wait_for(
                        fc._write_and_read(f"{unit}VD"), timeout=1.0)
                except Exception:
                    # During capability detection, a VD timeout is allowed to
                    # fall through to the older DV-statistic request.
                    if support['valve_mode'] == 'vd':
                        return ()
                values = self._parse_alicat_numeric_response(vd_raw, unit)
                if (values and 1 <= len(values) <= 3
                        and all(0.0 <= value <= 100.0 for value in values)):
                    support['valve_drive'] = True
                    support['valve_mode'] = 'vd'
                    if not support.get('valve_drive_diagnostic_logged'):
                        self._log(f"Unit {unit}: valve-drive telemetry active "
                                  f"(VD response: {vd_raw!r}).")
                        support['valve_drive_diagnostic_logged'] = True
                    return (values[0],)

                # Once a working VD mode has been established, treat a bad
                # response as transient rather than adding a second request.
                if support['valve_mode'] == 'vd':
                    return ()

            # VD was introduced later than the generic DV request. Older
            # compatible firmware can still expose statistic 13 (valve drive)
            # through a one-millisecond DV sample.
            dv_raw = await asyncio.wait_for(
                fc._write_and_read(f"{unit}DV 1 13"), timeout=1.0)
            fallback_values = self._parse_alicat_numeric_response(dv_raw, unit)
            if fallback_values and 0.0 <= fallback_values[-1] <= 100.0:
                support['valve_drive'] = True
                support['valve_mode'] = 'dv13'
                if not support.get('valve_drive_diagnostic_logged'):
                    self._log(f"Unit {unit}: valve-drive telemetry active via DV fallback "
                              f"(VD response: {vd_raw!r}; DV response: {dv_raw!r}).")
                    support['valve_drive_diagnostic_logged'] = True
                return (fallback_values[-1],)

            if ((vd_raw and '?' in vd_raw) and (dv_raw and '?' in dv_raw)):
                support['valve_drive'] = False
                support['valve_mode'] = 'none'
            if not support.get('valve_drive_diagnostic_logged'):
                self._log(f"Unit {unit}: valve-drive telemetry unavailable or unparsed "
                          f"(VD response: {vd_raw!r}; DV response: {dv_raw!r}).")
                support['valve_drive_diagnostic_logged'] = True
        except Exception:
            # A diagnostic read must never interrupt normal flow control.
            pass
        return ()

    async def _wait_for_next_poll(self):
        """Wait for the configured delay while remaining responsive to stop/restart."""
        remaining = self.poll_interval_s
        while self.is_monitoring and remaining > 0:
            chunk = min(0.05, remaining)
            await asyncio.sleep(chunk)
            remaining -= chunk

    async def _service_zero_requests(self, controllers):
        """Service priority zero commands on the monitor's serial owner."""
        serviced = False
        while not self._zero_request_queue.empty():
            try:
                request = self._zero_request_queue.get_nowait()
            except Exception:
                break
            serviced = True
            targets = set(request.units)

            # Remove stale normal commands for targeted units while preserving
            # commands for units outside the selected safety scope.
            retained = []
            while not self.setpoint_queue.empty():
                try:
                    item = self.setpoint_queue.get_nowait()
                except Exception:
                    break
                if item[0] not in targets:
                    retained.append(item)
            for item in retained:
                self.setpoint_queue.put(item)

            confirmed = {}
            errors = {}
            for unit in request.units:
                controller = controllers.get(unit)
                if controller is None:
                    errors[unit] = "controller connection is not open"
                    continue
                last_error = None
                for attempt in range(1, 3):
                    try:
                        await asyncio.wait_for(
                            controller.set_flow_rate(0.0), timeout=2.5)
                        reading = await asyncio.wait_for(
                            controller.get(), timeout=1.5)
                        setpoint = float(reading['setpoint'])
                        if not math.isfinite(setpoint) or abs(setpoint) > 0.001:
                            raise OSError(
                                f"zero readback not confirmed (setpoint={setpoint})")
                        confirmed[unit] = setpoint
                        self._last_sp[unit] = 0.0
                        break
                    except Exception as exc:
                        last_error = (
                            f"attempt {attempt}: {type(exc).__name__}: {exc}")
                if unit not in confirmed:
                    errors[unit] = last_error or "zero command was not confirmed"
            self._post_ui(
                self._finish_zero_request, request, confirmed, errors)
        return serviced

    async def _monitor_async(self):
        port = self._monitor_port or self.port_combo.get()
        baudrate = self._monitor_baudrate or self.serial_baudrate
        fcs = {}; MAX_TIMEOUTS = 10; timeout_counts = {}
        self._live_samples.clear()
        self._telemetry_support.clear()
        event_loop = asyncio.get_running_loop()
        previous_pass_start = None
        pass_period_ema = None
        try:
            all_units = {}
            for key, unit in self.assignments.items():
                if unit: all_units[unit] = key
            for unit, key in getattr(self,'_custom_assignments',{}).items():
                if unit not in all_units: all_units[unit] = key
            for unit, key in all_units.items():
                if unit in fcs:
                    self._log(f"WARNING: Unit {unit} assigned to multiple roles (duplicate at '{key}'). Fix assignments."); continue
                try:
                    fc = FlowController(address=port, unit=unit, baudrate=baudrate); await fc.__aenter__()
                    fcs[unit] = fc; timeout_counts[unit] = 0; self.controller_instances[unit] = fc
                    self._log(f"Opened  Unit {unit}  ({key})")
                except Exception as e:
                    self._log(f"ERROR: Could not open {key} (Unit {unit}): {e}  Setpoints will be dropped.")
            self._log(f"Connections ready: {len(fcs)}/{len(all_units)} opened.")
            if not fcs:
                raise OSError("No flow meter connections could be opened")
            if self._reconnect_active and len(fcs) != len(all_units):
                raise OSError(
                    f"Reconnect opened {len(fcs)} of {len(all_units)} flow meters")
            _did_restore = False
            restore_failures = []
            for unit in fcs:
                sp_to_set = self._last_sp.get(unit, 0.0)
                try:
                    await asyncio.wait_for(fcs[unit].set_flow_rate(sp_to_set), timeout=2.0)
                    reading = await asyncio.wait_for(fcs[unit].get(), timeout=1.5)
                    reported_sp = float(reading['setpoint'])
                    tolerance = max(0.001, abs(sp_to_set) * 0.0001)
                    if (not math.isfinite(reported_sp)
                            or abs(reported_sp - sp_to_set) > tolerance):
                        raise OSError(
                            f"setpoint readback mismatch: requested {sp_to_set}, "
                            f"reported {reported_sp}")
                    if sp_to_set != 0.0: _did_restore = True
                except Exception as exc:
                    restore_failures.append(unit)
                    self._log(
                        f"WARNING: Unit {unit} initial setpoint was not confirmed: "
                        f"{type(exc).__name__}: {exc}")
            if restore_failures:
                self._log(
                    "WARNING: Initial setpoints were not confirmed for Unit(s) "
                    + ", ".join(restore_failures))
                if self._reconnect_active:
                    raise OSError(
                        "Reconnect could not confirm restored setpoints for Unit(s) "
                        + ", ".join(restore_failures))
            elif _did_restore:
                self._log("Setpoints restored from last session and confirmed. \u2713")
            else:
                self._log("All initial zero setpoints confirmed. \u2713")
            if self._reconnect_active:
                self._post_ui(self._finish_reconnect_success, len(fcs))

            while self.is_monitoring:
                pass_started = event_loop.time()
                if previous_pass_start is not None:
                    measured_period = pass_started - previous_pass_start
                    pass_period_ema = (measured_period if pass_period_ema is None
                                       else 0.8 * pass_period_ema + 0.2 * measured_period)
                    if pass_period_ema > 0:
                        self._poll_rate_queue.put(
                            (1.0 / pass_period_ema, pass_period_ema * 1000.0))
                previous_pass_start = pass_started
                await self._service_zero_requests(fcs)
                pending_sps = {}
                while not self.setpoint_queue.empty():
                    try: u, s = self.setpoint_queue.get_nowait(); pending_sps[u] = s
                    except Exception: break
                for unit, sp in pending_sps.items():
                    if unit in self._zero_locked_units and abs(sp) > 0.001:
                        self._log(
                            f"Unit {unit}: nonzero setpoint blocked by active zero command.")
                        continue
                    if unit in fcs:
                        try:
                            await asyncio.wait_for(fcs[unit].set_flow_rate(sp), timeout=2.0)
                            timeout_counts[unit] = 0; self._last_sp[unit] = sp
                            self._log(f"Unit {unit}: SP \u2192 {sp:.3f} SLPM")
                        except asyncio.TimeoutError:
                            self._log(f"WARNING: Unit {unit} SP={sp:.3f} \u2014 write sent but readback timed out (command likely applied).")
                        except Exception as e:
                            self._log(f"Set-flow error Unit {unit}: {e}")
                        finally: await asyncio.sleep(0.05)
                    else:
                        role = next((k for k,v in self.assignments.items() if v==unit),"unknown")
                        self._log(f"ERROR: Setpoint dropped \u2014 Unit {unit} ({role}) has no open connection.")

                # Pass-local blanks keep failed reads blank in CSV output,
                # while _live_samples retains the last valid display value.
                pass_samples = {
                    unit: {
                        'flow': None, 'sp': None, 'press': None, 'temp': None,
                        'internal_error': None, 'valve_drives': (),
                    }
                    for unit in fcs
                }
                for unit, fc in list(fcs.items()):
                    if not self._zero_request_queue.empty():
                        await self._service_zero_requests(fcs)
                    try:
                        sample = await self._read_optimized_live_sample(fc, unit)
                        if sample is None:
                            # Compatible fallback for firmware that does not
                            # expose the multi-statistic DV request.
                            r = await asyncio.wait_for(fc.get(), timeout=1.0)
                            sample = {
                                'flow': r.get('mass_flow',0),
                                'sp': r.get('setpoint',0),
                                'press': r.get('pressure',0),
                                'temp': r.get('temperature',0),
                                'internal_error': await self._read_internal_setpoint_error(fc, unit),
                                # Graph-history export is available independently
                                # of CSV logging, so collect valve drive whenever
                                # the live monitor is running.
                                'valve_drives': await self._read_valve_drive(fc, unit),
                            }
                        pass_samples[unit] = sample
                        self._live_samples[unit] = sample
                        timeout_counts[unit] = 0
                    except asyncio.TimeoutError:
                        timeout_counts[unit] = timeout_counts.get(unit,0)+1
                        self._log(f"Read timeout Unit {unit} ({timeout_counts[unit]}/{MAX_TIMEOUTS})")
                        if timeout_counts[unit] >= MAX_TIMEOUTS:
                            self._post_ui(self._auto_restart_monitoring)
                            return
                    except Exception as exc:
                        self._log(
                            f"Read error Unit {unit}: {type(exc).__name__}: {exc}")

                # Publish one generation per actual serial polling pass.  The
                # UI and graph renderer may run more frequently, but must not
                # duplicate the same hardware sample in graph history.
                self._latest_sample_timestamp = datetime.now()
                self._latest_graph_samples = {
                    unit: dict(sample) for unit, sample in pass_samples.items()
                }
                self._telemetry_generation += 1

                if self.logging and self.log_writer:
                    try:
                        # Build row dynamically from every unit currently on
                        # the live monitor, so General-zone and custom units
                        # are included automatically. Column order matches the
                        # header written by _start_logging (set at that time).
                        row = [datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]]
                        for unit in self._log_units:
                            sample = pass_samples.get(unit, {})
                            flow = sample.get('flow')
                            sp = sample.get('sp')
                            pres = sample.get('press')
                            temp = sample.get('temp')
                            internal_error = sample.get('internal_error')
                            valve_drives = sample.get('valve_drives', ())
                            values = (flow, sp, pres, temp, internal_error)
                            formatted = [f"{value:.4f}" if value is not None else ''
                                         for value in values]
                            drive_1 = (f"{valve_drives[0]:.4f}"
                                       if valve_drives else '')
                            row += formatted + [drive_1]

                        # Append derived phi columns for the standard RQL roles
                        # (still useful even when extra units are present).
                        nh3_r=self._get_sample_flow('nh3_rich',pass_samples); h2_r=self._get_sample_flow('h2_rich',pass_samples)
                        nh3_l=self._get_sample_flow('nh3_lean',pass_samples); h2_l=self._get_sample_flow('h2_lean',pass_samples)
                        air_r=self._get_sample_flow('rich_air',pass_samples); air_l=self._get_sample_flow('lean_air',pass_samples)
                        ch4_p=self._get_sample_flow('ch4_pilot',pass_samples)
                        phi_r  = self.calc.phi(nh3_r, h2_r, air_r, ch4_p)
                        phi_l  = self.calc.phi(nh3_l, h2_l, air_l)
                        phi_g  = self.calc.phi(nh3_r+nh3_l, h2_r+h2_l, air_r+air_l, ch4_p)
                        row += [f"{phi_r:.4f}", f"{phi_l:.4f}", f"{phi_g:.4f}"]

                        self.log_writer.writerow(row)
                        try: self.log_file.flush()
                        except Exception: pass
                    except Exception: pass

                await self._wait_for_next_poll()

        finally:
            for fc in fcs.values():
                try: await fc.__aexit__(None,None,None)
                except Exception: pass
            self.controller_instances.clear()

    # ================================================================== #
    #  Manual setpoint + display helpers                                  #
    # ================================================================== #
    def _queue_setpoint(self, unit, setpoint):
        """Queue a normal command unless a priority zero holds this unit."""
        value = float(setpoint)
        if unit in self._zero_locked_units and abs(value) > 0.001:
            return False
        self.setpoint_queue.put((unit, value))
        return True

    def _manual_set(self, key, entry):
        try: sp = float(entry.get())
        except ValueError: messagebox.showerror("Manual Set","Enter a numeric setpoint."); return
        if sp < 0: messagebox.showerror("Manual Set","Setpoint cannot be negative."); return
        unit = self.assignments.get(key)
        if not unit and key.startswith('custom_'): unit = key[len('custom_'):]
        if not unit: messagebox.showerror("Manual Set",f"No unit assigned for {key}."); return
        live = self._get_live_flow(key); cap_basis = max(live,1.0)
        if sp > cap_basis*10.0 and live > 0.0:
            if not messagebox.askyesno("Manual Set",f"Setpoint {sp:.2f} is more than 10x the current flow ({live:.2f}). Continue?"): return
        if key in self.RAMP_KEYS: self._start_ramp(key, sp)
        else:
            self._queue_setpoint(unit, sp)
            self._log(f"Unit {unit} ({key}): SP \u2192 {sp:.3f} SLPM (manual)")

    def _get_sample_flow(self, key, samples=None):
        """Read flow from the serial sample cache without accessing Tk widgets."""
        unit = self.assignments.get(key)
        if not unit and key.startswith('custom_'):
            unit = key[len('custom_'):]
        if not unit:
            return 0.0
        source = self._live_samples if samples is None else samples
        value = source.get(unit, {}).get('flow')
        try: return float(value) if value is not None else 0.0
        except (TypeError, ValueError): return 0.0

    def _get_live_flow(self, key):
        unit = self.assignments.get(key)
        if not unit: return 0.0
        for store in (self.cards_tab2, self.cards_tab1):
            if unit in store:
                try: return float(store[unit]['flow'].cget('text'))
                except Exception: return 0.0
        return 0.0

    def _get_live_sp(self, key):
        unit = self.assignments.get(key)
        if not unit: return 0.0
        for store in (self.cards_tab2, self.cards_tab1):
            if unit in store:
                try: return float(store[unit]['setpoint'].cget('text'))
                except Exception: return 0.0
        return 0.0

    def _update_combustion(self):
        nh3_r=self._get_sample_flow('nh3_rich'); h2_r=self._get_sample_flow('h2_rich')
        nh3_l=self._get_sample_flow('nh3_lean'); h2_l=self._get_sample_flow('h2_lean')
        ch4_p=self._get_sample_flow('ch4_pilot'); air_r=self._get_sample_flow('rich_air')
        air_l=self._get_sample_flow('lean_air'); total_fuel=nh3_r+h2_r+nh3_l+h2_l
        for attr, val in [('lbl_nh3_rich',nh3_r),('lbl_h2_rich',h2_r),('lbl_nh3_lean',nh3_l),
                          ('lbl_h2_lean',h2_l),('lbl_ch4_pilot',ch4_p),('lbl_rich_air',air_r),
                          ('lbl_lean_air',air_l),('lbl_total_fuel',total_fuel)]:
            try: getattr(self,attr).config(text=f"{val:.3f}")
            except Exception: pass
        phi_r=self.calc.phi(nh3_r,h2_r,air_r,ch4_p); phi_g=self.calc.phi(nh3_r+nh3_l,h2_r+h2_l,air_r+air_l,ch4_p)
        try: self.lbl_phi_rich.config(text=f"{phi_r:.3f}")
        except Exception: pass
        try: self.lbl_phi_global.config(text=f"{phi_g:.3f}")
        except Exception: pass
        if self._disc_enabled and self.is_monitoring: self._check_discrepancy()
        try: self._push_graph_sample()
        except Exception: pass


    # ================================================================== #
    #  Auto-calculate flows                                               #
    # ================================================================== #
    def _auto_calc(self):
        if not getattr(self,'_autocalc_available',True):
            problems = self._check_autocalc_compatible()
            bullet = "\n".join("  "+chr(8226)+" "+p for p in problems)
            messagebox.showerror("Auto-Calc Unavailable","Auto-calculation is disabled for this configuration:\n\n"+bullet+"\n\nUse manual setpoints instead."); return
        self._check_autocalc_compatible(); config = getattr(self,'_autocalc_config',None)
        if config not in ("FULL_RQL","RICH_QUENCH"):
            messagebox.showerror("Auto-Calc Unavailable","Current assignment does not match a supported configuration."); return
        try:
            power_kw=float(self.total_power_entry.get()); h2_frac=float(self.h2_frac_entry.get())/100.0
            phi_rich_t=float(self.phi_rich_entry.get()); phi_global_t=float(self.phi_global_entry.get())
            if config=="FULL_RQL":
                split_rich=float(self.fuel_split_entry.get())/100.0
                if not (0.0 < split_rich <= 1.0): raise ValueError("Fuel split must be between 0 and 100 %.")
            else: split_rich=1.0
            if not (0.0 < h2_frac < 1.0): raise ValueError("H2 fraction must be between 0 and 100 %.")
            if phi_rich_t<=0 or phi_global_t<=0: raise ValueError(chr(966)+" values must be > 0.")
            nh3_frac=1.0-h2_frac; rho_mix=nh3_frac*self.RHO_NH3+h2_frac*self.RHO_H2
            m_nh3=nh3_frac*self.RHO_NH3/rho_mix; m_h2=h2_frac*self.RHO_H2/rho_mix
            LHV_mix=m_nh3*self.LHV_NH3+m_h2*self.LHV_H2
            m_dot=power_kw/(LHV_mix*1000.0); V_total=m_dot/rho_mix*60.0*1000.0
            V_rich=V_total*split_rich; V_lean=V_total*(1.0-split_rich)
            nh3_rich=V_rich*nh3_frac; h2_rich=V_rich*h2_frac
            nh3_lean=V_lean*nh3_frac; h2_lean=V_lean*h2_frac
            air_stoich_rich=self.calc.stoich_air(nh3_rich,h2_rich)
            air_stoich_total=self.calc.stoich_air(nh3_rich+nh3_lean,h2_rich+h2_lean)
            air_rich=air_stoich_rich/phi_rich_t; air_total=air_stoich_total/phi_global_t
            air_lean=air_total-air_rich
            if air_lean < 0:
                if config=="FULL_RQL":
                    min_phi_r=split_rich*phi_global_t
                    raise ValueError(f"Lean air is negative.\n{chr(966)}_s1 must be {chr(8805)} {min_phi_r:.3f} (= split {chr(215)} {chr(966)}_global).\nYou entered {chr(966)}_s1 = {phi_rich_t:.3f}.")
                else:
                    raise ValueError(f"Quench air is negative.\n{chr(966)}_global ({phi_global_t}) cannot be richer than {chr(966)}_stage1 ({phi_rich_t}).\nLower {chr(966)}_global or raise {chr(966)}_stage1.")
            all_targets = {'nh3_rich':nh3_rich,'h2_rich':h2_rich,'nh3_lean':nh3_lean,'h2_lean':h2_lean,'rich_air':air_rich,'lean_air':air_lean}
            self.target_flows = {k:v for k,v in all_targets.items() if self.assignments.get(k)}
            for lbl in self.stored_flow_labels.values(): lbl.config(text="--")
            for key, val in self.target_flows.items():
                if key in self.stored_flow_labels: self.stored_flow_labels[key].config(text=f"{val:.2f}")
            mode_str="Full RQL" if config=="FULL_RQL" else "Rich + quench-air"
            self.calc_status.config(text=(f"Stored ({mode_str}): {power_kw:.1f} kW  H2={h2_frac*100:.0f}%  {chr(966)}_s1={phi_rich_t}  {chr(966)}_g={phi_global_t}  {chr(8212)} no flows sent"), fg='#4ade80')
            self.state_banner.config(text=f"  IDLE {chr(8212)} targets stored. Click STEP 1 to begin.  ", bg='#1a2a1a', fg='#86efac')
            self._log(f"Auto-calc [{mode_str}]: {power_kw} kW  H2={h2_frac*100:.1f}%  split={split_rich*100:.0f}%  {chr(966)}_s1={phi_rich_t}  {chr(966)}_g={phi_global_t}  [targets stored {chr(8212)} no flows sent]")
            orig_text=self.autocalc_btn.cget('text')
            self.autocalc_btn.config(text=chr(10003)+"  Targets Stored")
            self.root.after(1200, lambda t=orig_text: self.autocalc_btn.config(text=t))
            self._animate_stored_cells()
            try:
                if config=="RICH_QUENCH": self.fuel_split_entry.config(state='disabled')
                else: self.fuel_split_entry.config(state='normal')
            except Exception: pass
        except ValueError as e: messagebox.showerror("Input Error",str(e)); self.calc_status.config(text="Error in calculation.",fg='#f87171')
        except Exception as e: messagebox.showerror("Error",f"Unexpected error: {e}")

    # ================================================================== #
    #  Ignition sequence                                                  #
    # ================================================================== #
    def _update_phi_preview(self, *_):
        try:
            fs=float(self.pre_fuel_entry.get())/100.0; as_=float(self.pre_air_entry.get())/100.0
            if fs<=0 or as_<=0: raise ValueError
            ratio=fs/as_
            color='#4ecdc4' if 0.9<=ratio<=1.1 else '#facc15' if 0.7<=ratio<=1.3 else '#f87171'
            self.phi_preview.config(text=f"{chr(966)} ratio: {ratio:.2f}x", fg=color)
        except Exception: self.phi_preview.config(text=f"{chr(966)} ratio: --", fg='#555555')

    def _update_ramp_time(self, *_):
        try:
            steps=int(self.ramp_steps.get()); interval=float(self.ramp_interval.get())
            self.ramp_time_lbl.config(text=f"total: {steps*interval:.1f} s", fg='#666666')
        except Exception: self.ramp_time_lbl.config(text="total: --", fg='#555555')

    def _ready_ignition(self):
        if not self.target_flows: messagebox.showerror("Pre-ignition","Calculate targets first."); return
        if not self.is_monitoring: messagebox.showerror("Pre-ignition","Start the monitor first."); return
        try:
            fs=float(self.pre_fuel_entry.get())/100.0; as_=float(self.pre_air_entry.get())/100.0
            if not (0<fs<=1) or not (0<as_<=1): raise ValueError
        except Exception: messagebox.showerror("Pre-ignition","Fuel% and Air% must be > 0 and \u2264 100."); return
        try:
            steps=int(self.ramp_steps.get()); interval=float(self.ramp_interval.get())
            if steps<1 or interval<0.05: raise ValueError
        except Exception: messagebox.showerror("Pre-ignition","Ramp steps \u2265 1 and interval \u2265 0.05 s."); return
        self.pre_fuel_scale=fs; self.pre_air_scale=as_
        pre_targets = {}
        for key, val in self.target_flows.items():
            unit=self.assignments.get(key)
            if not unit: continue
            pre_targets[key] = val*(fs if key in self.FUEL_KEYS else as_)
        self.ignition_state = "PRE_IGNITION"
        self.state_banner.config(
            text=f"  PRE-IGNITION {chr(8212)} ramping to {fs*100:.0f}% fuel / {as_*100:.0f}% air {chr(8212)} ({steps} steps {chr(215)} {interval} s)  ",
            bg='#3a2400', fg='#fbbf24')
        self.ready_btn.config(state='disabled'); self.ignite_btn.config(state='disabled')
        self._log(f"Pre-ignition: ramping to {fs*100:.0f}% fuel, {as_*100:.0f}% air ({steps} x {interval} s).")
        threading.Thread(target=self._pre_ignition_ramp_thread, args=(pre_targets,steps,interval), daemon=True).start()

    def _pre_ignition_ramp_thread(self, pre_targets, steps, interval):
        """
        Interpolate every assigned controller from its current live flow to the
        scaled pre-ignition target. Unlocks the Ignite button on completion.
        """
        import time
        starts = {}
        for key in pre_targets:
            unit=self.assignments.get(key)
            if not unit: continue
            starts[key] = self._get_live_flow(key)
        for step in range(1, steps+1):
            if self.ignition_state != "PRE_IGNITION": break
            frac = step/steps
            for key, target in pre_targets.items():
                unit=self.assignments.get(key)
                if not unit: continue
                sp = starts.get(key,0.0) + (target-starts.get(key,0.0))*frac
                self._queue_setpoint(unit, sp)
            time.sleep(interval)
        if self.ignition_state == "PRE_IGNITION":
            # Snap to exact targets to absorb floating-point rounding.
            for key, val in pre_targets.items():
                unit=self.assignments.get(key)
                if not unit: continue
                self._queue_setpoint(unit, val)
            self.root.after(0, lambda: self.state_banner.config(
                text=f"  PRE-IGNITION COMPLETE {chr(8212)} ready to ignite  ",
                bg='#3a2400', fg='#fbbf24'))
            self.root.after(0, lambda: self.ignite_btn.config(state='normal'))
            self._log("Pre-ignition ramp complete \u2014 ready to ignite. \u2713")

    def _ignite(self):
        if self.ignition_state != "PRE_IGNITION": messagebox.showerror("Ignite","Pre-ignition first."); return
        try:
            steps=int(self.ramp_steps.get()); interval=float(self.ramp_interval.get())
            if steps<1 or interval<0.05: raise ValueError
        except Exception: messagebox.showerror("Ignite","Steps \u2265 1 and Step interval \u2265 0.05 s."); return
        self.ignition_state="IGNITED"; self.ignite_btn.config(state='disabled')
        self.state_banner.config(text=f"  IGNITED {chr(8212)} ramping to full target ({steps} steps {chr(215)} {interval} s)  ", bg='#2a0a0a', fg='#fb923c')
        self._log(f"Ignite: ramping all flows to target ({steps} \u00d7 {interval} s).")
        threading.Thread(target=self._ignition_ramp_thread, args=(steps,interval), daemon=True).start()

    def _ignition_ramp_thread(self, steps, interval):
        import time
        starts = {}
        for key in self.target_flows:
            unit=self.assignments.get(key)
            if not unit: continue
            scale=self.pre_fuel_scale if key in self.FUEL_KEYS else self.pre_air_scale
            starts[key] = self.target_flows[key]*scale
        for step in range(1, steps+1):
            if self.ignition_state != "IGNITED": break
            frac=step/steps
            for key, target in self.target_flows.items():
                unit=self.assignments.get(key)
                if not unit: continue
                sp=starts.get(key,0.0)+(target-starts.get(key,0.0))*frac
                self._queue_setpoint(unit, sp)
            time.sleep(interval)
        if self.ignition_state == "IGNITED":
            for key, val in self.target_flows.items():
                unit=self.assignments.get(key)
                if not unit: continue
                self._queue_setpoint(unit, val)
            self.root.after(0, lambda: self.state_banner.config(
                text=f"  IGNITED {chr(8212)} full target flows  ", bg='#1a2a1a', fg='#86efac'))
            self._log("Ignition ramp complete \u2014 at full target flows. \u2713")

    def _abort(self):
        self._zero_all()

    # ================================================================== #
    #  NEW: Stage targets / Set all flows together                        #
    # ================================================================== #
    def _stage_targets_to_entries(self):
        """
        Copy the stored auto-calc target (or pre-ignition scaled value) into
        each card's SP entry box. Nothing is queued or sent to controllers.
        """
        if not self.target_flows:
            messagebox.showerror("Stage Targets","No targets stored. Run Auto-Calculate first."); return
        use_pre = (self.ignition_state == "PRE_IGNITION")
        staged = {}
        for key, target in self.target_flows.items():
            unit=self.assignments.get(key)
            if not unit: continue
            if use_pre:
                scale=self.pre_fuel_scale if key in self.FUEL_KEYS else self.pre_air_scale
                staged[key] = target*scale
            else:
                staged[key] = target
        written = 0
        for key, value in staged.items():
            unit=self.assignments.get(key)
            if not unit: continue
            for store in (self.cards_tab2, self.cards_tab1):
                if unit in store and 'entry' in store[unit]:
                    try:
                        store[unit]['entry'].delete(0, tk.END)
                        store[unit]['entry'].insert(0, f"{value:.3f}")
                        written += 1
                    except Exception: pass
                    break
        mode="pre-ignition scaled" if use_pre else "full target"
        self._log(f"Staged {written} setpoint(s) to SP fields ({mode}). Nothing sent {chr(8212)} click Set per-card or Set All.")

    def _set_all_flows_together(self):
        """
        Read the current value in every card's SP entry and queue all of them
        in a single batch so controllers move together. Validates for negatives
        and a 10x runaway check before queuing.
        """
        if not self.is_monitoring:
            messagebox.showerror("Set All Flows","Start the Live Monitor first \u2014 setpoints are sent through the monitor loop."); return
        pending = []; skipped = []; seen_units = set()
        def _collect(store):
            for unit, widgets in store.items():
                if unit in seen_units: continue
                entry=widgets.get('entry')
                if entry is None: continue
                raw=entry.get().strip()
                try: sp=float(raw)
                except ValueError: skipped.append((unit,raw)); seen_units.add(unit); continue
                if sp < 0: skipped.append((unit,f"{sp} (negative)")); seen_units.add(unit); continue
                pending.append((unit,sp)); seen_units.add(unit)
        _collect(self.cards_tab2); _collect(self.cards_tab1)
        if not pending: messagebox.showwarning("Set All Flows","No valid setpoints found in card SP fields."); return
        runaway = []
        for unit, sp in pending:
            live=0.0
            for store in (self.cards_tab2,self.cards_tab1):
                if unit in store:
                    try: live=float(store[unit]['flow'].cget('text'))
                    except Exception: pass
                    break
            if live>0.0 and sp>live*10.0: runaway.append((unit,sp,live))
        if runaway:
            lines="\n".join(f"  Unit {u}: SP={sp:.2f} vs live={lv:.2f}" for u,sp,lv in runaway)
            if not messagebox.askyesno("Set All Flows",f"The following setpoints are >10x current flow:\n\n{lines}\n\nContinue?"): return
        for unit, sp in pending:
            self._queue_setpoint(unit, sp)
        self._log(f"Set All Flows Together \u2192 queued {len(pending)} setpoint(s) in one batch."
                  +(f"  Skipped: {len(skipped)}" if skipped else ""))
        if skipped:
            lines="\n".join(f"  Unit {u}: {raw}" for u,raw in skipped)
            messagebox.showwarning("Set All Flows",f"Queued {len(pending)} setpoint(s).\n\nSkipped {len(skipped)} due to invalid input:\n{lines}")

    # ================================================================== #
    #  Logging, UDP control, and priority zero actions                    #
    # ================================================================== #
    def _start_udp_listener(self):
        """Listen for LabVIEW Log/Stop datagrams without blocking Tk."""
        stop_event = threading.Event()
        self._udp_stop_event = stop_event
        host = self._udp_host; port = self._udp_port
        thread = threading.Thread(
            target=self._udp_listener_loop, args=(stop_event, host, port),
            daemon=True, name="labview-udp-listener")
        self._udp_thread = thread
        thread.start()

    def _stop_udp_listener(self):
        stop_event = getattr(self, '_udp_stop_event', None)
        if stop_event:
            stop_event.set()
        udp_socket = getattr(self, '_udp_socket', None)
        if udp_socket:
            try: udp_socket.close()
            except Exception: pass

    def _apply_udp_port(self):
        raw = self.udp_port_entry.get().strip()
        try:
            port = int(raw)
        except ValueError:
            messagebox.showerror("LabVIEW UDP Port", "Enter a whole-number port from 1 to 65535.")
            return
        if not 1 <= port <= 65535:
            messagebox.showerror("LabVIEW UDP Port", "Port must be between 1 and 65535.")
            return
        if port == self._udp_port and self._udp_socket is not None:
            self.udp_status_lbl.config(
                text=f"LabVIEW UDP: listening {self._udp_host}:{self._udp_port}",
                fg='#4ade80')
            return

        old_port = self._udp_port
        old_thread = self._udp_thread
        self._udp_restart_token += 1
        token = self._udp_restart_token
        self._stop_udp_listener()
        self._udp_port = port
        self.udp_port_entry.delete(0, tk.END)
        self.udp_port_entry.insert(0, str(port))
        self.udp_status_lbl.config(
            text=f"LabVIEW UDP: switching to {self._udp_host}:{port}", fg='#fbbf24')
        self._log(f"LabVIEW UDP port changing from {old_port} to {port}.")
        self._restart_udp_when_stopped(old_thread, token)

    def _restart_udp_when_stopped(self, old_thread, token):
        if token != self._udp_restart_token:
            return
        if old_thread and old_thread.is_alive():
            self.root.after(50, lambda: self._restart_udp_when_stopped(old_thread, token))
            return
        self._start_udp_listener()

    def _udp_listener_loop(self, stop_event, host, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.5)
        try:
            sock.bind((host, port))
        except OSError as exc:
            try: sock.close()
            except Exception: pass
            self._post_ui(self._on_udp_listener_error, str(exc), host, port)
            return

        self._udp_socket = sock
        self._post_ui(self._on_udp_listener_ready, host, port)
        try:
            while not stop_event.is_set():
                try:
                    payload, sender = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if not stop_event.is_set():
                        self._post_ui(
                            self._on_udp_listener_error, str(exc), host, port)
                    break

                command = payload.decode('utf-8', errors='replace').strip().lower()
                if command == 'log':
                    self._post_ui(self._start_labview_logging)
                elif command == 'stop':
                    self._post_ui(self._stop_labview_logging)
                else:
                    display = payload.decode('utf-8', errors='replace').strip()
                    self._post_ui(
                        self._log,
                        f"LabVIEW UDP ignored {display!r} from "
                        f"{sender[0]}:{sender[1]}")
        finally:
            try: sock.close()
            except Exception: pass
            if self._udp_socket is sock:
                self._udp_socket = None

    def _on_udp_listener_ready(self, host, port):
        if host != self._udp_host or port != self._udp_port:
            return
        self.udp_status_lbl.config(
            text=f"LabVIEW UDP: listening {host}:{port}",
            fg='#4ade80')
        self._log(f"LabVIEW UDP trigger listening on {host}:{port}.")

    def _on_udp_listener_error(self, error, host=None, port=None):
        host = self._udp_host if host is None else host
        port = self._udp_port if port is None else port
        if host != self._udp_host or port != self._udp_port:
            return
        self.udp_status_lbl.config(text="LabVIEW UDP: unavailable", fg='#f87171')
        self._log(f"LabVIEW UDP listener error on {host}:{port}: {error}")

    def _start_labview_logging(self):
        if self.logging:
            return
        if not self.is_monitoring:
            self.udp_status_lbl.config(
                text="LabVIEW UDP: Log ignored (monitor stopped)", fg='#fbbf24')
            self._log("LabVIEW Log received, but Live Monitor is stopped; logging was not started.")
            return
        self._start_logging(source="LabVIEW")

    def _stop_labview_logging(self):
        if not self.logging:
            return
        self._stop_logging(source="LabVIEW")

    def _choose_log_file(self):
        from tkinter import filedialog
        current = Path(self.log_path_entry.get().strip() or self._log_destination)
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV files","*.csv")],
            title="Choose log file",
            initialdir=str(current.parent),
            initialfile=current.name)
        if not path:
            return
        self._log_destination = path
        self.log_path_entry.delete(0, tk.END)
        self.log_path_entry.insert(0, path)
        self._log(f"Log destination changed to {path}.")

    def _configured_log_path(self, source):
        raw = self.log_path_entry.get().strip() or self._log_destination
        base = Path(raw).expanduser()
        if not base.is_absolute():
            base = Path(__file__).resolve().parent / base
        if base.suffix.lower() != '.csv':
            base = base.with_suffix('.csv')
        self._log_destination = str(base)
        self.log_path_entry.delete(0, tk.END)
        self.log_path_entry.insert(0, self._log_destination)
        if source == "LabVIEW":
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]
            return base.with_name(f"{base.stem}_{stamp}{base.suffix}")
        return base

    def _set_logging_controls_state(self, active):
        self.start_logging_btn.config(state='disabled' if active else 'normal')
        self.stop_logging_btn.config(state='normal' if active else 'disabled')
        self.log_path_entry.config(state='disabled' if active else 'normal')
        self.log_browse_btn.config(state='disabled' if active else 'normal')

    def _start_logging(self, path=None, source="Manual"):
        if self.logging: return
        if not self.is_monitoring:
            if source == "LabVIEW":
                self._log("LabVIEW Log received, but Live Monitor is stopped; logging was not started.")
            else:
                messagebox.showwarning("Logging",
                    "Start the Live Monitor before logging.\n\n"
                    "Rows are written from the monitor loop, so nothing is "
                    "captured while the monitor is stopped.")
            return
        try:
            output_path = Path(path) if path is not None else self._configured_log_path(source)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if source == "Manual" and output_path.exists():
                if not messagebox.askyesno(
                        "Replace log file?",
                        f"The selected log file already exists:\n\n{output_path}\n\n"
                        "Replace it?"):
                    return
            path = str(output_path)
            # Snapshot the ordered list of units currently on the live monitor,
            # in zone order so columns are stable and match what is on screen.
            zone_order = ["Zone 1", "Zone 2", "Pilot", "General"]
            zone_buckets = {z: [] for z in zone_order}
            for unit, cbs in self._selected_controller_rows():
                g = cbs["gas_type"].get()
                z = cbs["zone"].get()
                if g in ("-- select --", "") or z == "-- unassigned --":
                    continue
                if z in zone_buckets:
                    zone_buckets[z].append(unit)
            self._log_units = []
            for z in zone_order:
                self._log_units.extend(zone_buckets[z])

            # Build descriptive column names for every unit.
            def _unit_label(unit):
                cbs = self.assign_combos.get(unit, {})
                gas  = cbs["gas_type"].get() if cbs else ""
                zone = cbs["zone"].get()     if cbs else ""
                if gas and zone and zone != "-- unassigned --":
                    return f"{gas}_{zone.replace(' ','')}_U{unit}"
                return f"Unit{unit}"

            # Header: live process data plus controller-reported closed-loop
            # telemetry for each unit, followed by the derived phi columns.
            header = ['timestamp']
            for unit in self._log_units:
                lbl = _unit_label(unit)
                header += [f"{lbl}_flow", f"{lbl}_sp",
                           f"{lbl}_press", f"{lbl}_temp",
                           f"{lbl}_internal_sp_error",
                           f"{lbl}_valve_drive_1_pct"]
            header += ['phi_stage1_live', 'phi_stage2_live', 'phi_global_live']

            # buffering=1: line-buffered so every row reaches disk immediately.
            self.log_file   = open(path, 'w', newline='', buffering=1)
            self.log_writer = csv.writer(self.log_file)
            self._log_path  = path
            self.log_writer.writerow(header)
            self.logging = True
            self._logging_source = source
            self._set_logging_controls_state(True)
            self.log_status_lbl.config(
                text=f"Logging: ON  \u2014  {Path(path).name}"
                     f"  [{len(self._log_units)} units; {source}]",
                fg='#4ade80')
            self._log(f"Logging started \u2192 {path}  "
                      f"({len(self._log_units)} units: {', '.join(self._log_units)}; source: {source})")
            if source == "LabVIEW":
                self.udp_status_lbl.config(text="LabVIEW UDP: logging", fg='#4ade80')
                self._start_labview_tab_flare()
        except Exception as e:
            if source == "LabVIEW":
                self.udp_status_lbl.config(text="LabVIEW UDP: log start failed", fg='#f87171')
                self._log(f"LabVIEW logging error: could not open file: {e}")
            else:
                messagebox.showerror("Logging Error", f"Could not open file:\n{e}")
            self.log_file = None; self.log_writer = None
            self._logging_source = None
            self._set_logging_controls_state(False)
            self._stop_labview_tab_flare()

    def _stop_logging(self, source="Manual"):
        if not self.logging: return
        self.logging = False
        # Flush and fsync before closing so buffered rows are committed to disk.
        try:
            if self.log_file:
                self.log_file.flush()
                import os; os.fsync(self.log_file.fileno())
        except Exception: pass
        try: self.log_file.close()
        except Exception: pass
        path=getattr(self,'_log_path',None)
        logging_source = self._logging_source
        self.log_writer=None; self.log_file=None
        self._log_path=None; self._logging_source=None
        self._set_logging_controls_state(False)
        self.log_status_lbl.config(text="Logging: OFF", fg='#555555')
        if path: self._log(f"Logging stopped by {source}. File saved: {path}")
        else: self._log("Logging stopped.")
        if source == "LabVIEW" or logging_source == "LabVIEW":
            self._stop_labview_tab_flare()
            self.udp_status_lbl.config(
                text=f"LabVIEW UDP: listening {self._udp_host}:{self._udp_port}",
                fg='#4ade80')

    def _start_labview_tab_flare(self):
        self._labview_flare_active = True
        self._labview_flare_phase = 0.0
        if self._labview_flare_after_id is not None:
            try: self.root.after_cancel(self._labview_flare_after_id)
            except Exception: pass
        self._animate_labview_tab_flare()

    def _animate_labview_tab_flare(self):
        self._labview_flare_after_id = None
        if not self._labview_flare_active:
            return
        self._labview_flare_phase = (self._labview_flare_phase + 0.07) % 1.0
        self.notebook.set_flare(2, True, self._labview_flare_phase)
        self._labview_flare_after_id = self.root.after(
            70, self._animate_labview_tab_flare)

    def _stop_labview_tab_flare(self):
        self._labview_flare_active = False
        after_id = self._labview_flare_after_id
        self._labview_flare_after_id = None
        if after_id is not None:
            try: self.root.after_cancel(after_id)
            except Exception: pass
        self.notebook.set_flare(2, False, 0.0)

    def _zero_fuel(self):
        self._request_zero(include_air=False)

    def _zero_all(self):
        self._request_zero(include_air=True)

    def _request_zero(self, *, include_air):
        """Request an immediate verified zero without closing live monitoring."""
        if not self.controllers_connected:
            messagebox.showerror("Zero Flow", "Connect the flow meters first.")
            return
        if self._zero_action_active:
            return
        if self.is_connecting or self._restart_pending:
            messagebox.showerror(
                "Zero Flow", "Wait for the current connection operation to finish.")
            return

        unit_gases = []
        for unit, controls in self._selected_controller_rows():
            gas = controls['gas_type'].get().strip()
            zone = controls['zone'].get().strip()
            if gas in ('', '-- select --') or zone == '-- unassigned --':
                continue
            unit_gases.append((unit, gas))
        units = select_zero_units(unit_gases, include_air=include_air)
        if not units:
            messagebox.showwarning(
                "Zero Flow",
                "No selected controllers match this zero-flow action.")
            return

        scope = "all" if include_air else "fuel"
        scope_label = "ALL FLOWS" if include_air else "FUEL FLOWS"
        self._zero_action_active = True
        self._zero_locked_units.update(units)
        self.ignition_state = "IDLE"
        self._reset_ramp_active()
        for unit in units:
            # A reconnect must never restore the pre-zero value, even if one
            # controller fails to confirm before the operator retries.
            self._last_sp[unit] = 0.0
        try:
            self.ready_btn.config(state='normal')
            self.ignite_btn.config(state='disabled')
        except Exception:
            pass
        self._set_estop_armed(False)
        self.state_banner.config(
            text=f"  ZERO {scope_label} {chr(8212)} command in progress  ",
            bg='#4a1608' if not include_air else '#5a1010', fg='white')
        self._log(
            f"ZERO {scope_label} requested for Unit(s) {', '.join(units)}. "
            "Live monitoring will remain connected.")

        active_monitor = (
            self.is_monitoring and self._monitor_future is not None
            and not self._monitor_future.done())
        request = ZeroRequest(scope=scope, units=tuple(units))
        self._active_zero_request = request
        if active_monitor:
            self._zero_request_queue.put(request)
            return

        if self._monitor_future is not None and not self._monitor_future.done():
            self._zero_action_active = False
            self._active_zero_request = None
            self._zero_locked_units.difference_update(units)
            self._set_estop_armed(True)
            messagebox.showerror(
                "Zero Flow", "Wait for the live monitor connection to finish closing.")
            return

        # When monitoring is intentionally stopped, use transient controller
        # handles on the same serial owner and retain the logical connection.
        self._emergency_stop_active = True
        port = self._monitor_port or self.port_combo.get()
        baudrate = self._monitor_baudrate or self.serial_baudrate
        try:
            self._submit_serial(
                self._zero_controllers_async(port, baudrate, units),
                lambda future, req=request: self._finish_direct_zero(req, future))
        except Exception as exc:
            self._finish_zero_request(
                request, {}, {"serial worker": f"{type(exc).__name__}: {exc}"})

    def _finish_direct_zero(self, request, future):
        try:
            confirmed, errors = future.result()
        except Exception as exc:
            confirmed = {}
            errors = {"serial task": f"{type(exc).__name__}: {exc}"}
        self._finish_zero_request(request, confirmed, errors)

    def _finish_zero_request(self, request, confirmed, errors):
        """Report zero verification while preserving controller connections."""
        self._zero_action_active = False
        self._active_zero_request = None
        self._emergency_stop_active = False
        self._zero_locked_units.difference_update(request.units)
        self._set_estop_armed(self.controllers_connected)
        scope_label = "ALL FLOWS" if request.scope == "all" else "FUEL FLOWS"
        for unit in confirmed:
            self._last_sp[unit] = 0.0
            self._log(f"ZERO {scope_label}: Unit {unit} confirmed at zero. \u2713")
        if errors:
            details = "\n".join(
                f"Unit {unit}: {error}" for unit, error in errors.items())
            self.state_banner.config(
                text=f"  ZERO {scope_label} {chr(8212)} NOT CONFIRMED ON ALL UNITS  ",
                bg='#7f0000', fg='white')
            self._log(
                f"ZERO {scope_label} warning: unconfirmed Unit(s) "
                + ", ".join(str(unit) for unit in errors))
            messagebox.showerror(
                f"Zero {scope_label.title()} — confirmation failed",
                "Monitoring remains connected, but zero was not confirmed "
                "for every requested controller.\n\n" + details)
        else:
            self.state_banner.config(
                text=(f"  ZERO {scope_label} CONFIRMED {chr(8212)} "
                      "live monitoring continues  "),
                bg='#2a0a0a', fg='#fb923c')
            self._log(
                f"ZERO {scope_label} confirmed on {len(confirmed)} controller(s); "
                "connections remain open.")

    def _emergency_stop(self):
        """Compatibility alias for the former single emergency button."""
        self._zero_all()

    async def _zero_controllers_async(self, port, baudrate, units):
        """Write zero and confirm the controller-reported setpoint for each unit."""
        confirmed = {}
        errors = {}
        for unit in units:
            last_error = None
            for attempt in range(1, 3):
                try:
                    async with FlowController(
                            address=port, unit=unit, baudrate=baudrate,
                            timeout=0.3) as fc:
                        await asyncio.wait_for(fc.set_flow_rate(0.0), timeout=2.5)
                        reading = await asyncio.wait_for(fc.get(), timeout=1.5)
                    setpoint = float(reading['setpoint'])
                    if not math.isfinite(setpoint) or abs(setpoint) > 0.001:
                        raise OSError(
                            f"zero readback not confirmed (setpoint={setpoint})")
                    confirmed[unit] = setpoint
                    break
                except Exception as exc:
                    last_error = f"attempt {attempt}: {type(exc).__name__}: {exc}"
            if unit not in confirmed:
                errors[unit] = last_error or "zero command was not confirmed"
        return confirmed, errors

    # ================================================================== #
    #  Logging tab: graphs                                                #
    # ================================================================== #
    def _build_logging_tab(self):
        paned = tk.PanedWindow(
            self.tab_log, orient='horizontal', bg='#3a3a34', sashwidth=5,
            sashrelief='flat', sashpad=0, opaqueresize=False,
            borderwidth=0, relief='flat')
        paned.pack(fill='both', expand=True, padx=8, pady=8)
        left_shell, left = self._scrollable_column(paned)
        graph_side = tk.Frame(paned, bg='#111110')
        paned.add(left_shell, stretch='never', minsize=430)
        paned.add(graph_side, stretch='always')

        def _place_graph_sash(event, _done=[False]):
            if _done[0] or event.width <= 1:
                return
            _done[0] = True
            self._set_sashpos(paned, 0, 430)
        paned.bind('<Configure>', _place_graph_sash)

        series_panel = RoundedPanel(
            left, text="Displayed Series", bg='#111110', padx=8, pady=7)
        series_panel.pack(fill='x', padx=(0,8), pady=(0,6))
        self._dlabel(
            series_panel.body,
            "Choose controllers and measurements to display. Collection and CSV logging are unchanged.",
            fg='#777770', size=7, italic=True, wraplength=390,
            justify='left').pack(anchor='w', pady=(0,5))
        self._graph_series_container = self._drow(series_panel.body)
        self._graph_series_container.pack(fill='x')
        preset_row = self._drow(series_panel.body)
        preset_row.pack(fill='x', pady=(6,0))
        ttk.Button(
            preset_row, text="Flow + SP",
            command=lambda: self._set_graph_preset({'flow','sp'}),
            style='Compact.TButton').pack(side='left', padx=(0,4))
        ttk.Button(
            preset_row, text="All Telemetry",
            command=lambda: self._set_graph_preset(set(self.GRAPH_METRICS)),
            style='Compact.TButton').pack(side='left', padx=4)
        ttk.Button(
            preset_row, text="None",
            command=lambda: self._set_graph_preset(set()),
            style='Compact.TButton').pack(side='left', padx=4)

        axis_panel = RoundedPanel(
            left, text="Axis Limits", bg='#111110', padx=8, pady=7)
        axis_panel.pack(fill='x', padx=(0,8), pady=6)
        axis_grid = self._drow(axis_panel.body)
        axis_grid.pack(fill='x')
        for column, heading in enumerate(("Axis", "Auto", "Minimum", "Maximum")):
            self._dlabel(axis_grid, heading, fg='#777770', size=7, bold=True).grid(
                row=0, column=column, padx=4, pady=(0,4), sticky='w')
        axis_rows = [
            ('x', 'Time (s)', '0', '60'),
            ('flow', 'Flow / SP', '0', '10'),
            ('pressure', 'Pressure', '0', '30'),
            ('temperature', 'Temperature', '0', '100'),
            ('error', 'SP Error', '-1', '1'),
            ('valve', 'Valve Drive', '0', '100'),
        ]
        self._graph_axis_controls = {}
        for row_index, (key, label, default_min, default_max) in enumerate(
                axis_rows, start=1):
            self._dlabel(axis_grid, label, fg='#b8b2a5', size=8).grid(
                row=row_index, column=0, padx=4, pady=2, sticky='w')
            auto_var = tk.BooleanVar(value=True)
            auto_cb = ttk.Checkbutton(
                axis_grid, variable=auto_var, style='Graph.TCheckbutton',
                command=self._sync_graph_axis_controls)
            auto_cb.grid(row=row_index, column=1, padx=5, pady=2)
            minimum = ttk.Entry(axis_grid, width=8, font=('Consolas',8))
            maximum = ttk.Entry(axis_grid, width=8, font=('Consolas',8))
            minimum.insert(0, default_min); maximum.insert(0, default_max)
            minimum.grid(row=row_index, column=2, padx=3, pady=2)
            maximum.grid(row=row_index, column=3, padx=3, pady=2)
            self._graph_axis_controls[key] = {
                'auto': auto_var, 'min': minimum, 'max': maximum,
            }
        axis_action_row = self._drow(axis_panel.body)
        axis_action_row.pack(fill='x', pady=(7,0))
        self._graph_grid_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            axis_action_row, text="Grid", variable=self._graph_grid_var,
            style='Graph.TCheckbutton',
            command=lambda: self._apply_graph_axis_settings(False)).pack(
                side='left', padx=(0,8))
        ttk.Button(
            axis_action_row, text="Apply Axes",
            command=self._apply_graph_axis_settings,
            style='Accent.TButton').pack(side='left', padx=4)
        ttk.Button(
            axis_action_row, text="Reset Auto",
            command=self._reset_graph_axes,
            style='Compact.TButton').pack(side='left', padx=4)

        data_panel = RoundedPanel(
            left, text="History & Export", bg='#111110', padx=8, pady=7)
        data_panel.pack(fill='x', padx=(0,8), pady=6)
        data_row = self._drow(data_panel.body)
        data_row.pack(fill='x')
        ttk.Button(
            data_row, text="Export to Excel", command=self._export_to_excel,
            style='Secondary.TButton').pack(side='left', padx=(0,4))
        ttk.Button(
            data_row, text="Clear History", command=self._clear_graph_history,
            style='Compact.TButton').pack(side='left', padx=4)

        graph_header = self._drow(graph_side, bg='#181714')
        graph_header.pack(fill='x', padx=(8,0), pady=(0,5))
        self._dlabel(
            graph_header, "Live Trends", bg='#181714', fg='#efe9dc',
            size=10, bold=True).pack(side='left', padx=10, pady=7)
        self._graph_status_label = self._dlabel(
            graph_header, "Waiting for monitoring", bg='#181714',
            fg='#777770', family='Consolas', size=8)
        self._graph_status_label.pack(side='right', padx=10)
        self._graph_container = tk.Frame(graph_side, bg='#111110')
        self._graph_container.pack(fill='both', expand=True, padx=(8,0), pady=0)
        self._dlabel(
            self._graph_container, "Start monitoring to view live graphs.",
            fg='#444444', size=10).pack(pady=80)
        self._sync_graph_axis_controls()
        self._refresh_graph_series_controls()

    def _active_graph_units(self):
        active = []
        for unit, controls in self._selected_controller_rows():
            gas = controls['gas_type'].get().strip()
            zone = controls['zone'].get().strip()
            if gas in ('', '-- select --') or zone == '-- unassigned --':
                continue
            active.append((unit, gas, zone))
        return active

    def _refresh_graph_series_controls(self):
        if not hasattr(self, '_graph_series_container'):
            return
        previous = {
            key: bool(variable.get())
            for key, variable in self._graph_series_vars.items()
        }
        for widget in self._graph_series_container.winfo_children():
            widget.destroy()
        self._graph_series_vars = {}
        active = self._active_graph_units()
        colors = ['#f87171','#fbbf24','#34d399','#60a5fa','#c084fc','#fb923c','#22d3ee','#f472b6']
        self._dlabel(
            self._graph_series_container, "Controller", fg='#777770',
            size=7, bold=True).grid(row=0, column=0, padx=(2,8), pady=2, sticky='w')
        for column, (metric, metadata) in enumerate(
                self.GRAPH_METRICS.items(), start=1):
            self._dlabel(
                self._graph_series_container, metadata[0], fg='#777770',
                size=7, bold=True).grid(
                    row=0, column=column, padx=3, pady=2)
        active_units = set()
        for row, (unit, gas, zone) in enumerate(active, start=1):
            active_units.add(unit)
            color = self._graph_unit_meta.get(unit, {}).get(
                'color', colors[(row - 1) % len(colors)])
            self._graph_unit_meta[unit] = {
                'gas': gas, 'zone': zone,
                'label': f"{gas} (Unit {unit})", 'color': color,
            }
            history = self._graph_history.setdefault(unit, {})
            for metric in self.GRAPH_METRICS:
                history.setdefault(
                    metric, deque(maxlen=self._graph_history_limit))
            self._dlabel(
                self._graph_series_container, f"{gas}  U{unit}",
                fg=color, family='Consolas', size=8).grid(
                    row=row, column=0, padx=(2,8), pady=2, sticky='w')
            for column, metric in enumerate(self.GRAPH_METRICS, start=1):
                # Nothing is plotted until the operator asks for it, so a
                # newly assigned controller starts with every series off.
                variable = tk.BooleanVar(
                    value=previous.get((unit, metric), False))
                self._graph_series_vars[(unit, metric)] = variable
                ttk.Checkbutton(
                    self._graph_series_container, variable=variable,
                    style='Graph.TCheckbutton',
                    command=self._schedule_graph_rebuild).grid(
                        row=row, column=column, padx=4, pady=2)
        for unit in list(self._graph_history):
            if unit not in active_units:
                self._graph_history.pop(unit, None)
                self._graph_unit_meta.pop(unit, None)
        if not active:
            self._dlabel(
                self._graph_series_container, "Assign controllers to configure graph series.",
                fg='#555550', italic=True).grid(
                    row=1, column=0, columnspan=7, padx=4, pady=8, sticky='w')

    def _set_graph_preset(self, enabled_metrics):
        for (_unit, metric), variable in self._graph_series_vars.items():
            variable.set(metric in enabled_metrics)
        self._schedule_graph_rebuild()

    def _schedule_graph_rebuild(self):
        if self._graph_rebuild_after_id is None:
            self._graph_rebuild_after_id = self.root.after_idle(
                self._rebuild_graphs)

    def _sync_graph_axis_controls(self):
        for controls in self._graph_axis_controls.values():
            state = 'disabled' if controls['auto'].get() else 'normal'
            controls['min'].config(state=state)
            controls['max'].config(state=state)

    def _apply_graph_axis_settings(self, show_errors=True):
        try:
            settings = {}
            labels = {
                'x': 'Time axis', 'flow': 'Flow axis',
                'pressure': 'Pressure axis',
                'temperature': 'Temperature axis',
                'error': 'Setpoint-error axis', 'valve': 'Valve-drive axis',
            }
            for key, controls in self._graph_axis_controls.items():
                settings[key] = parse_axis_limits(
                    controls['auto'].get(), controls['min'].get(),
                    controls['max'].get(), labels[key])
            self._graph_axis_settings = settings
        except ValueError as exc:
            if show_errors:
                messagebox.showerror("Graph Axes", str(exc))
            return False
        self._sync_graph_axis_controls()
        if self._canvas is not None:
            # The grid is part of the cached background, so a change to it
            # has to be re-applied and the background re-captured.
            grid_on = bool(self._graph_grid_var.get())
            for axis in self._graph_axes.values():
                axis.grid(
                    grid_on, color='#292925', linewidth=0.55, alpha=0.9)
            self._animate_configurable_graphs(None)
            self._update_graph_limits()
            self._full_redraw_graphs()
        return True

    def _reset_graph_axes(self):
        for controls in self._graph_axis_controls.values():
            controls['auto'].set(True)
        self._graph_grid_var.set(True)
        self._apply_graph_axis_settings(False)

    def _dispose_graph_figure(self):
        self._stop_graph_rendering()
        if self._fig is not None:
            try: plt.close(self._fig)
            except Exception: pass
            self._fig = None
        self._canvas = None
        self._graph_axes = {}
        self._graph_lines = {}
        self._graph_background = None
        self._graph_needs_full_redraw = True

    # ------------------------------------------------------------------ #
    #  Lazy graph activation                                              #
    # ------------------------------------------------------------------ #
    def _on_tab_changed(self, _event=None):
        try:
            current = self.notebook.index('current')
        except Exception:
            return
        visible = (current == self._graph_tab_index)
        if visible == self._graph_tab_visible:
            return
        self._graph_tab_visible = visible
        self._sync_graph_activation()

    def _selected_graph_series(self):
        return [
            (unit, metric)
            for (unit, metric), variable in self._graph_series_vars.items()
            if variable.get()
        ]

    def _sync_graph_activation(self):
        """Build/tear down the figure to match tab visibility and selection.

        Called whenever either input changes.  Leaving the tab stops the
        render loop but leaves history collection untouched.
        """
        if not hasattr(self, '_graph_container'):
            return
        wanted = bool(self._graph_tab_visible and self._selected_graph_series())
        if wanted:
            if self._canvas is None:
                self._rebuild_configurable_graphs()
            else:
                self._start_graph_rendering()
        else:
            self._stop_graph_rendering()
            if self._canvas is not None and not self._graph_tab_visible:
                # Keep the built figure while the operator is only toggling
                # series; discard it when they navigate away.
                self._dispose_graph_figure()
                self._show_graph_placeholder()
        self._update_graph_status()

    def _show_graph_placeholder(self):
        for widget in self._graph_container.winfo_children():
            widget.destroy()
        if not self._active_graph_units():
            message = "Assign controllers, then choose series to plot."
        elif not self._selected_graph_series():
            message = "Tick a controller/measurement above to start plotting."
        else:
            message = "Open this tab to render the selected series."
        self._dlabel(
            self._graph_container, message, fg='#444444', size=10).pack(pady=80)

    def _start_graph_rendering(self):
        if self._graph_rendering or self._canvas is None:
            return
        self._graph_rendering = True
        self._graph_needs_full_redraw = True
        self._graph_frame_index = 0
        self._graph_render_after_id = self.root.after(
            0, self._graph_render_tick)

    def _stop_graph_rendering(self):
        self._graph_rendering = False
        after_id = self._graph_render_after_id
        self._graph_render_after_id = None
        if after_id is not None:
            try: self.root.after_cancel(after_id)
            except Exception: pass

    def _graph_render_tick(self):
        self._graph_render_after_id = None
        if not self._graph_rendering:
            return
        try:
            self._draw_graph_frame()
        except Exception:
            # A rendering fault must never stop control or acquisition.
            self._graph_needs_full_redraw = True
        if self._graph_rendering:
            self._graph_render_after_id = self.root.after(
                self._graph_render_ms, self._graph_render_tick)

    def _rebuild_configurable_graphs(self):
        """Recreate the plot layout from the operator's series selection."""
        self._graph_rebuild_after_id = None
        self._refresh_graph_series_controls()
        self._dispose_graph_figure()
        for widget in self._graph_container.winfo_children():
            widget.destroy()

        if not self._graph_tab_visible:
            self._show_graph_placeholder()
            self._update_graph_status()
            return

        visible = [
            (unit, metric)
            for (unit, metric), variable in self._graph_series_vars.items()
            if variable.get()
        ]
        group_order = ('flow', 'pressure', 'temperature', 'error', 'valve')
        visible_groups = [
            group for group in group_order
            if any(self.GRAPH_METRICS[metric][1] == group
                   for _unit, metric in visible)
        ]
        self._graph_axes = {}
        self._graph_lines = {}
        if not visible_groups:
            self._dlabel(
                self._graph_container,
                "Tick a controller/measurement above to start plotting.",
                fg='#555550', size=10).pack(pady=80)
            self._graph_status_label.config(text="No series selected")
            return

        figure, axes = plt.subplots(
            len(visible_groups), 1,
            figsize=(10, max(3.0, 2.45 * len(visible_groups))),
            facecolor='#111110', sharex=True)
        if len(visible_groups) == 1:
            axes = [axes]
        for axis, group in zip(axes, visible_groups):
            axis.set_facecolor('#181714')
            axis.set_title(
                self.GRAPH_GROUP_LABELS[group], color='#efe9dc', fontsize=10,
                loc='left', pad=5)
            axis.tick_params(colors='#8a8a84', labelsize=7)
            for spine in axis.spines.values():
                spine.set_color('#3a3a34')
            units = []
            for unit, metric in visible:
                label, metric_group, _unit_label, _sample_key, linestyle = (
                    self.GRAPH_METRICS[metric])
                if metric_group != group:
                    continue
                metadata = self._graph_unit_meta.get(unit, {})
                color = metadata.get('color', '#efe9dc')
                controller_label = metadata.get('label', f'Unit {unit}')
                line, = axis.plot(
                    [], [], color=color, linestyle=linestyle,
                    linewidth=1.45 if metric != 'sp' else 1.0,
                    alpha=0.95 if metric != 'sp' else 0.75,
                    label=f"{controller_label} — {label}")
                # Animated artists are skipped by draw(), which is what makes
                # them cheap to repaint over a cached background.
                line.set_animated(True)
                self._graph_lines[(unit, metric)] = line
                units.append(self.GRAPH_METRICS[metric][2])
            axis.set_ylabel(
                units[0] if len(set(units)) == 1 else '',
                color='#8a8a84', fontsize=8)
            axis.legend(
                loc='upper left', fontsize=7, facecolor='#181714',
                edgecolor='#3a3a34', labelcolor='#efe9dc', ncol=2)
            # The grid belongs to the cached background, so it is configured
            # once here rather than rebuilt on every frame.
            axis.grid(
                bool(self._graph_grid_var.get()), color='#292925',
                linewidth=0.55, alpha=0.9)
            self._graph_axes[group] = axis
        axes[-1].set_xlabel('Time (s)', color='#8a8a84', fontsize=8)
        figure.tight_layout()
        self._fig = figure
        self._canvas = FigureCanvasTkAgg(figure, master=self._graph_container)
        self._canvas.get_tk_widget().pack(fill='both', expand=True)
        # Any resize invalidates the cached background bitmap.
        self._canvas.get_tk_widget().bind(
            '<Configure>', self._on_graph_canvas_resize, add='+')
        if not self._graph_axis_settings:
            self._apply_graph_axis_settings(False)
        self._animate_configurable_graphs(None)
        self._update_graph_limits()
        self._graph_needs_full_redraw = True
        self._update_graph_status()
        self._start_graph_rendering()

    def _on_graph_canvas_resize(self, _event=None):
        self._graph_background = None
        self._graph_needs_full_redraw = True

    def _update_graph_status(self):
        if not hasattr(self, '_graph_status_label'):
            return
        selected = self._selected_graph_series()
        if not selected:
            text = "No series selected"
        elif not self._graph_tab_visible:
            text = f"{len(selected)} series · paused (tab hidden)"
        else:
            text = f"{len(selected)} series · {len(self._graph_axes)} axes"
        try: self._graph_status_label.config(text=text)
        except Exception: pass

    # ------------------------------------------------------------------ #
    #  Frame rendering                                                    #
    # ------------------------------------------------------------------ #
    def _draw_graph_frame(self):
        """Render one frame, blitting unless the axes themselves changed."""
        if self._canvas is None or not self._graph_lines:
            return
        self._animate_configurable_graphs(None)
        self._graph_frame_index += 1
        rescaled = False
        if self._graph_frame_index % self.GRAPH_LIMIT_CHECK_FRAMES == 0:
            rescaled = self._update_graph_limits()
        if (self._graph_needs_full_redraw or rescaled
                or self._graph_background is None):
            self._full_redraw_graphs()
        else:
            self._blit_graph_frame()

    def _full_redraw_graphs(self):
        """Repaint the whole figure and re-cache the static background."""
        if self._canvas is None or self._fig is None:
            return
        self._graph_needs_full_redraw = False
        try:
            self._canvas.draw()
            self._graph_background = self._canvas.copy_from_bbox(
                self._fig.bbox)
        except Exception:
            self._graph_background = None
            self._graph_needs_full_redraw = True
            return
        self._blit_graph_frame()

    def _blit_graph_frame(self):
        """Restore the cached background and repaint only the traces."""
        if self._canvas is None or self._graph_background is None:
            self._graph_needs_full_redraw = True
            return
        try:
            self._canvas.restore_region(self._graph_background)
            for line in self._graph_lines.values():
                axis = line.axes
                if axis is not None:
                    axis.draw_artist(line)
            self._canvas.blit(self._fig.bbox)
        except Exception:
            self._graph_background = None
            self._graph_needs_full_redraw = True

    def _group_data_bounds(self, group):
        """Finite min/max across the lines already loaded for one axis."""
        low = high = None
        for (_unit, metric), line in self._graph_lines.items():
            if self.GRAPH_METRICS[metric][1] != group:
                continue
            values = line.get_ydata()
            if len(values) == 0:
                continue
            finite = values[np.isfinite(values)]
            if finite.size == 0:
                continue
            value_low = float(finite.min())
            value_high = float(finite.max())
            low = value_low if low is None else min(low, value_low)
            high = value_high if high is None else max(high, value_high)
        return low, high

    def _update_graph_limits(self):
        """Apply manual limits, or auto-scale only when it is worth a repaint.

        Returns True when any axis moved, which forces a full redraw.
        """
        if not self._graph_axes:
            return False
        settings = self._graph_axis_settings or {}
        x_limits = settings.get('x')
        changed = False
        times = self._log_time
        for group, axis in self._graph_axes.items():
            y_limits = settings.get(group)
            if y_limits is not None:
                if tuple(axis.get_ylim()) != tuple(y_limits):
                    axis.set_ylim(*y_limits)
                    changed = True
            else:
                low, high = self._group_data_bounds(group)
                if low is not None and should_rescale(
                        axis.get_ylim(), low, high):
                    axis.set_ylim(*padded_limits(low, high))
                    changed = True
            if x_limits is not None:
                if tuple(axis.get_xlim()) != tuple(x_limits):
                    axis.set_xlim(*x_limits)
                    changed = True
            elif times:
                first, last = times[0], times[-1]
                if should_rescale(axis.get_xlim(), first, last):
                    axis.set_xlim(*padded_limits(first, last, pad=0.02))
                    changed = True
        return changed

    def _animate_configurable_graphs(self, _frame=None):
        """Load the newest history into the line artists.

        Only the data is touched here.  Grid, limits and repainting are
        handled separately so that the common case stays a pure blit.
        """
        if not self._graph_axes:
            return ()
        times = np.fromiter(
            self._log_time, dtype=float, count=len(self._log_time))
        empty = times[:0]
        for (unit, metric), line in self._graph_lines.items():
            history = self._graph_history.get(unit, {}).get(metric)
            if not history:
                line.set_data(empty, empty)
                continue
            # Missing readings become NaN so the trace breaks instead of
            # interpolating across a gap.
            values = np.fromiter(
                (np.nan if value is None else value for value in history),
                dtype=float, count=len(history))
            # Histories added after acquisition starts correspond to the most
            # recent portion of the common time axis.
            count = min(len(values), len(times))
            if count == 0:
                line.set_data(empty, empty)
            else:
                line.set_data(times[-count:], values[-count:])
        return tuple(self._graph_lines.values())

    def _push_configurable_graph_sample(self):
        """Store every acquired metric, independent of what is displayed."""
        generation = self._telemetry_generation
        if generation == self._last_graphed_generation:
            return
        self._last_graphed_generation = generation
        sampled_at = self._latest_sample_timestamp or datetime.now()
        if self._log_t0 is None:
            self._log_t0 = sampled_at
        self._log_time.append((sampled_at - self._log_t0).total_seconds())
        for unit, metric_history in self._graph_history.items():
            sample = self._latest_graph_samples.get(unit, {})
            valve_drives = sample.get('valve_drives') or ()
            for metric, metadata in self.GRAPH_METRICS.items():
                sample_key = metadata[3]
                value = (valve_drives[0] if valve_drives
                         else None) if metric == 'valve_drive' else sample.get(sample_key)
                metric_history[metric].append(value)

    def _clear_configurable_graph_history(self, *, log=True):
        self._log_time.clear()
        self._log_t0 = None
        self._last_graphed_generation = self._telemetry_generation
        for metric_history in self._graph_history.values():
            for history in metric_history.values():
                history.clear()
        if self._canvas is not None:
            self._animate_configurable_graphs(None)
            self._update_graph_limits()
            self._full_redraw_graphs()
        if log:
            self._log("Graph history cleared.")

    def _export_configurable_graph_data(self):
        try:
            import openpyxl
        except ImportError:
            messagebox.showerror(
                "Export", "openpyxl is not installed. Run: pip install openpyxl")
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension='.xlsx', filetypes=[('Excel files', '*.xlsx')],
            title='Export Alicat flow-controller data',
            initialfile=f"alicat_export_{datetime.now():%Y%m%d_%H%M%S}.xlsx")
        if not path:
            return
        try:
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = 'Live Data'
            units = list(self._graph_history)
            header = ['time_s']
            for unit in units:
                metadata = self._graph_unit_meta.get(unit, {})
                name = metadata.get('gas', 'gas')
                for metric, definition in self.GRAPH_METRICS.items():
                    header.append(
                        f"unit_{unit}_{name}_{metric}_{definition[2]}")
            sheet.append(header)
            times = list(self._log_time)
            for row_index, elapsed in enumerate(times):
                row = [elapsed]
                for unit in units:
                    for metric in self.GRAPH_METRICS:
                        values = list(self._graph_history[unit][metric])
                        offset = len(times) - len(values)
                        value_index = row_index - offset
                        row.append(
                            values[value_index]
                            if 0 <= value_index < len(values) else None)
                sheet.append(row)
            workbook.save(path)
            self._log(f"Exported {len(times)} graph samples to {path}")
            messagebox.showinfo("Export", f"Saved:\n{path}")
        except Exception as exc:
            messagebox.showerror("Export", f"Failed:\n{exc}")

    def _rebuild_graphs(self):
        return self._rebuild_configurable_graphs()

    def _animate_graphs(self, _frame=None):
        return self._animate_configurable_graphs(_frame)

    def _push_graph_sample(self):
        return self._push_configurable_graph_sample()

    def _clear_graph_history(self, log=True):
        return self._clear_configurable_graph_history(log=log)

    def _export_to_excel(self):
        return self._export_configurable_graph_data()

    # ================================================================== #
    #  Discrepancy detection                                              #
    # ================================================================== #
    def _on_disc_toggle(self):
        self._disc_enabled=self._disc_enabled_var.get()
        if not self._disc_enabled:
            self._clear_disc_highlights()
            self.disc_status_lbl.config(text=chr(9679)+" Discrepancy monitoring disabled",fg='#555555')
        else:
            self.disc_status_lbl.config(text=chr(9679)+" All flows nominal",fg='#4ade80')

    def _check_discrepancy(self):
        try: threshold=float(self.disc_threshold_entry.get())/100.0
        except Exception: threshold=0.05
        try: suppress=float(self.disc_suppress_entry.get())
        except Exception: suppress=30.0
        now=datetime.now().timestamp(); flagged=[]
        for unit in self.controller_instances:
            sample = self._live_samples.get(unit, {})
            flow = sample.get('flow'); sp = sample.get('sp')
            internal_error = sample.get('internal_error')
            if flow is None or sp is None or internal_error is None:
                self._set_card_highlight(unit,False); continue
            if abs(sp)<0.05:
                self._set_card_highlight(unit,False); continue
            err=abs(internal_error)/abs(sp)
            if err>threshold:
                flagged.append((unit,flow,sp,err)); self._set_card_highlight(unit,True)
                last=self._disc_ignore_until.get(unit,0)
                if now-last>suppress:
                    self._disc_ignore_until[unit]=now
                    self._log(f"Discrepancy Unit {unit}: flow={flow:.2f} sp={sp:.2f} "
                              f"internal_error={internal_error:+.3f} ({err*100:.1f}% off)")
            else: self._set_card_highlight(unit,False)
        if flagged: self.disc_status_lbl.config(text=chr(9679)+f" {len(flagged)} discrepancies",fg='#f87171')
        else: self.disc_status_lbl.config(text=chr(9679)+" All flows nominal",fg='#4ade80')

    def _set_card_highlight(self, unit, flagged):
        for store in (self.cards_tab2,self.cards_tab1):
            if unit in store:
                card=store[unit].get('card')
                if not card: continue
                try:
                    if flagged:
                        if not self._flash_state.get(unit):
                            self._flash_state[unit]=True; self._flash_tick(unit,card)
                    else: self._cancel_flash(unit,card)
                except Exception: pass

    def _flash_tick(self, unit, card):
        if not self._flash_state.get(unit): return
        try:
            current=card.cget('highlightbackground')
            new='#f87171' if current!='#f87171' else '#7a2020'
            card.config(highlightbackground=new,highlightthickness=2)
        except Exception: return
        jid=self.root.after(450, lambda: self._flash_tick(unit,card))
        self._flash_jobs[unit]=jid

    def _apply_card_style(self, unit, color, thickness):
        for store in (self.cards_tab2,self.cards_tab1):
            if unit in store:
                card=store[unit].get('card')
                if card:
                    try: card.config(highlightbackground=color,highlightthickness=thickness)
                    except Exception: pass

    def _cancel_flash(self, unit, card):
        self._flash_state[unit]=False
        jid=self._flash_jobs.pop(unit,None)
        if jid:
            try: self.root.after_cancel(jid)
            except Exception: pass
        try: card.config(highlightbackground='#333333',highlightthickness=1)
        except Exception: pass

    def _clear_disc_highlights(self):
        for unit in list(self._flash_state.keys()):
            for store in (self.cards_tab2,self.cards_tab1):
                if unit in store:
                    card=store[unit].get('card')
                    if card: self._cancel_flash(unit,card)
                    break

    # ================================================================== #
    #  Utility                                                            #
    # ================================================================== #
    def _post_ui(self, callback, *args, **kwargs):
        """Queue a callback for execution by Tk's main thread."""
        if not self._closing:
            self._ui_callback_queue.put((callback, args, kwargs))

    def _submit_serial(self, coroutine, on_done=None):
        """Run a coroutine on the sole serial loop and marshal completion to Tk."""
        future = self._serial_worker.submit(coroutine)
        if on_done is not None:
            future.add_done_callback(
                lambda completed: self._post_ui(on_done, completed))
        return future

    def _drain_ui_queues(self):
        """Apply worker results and log messages without touching Tk off-thread."""
        if self._closing:
            return
        while True:
            try:
                callback, args, kwargs = self._ui_callback_queue.get_nowait()
            except Exception:
                break
            try:
                callback(*args, **kwargs)
            except Exception as exc:
                self._ui_log_queue.put(
                    ("system", datetime.now().strftime("%H:%M:%S"),
                     f"UI callback error: {exc}"))

        while True:
            try:
                target, ts, msg = self._ui_log_queue.get_nowait()
            except Exception:
                break
            try:
                widget = self.conn_log if target == "connection" else self.sys_log
                widget.insert(tk.END, f"[{ts}] {msg}\n")
                widget.see(tk.END)
            except tk.TclError:
                return
        self.root.after(25, self._drain_ui_queues)

    def _log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self._ui_log_queue.put(("system", ts, str(msg)))

    def _animate_stored_cells(self):
        for cell in self._stored_flow_cells:
            try: cell.set_border('#d97706')
            except Exception: pass
        def _restore():
            for cell in self._stored_flow_cells:
                try: cell.set_border('#3a3a34')
                except Exception: pass
        self.root.after(700, _restore)


# ====================================================================== #
#  Module-level helpers                                                    #
# ====================================================================== #
def _enable_hidpi():
    import sys
    if sys.platform != 'win32': return
    try:
        import ctypes
        try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception: ctypes.windll.user32.SetProcessDPIAware()
    except Exception: pass


def _patch_geometry_scaling(root):
    try:
        import sys
        if sys.platform != 'win32': return
        import ctypes
        hdc=ctypes.windll.user32.GetDC(0); LOGPIXELSX=88
        dpi=ctypes.windll.gdi32.GetDeviceCaps(hdc,LOGPIXELSX)
        ctypes.windll.user32.ReleaseDC(0,hdc)
        root.tk.call('tk','scaling',dpi/72.0)
    except Exception: pass


def main():
    _enable_hidpi()
    root=tk.Tk()
    _patch_geometry_scaling(root)
    app=AlicatDetectorUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
