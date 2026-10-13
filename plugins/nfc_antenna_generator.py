"""
NFC Antenna Generator —— KiCad PCB 插件

按 NFC 布线规范一键生成 NFC/HF 天线封装，并可一键放置到当前 PCB。

规范依据见 nfc_geometry.py 顶部说明（ST AN2972 + KiCad 官方 RF_Antenna 库范式）。

兼容 KiCad 8.0 ~ 11（SWIG Action Plugin）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import wx
import pcbnew

# --- 导入同目录的几何模块（兼容两种加载方式）---
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    from . import nfc_geometry as G
except Exception:  # noqa: BLE001 - 被当作顶层模块加载时
    import nfc_geometry as G  # type: ignore

MM = pcbnew.FromMM
PLUGIN_VERSION = "1.1.0"

SILK_W = 0.12
FAB_W = 0.10
CRTYD_W = 0.05
KEEPOUT_W = 0.10


# ==========================================================================
#  KiCad 版本兼容层
# ==========================================================================
# KiCad 10 起 PAD.SetAnchorPadShape / PAD.AddPrimitivePoly 多了一个前置的
# 图层参数（KiCad 8/9 没有）。这里靠「先按旧签名调，TypeError 则用新签名」
# 自适应，比读 docstring 更稳。

def _set_anchor_pad_shape(pad, shape, layer) -> None:
    """KiCad 8/9: SetAnchorPadShape(shape)；KiCad 10+: (layer, shape)。"""
    try:
        pad.SetAnchorPadShape(shape)
    except TypeError:
        pad.SetAnchorPadShape(layer, shape)


def _add_poly_primitive(pad, pts, layer) -> None:
    """KiCad 8/9: AddPrimitivePoly(pts, w, filled)；KiCad 10+: (layer, pts, w, filled)。"""
    try:
        pad.AddPrimitivePoly(pts, 0, True)
    except TypeError:
        pad.AddPrimitivePoly(layer, pts, 0, True)


# ==========================================================================
#  封装构建
# ==========================================================================
def _vec(x_mm: float, y_mm: float) -> pcbnew.VECTOR2I:
    return pcbnew.VECTOR2I(MM(x_mm), MM(y_mm))


def _add_polygon(fp, points, layer, width: float = 0.0) -> None:
    if len(points) < 3:
        return
    sp = pcbnew.SHAPE_POLY_SET()
    sp.NewOutline()
    for (x, y) in points:
        sp.Append(MM(x), MM(y))
    shp = pcbnew.PCB_SHAPE(fp)
    shp.SetShape(pcbnew.SHAPE_T_POLY)
    shp.SetPolyShape(sp)
    shp.SetFilled(True)
    shp.SetLayer(layer)
    shp.SetWidth(MM(width))
    fp.Add(shp)


def _add_lines(fp, segs, layer, width: float) -> None:
    for (a, b) in segs:
        shp = pcbnew.PCB_SHAPE(fp)
        shp.SetShape(pcbnew.SHAPE_T_SEGMENT)
        shp.SetStart(_vec(a[0], a[1]))
        shp.SetEnd(_vec(b[0], b[1]))
        shp.SetWidth(MM(width))
        shp.SetLayer(layer)
        fp.Add(shp)


def _add_pad(fp, number: str, x_mm: float, y_mm: float,
             size_mm: float, drill_mm: float) -> None:
    pad = pcbnew.PAD(fp)
    pad.SetNumber(number)
    pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
    pad.SetSize(pcbnew.VECTOR2I(MM(size_mm), MM(size_mm)))

    ls = pcbnew.LSET()
    if drill_mm > 0:
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetDrillSize(pcbnew.VECTOR2I(MM(drill_mm), MM(drill_mm)))
        ls = pcbnew.LSET.AllCuMask()
        ls.AddLayer(pcbnew.F_Mask)
        ls.AddLayer(pcbnew.B_Mask)
    else:
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        ls.AddLayer(pcbnew.F_Cu)
        ls.AddLayer(pcbnew.F_Paste)
        ls.AddLayer(pcbnew.F_Mask)
    pad.SetLayerSet(ls)
    pad.SetPosition(_vec(x_mm, y_mm))
    fp.Add(pad)


def _add_return_pad(fp, anchor, polygon, width_mm: float, number: str = "2") -> None:
    """B.Cu 回线：用自定义焊盘表达，因此这根铜箔带焊盘网络（纯图形不参与连通）。

    注意：这里用 AddPrimitivePoly（按值拷贝）而非 AddPrimitiveShape，避免
    SWIG 临时对象被回收后图元内存被后续图形复用的问题。
    """
    pad = pcbnew.PAD(fp)
    pad.SetNumber(number)
    pad.SetShape(pcbnew.PAD_SHAPE_CUSTOM)
    _set_anchor_pad_shape(pad, pcbnew.PAD_SHAPE_CIRCLE, pcbnew.B_Cu)
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    size = max(width_mm, 0.4)
    pad.SetSize(pcbnew.VECTOR2I(MM(size), MM(size)))
    ls = pcbnew.LSET()
    ls.AddLayer(pcbnew.B_Cu)
    ls.AddLayer(pcbnew.B_Mask)
    pad.SetLayerSet(ls)
    pad.SetPosition(_vec(anchor[0], anchor[1]))

    pts = [pcbnew.VECTOR2I(MM(x - anchor[0]), MM(y - anchor[1])) for (x, y) in polygon]
    _add_poly_primitive(pad, pts, pcbnew.B_Cu)
    fp.Add(pad)


def build_footprint(geo: "G.AntennaGeometry", board=None) -> pcbnew.FOOTPRINT:
    """按几何数据构建 FOOTPRINT（原点在天线铜箔左上角）。"""
    p = geo.params
    fp = pcbnew.FOOTPRINT(board)

    fp.SetFPID(pcbnew.LIB_ID("", p.name))
    fp.SetReference("AE**")
    fp.SetValue(p.name)
    fp.SetAttributes(pcbnew.FP_EXCLUDE_FROM_POS_FILES | pcbnew.FP_EXCLUDE_FROM_BOM)
    try:
        fp.SetDescription(
            f"NFC/HF 天线, {p.turns} 匝, {p.length:g}x{p.width:g}mm, "
            f"线宽{p.conductor_width:g}/线距{p.conductor_space:g}mm"
        )
        fp.SetKeywords("NFC HF RFID antenna 13.56MHz coil 天线")
    except Exception:  # noqa: BLE001
        pass

    # ---- 参考号 / 值 ----
    ref = fp.Reference()
    ref.SetLayer(pcbnew.F_SilkS)
    ref.SetTextSize(pcbnew.VECTOR2I(MM(1.0), MM(1.0)))
    ref.SetPosition(_vec(*geo.ref_pos))
    val = fp.Value()
    val.SetLayer(pcbnew.F_Fab)
    val.SetTextSize(pcbnew.VECTOR2I(MM(1.0), MM(1.0)))
    val.SetPosition(_vec(*geo.value_pos))

    # ---- 铜箔 ----
    _add_polygon(fp, geo.coil_polygon, pcbnew.F_Cu)

    # ---- 接线焊盘 ----
    pad_size = max(p.pad_size, p.conductor_width)
    drill = p.pad_drill
    t1 = geo.terminals["1"]
    _add_pad(fp, "1", t1[0], t1[1], pad_size, drill)

    t2 = geo.terminals["2"]
    if p.terminal == "outer":
        # 回线：B.Cu 自定义焊盘（带网络）+ 两个通孔焊盘充当过孔
        ret_w = max(p.conductor_width, p.via_drill + 0.2)
        if geo.return_polygon:
            _add_return_pad(fp, t2, geo.return_polygon, ret_w, "2")
        via_size = max(p.via_drill + 0.4, 0.8)
        for (vx, vy) in geo.vias:
            _add_pad(fp, "2", vx, vy, via_size, max(p.via_drill, 0.2))
    else:
        _add_pad(fp, "2", t2[0], t2[1], pad_size, drill)

    # ---- 注释线 ----
    _add_lines(fp, geo.silk, pcbnew.F_SilkS, SILK_W)
    _add_lines(fp, geo.fab, pcbnew.F_Fab, FAB_W)
    _add_lines(fp, geo.courtyard, pcbnew.F_CrtYd, CRTYD_W)
    _add_lines(fp, geo.keepout, pcbnew.Dwgs_User, KEEPOUT_W)

    # ---- 天线是「净绑器件」：两焊盘由铜箔相连，声明后不报短路 ----
    fp.AddNetTiePadGroup("1,2")
    return fp


def default_library_dir() -> str:
    """默认封装库路径：<文档>/KiCad/<版本>/footprints/NFC_Antenna.pretty"""
    try:
        ver = str(pcbnew.GetBuildVersion())
        major = ver.split(".")[0]
    except Exception:  # noqa: BLE001
        major = "8"
    return os.path.join(
        os.path.expanduser("~"), "Documents", "KiCad", f"{major}.0",
        "footprints", "NFC_Antenna.pretty",
    )


def next_reference(board, prefix: str = "AE") -> str:
    """在现有板上找一个未占用的参考号（备用）。"""
    used = set()
    try:
        for f in board.GetFootprints():
            used.add(f.GetReference())
    except Exception:  # noqa: BLE001
        pass
    i = 1
    while f"{prefix}{i}" in used:
        i += 1
    return f"{prefix}{i}"


# --------------------------------------------------------------------------
#  剪贴板：让 KiCad 原生粘贴接管，封装就会跟随鼠标
# --------------------------------------------------------------------------
# 依据 KiCad 源码 pcbnew/kicad_clipboard.cpp：PCB 编辑器的剪贴板格式就是
# **纯文本 wxTextDataObject**，内容是裸的 (footprint ...) s-expression。
# 所以只要把 .kicad_mod 文本写进系统剪贴板，再按 Ctrl+V，KiCad 就会把它
# 当成复制/粘贴的对象，自然地跟随鼠标。

def footprint_s_expr(geo: "G.AntennaGeometry") -> str:
    """把封装导出为 KiCad s-expression 文本（与剪贴板内容一致）。"""
    fp = build_footprint(geo, None)
    tmp = tempfile.mkdtemp(prefix="nfc_ant_")
    try:
        lib = os.path.join(tmp, "tmp.pretty")
        os.makedirs(lib, exist_ok=True)
        pcbnew.PCB_IO_KICAD_SEXPR().FootprintSave(lib, fp)
        mods = [n for n in os.listdir(lib) if n.endswith(".kicad_mod")]
        if not mods:
            raise RuntimeError("导出封装文本失败")
        with open(os.path.join(lib, mods[0]), "r", encoding="utf-8") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verify_s_expr(text: str) -> bool:
    """用 KiCad 自己的解析器回读，确认文本能当作封装粘贴。"""
    try:
        return pcbnew.PCB_IO_KICAD_SEXPR().Parse(text) is not None
    except Exception:  # noqa: BLE001
        return False


def copy_text_to_clipboard(text: str) -> None:
    """写入系统剪贴板（纯文本，与 KiCad 复制封装时的格式一致）。

    注意：Windows 剪贴板可能被其它进程短暂占用，此时 wx 会产生日志/断言，
    严重时会 abort 整个进程。KiCad 自己在做剪贴板操作时用 wxLogNull 抑制，
    这里照做，并加少量重试。
    """
    log_null = wx.LogNull()          # 抑制剪贴板被占用时的报错（同 KiCad 源码）
    try:
        err = RuntimeError("无法打开系统剪贴板")
        for _ in range(6):
            if wx.TheClipboard.Open():
                try:
                    wx.TheClipboard.SetData(wx.TextDataObject(text))
                    # Flush 让数据真正落到系统剪贴板（KiCad 源码同样这么写）
                    wx.TheClipboard.Flush()
                finally:
                    wx.TheClipboard.Close()
                return
            time.sleep(0.06)
        raise err
    finally:
        del log_null


# ==========================================================================
#  预览面板
# ==========================================================================
class PreviewPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent, size=(360, 320))
        self.geo = None
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def set_geometry(self, geo):
        self.geo = geo
        self.Refresh(False)

    def _on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.Colour(18, 42, 36)))
        dc.Clear()
        geo = self.geo
        if not geo:
            return

        xs, ys = [], []
        for poly in (geo.coil_polygon, geo.return_polygon):
            for (x, y) in poly:
                xs.append(x)
                ys.append(y)
        for segs in (geo.keepout, geo.courtyard, geo.silk):
            for (a, b) in segs:
                xs += [a[0], b[0]]
                ys += [a[1], b[1]]
        for (x, y) in geo.terminals.values():
            xs.append(x)
            ys.append(y)
        if not xs:
            return
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        w = max(x1 - x0, 1e-3)
        h = max(y1 - y0, 1e-3)

        cw, ch = self.GetClientSize()
        scale = min((cw - 24) / w, (ch - 24) / h)
        ox = (cw - w * scale) / 2 - x0 * scale
        oy = (ch - h * scale) / 2 - y0 * scale

        def P(x, y):
            return wx.Point(int(x * scale + ox), int(y * scale + oy))

        # 禁铺铜区
        dc.SetPen(wx.Pen(wx.Colour(70, 130, 110), 1))
        for (a, b) in geo.keepout:
            dc.DrawLine(P(*a), P(*b))
        # 丝印
        dc.SetPen(wx.Pen(wx.Colour(255, 224, 102), 1))
        for (a, b) in geo.silk:
            dc.DrawLine(P(*a), P(*b))

        # 线圈（F.Cu 实心多边形）
        for poly, colour in ((geo.coil_polygon, wx.Colour(200, 89, 43)),
                             (geo.return_polygon, wx.Colour(43, 107, 200))):
            if len(poly) >= 3:
                dc.SetPen(wx.Pen(colour, 1))
                dc.SetBrush(wx.Brush(colour))
                dc.DrawPolygon([P(x, y) for (x, y) in poly])

        # 焊盘
        pr = max(geo.params.pad_size, geo.params.conductor_width) / 2 * scale
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.SetPen(wx.Pen(wx.Colour(25, 211, 255), 2))
        for name, (x, y) in geo.terminals.items():
            p = P(x, y)
            dc.DrawCircle(p, max(int(pr), 3))
            dc.DrawText(name, wx.Point(p.x - 3, p.y - 6))
        # 过孔
        dc.SetBrush(wx.Brush(wx.Colour(25, 211, 255)))
        for (x, y) in geo.vias:
            dc.DrawCircle(P(x, y), 3)


# ==========================================================================
#  参数对话框
# ==========================================================================
class NfcAntennaDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title=f"NFC 天线生成器  v{PLUGIN_VERSION}",
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = pcbnew.GetBoard()
        self._copied = False

        self.txt_name = wx.TextCtrl(self, value="NFC_ANT")
        self.sp_turns = wx.SpinCtrl(self, min=1, max=40, initial=4)
        self.sp_len = wx.SpinCtrlDouble(self, min=5, max=500, inc=0.5, initial=45.0)
        self.sp_wid = wx.SpinCtrlDouble(self, min=5, max=500, inc=0.5, initial=45.0)
        self.sp_cw = wx.SpinCtrlDouble(self, min=0.1, max=10, inc=0.05, initial=0.6)
        self.sp_cs = wx.SpinCtrlDouble(self, min=0.1, max=10, inc=0.05, initial=0.5)
        self.sp_ch = wx.SpinCtrlDouble(self, min=0, max=20, inc=0.1, initial=0.0)
        self.sp_ko = wx.SpinCtrlDouble(self, min=0, max=50, inc=0.5, initial=2.0)
        self.cb_term = wx.Choice(self, choices=["内侧端子（单层，推荐标签）",
                                                "外侧双焊盘（B.Cu 过孔跨接，推荐阅读器）"])
        self.cb_term.SetSelection(0)
        self.sp_via = wx.SpinCtrlDouble(self, min=0.1, max=2, inc=0.05, initial=0.3)
        self.sp_pdrill = wx.SpinCtrlDouble(self, min=0, max=5, inc=0.1, initial=0.0)
        self.sp_psize = wx.SpinCtrlDouble(self, min=0.4, max=10, inc=0.1, initial=1.2)
        self.sp_cu = wx.SpinCtrlDouble(self, min=5, max=210, inc=5, initial=35.0)
        self.sp_silk = wx.SpinCtrlDouble(self, min=-1, max=20, inc=0.5, initial=1.0)
        self.txt_lib = wx.TextCtrl(self, value=default_library_dir())
        self.btn_browse = wx.Button(self, label="浏览…")

        self.preview = PreviewPanel(self)
        self.lbl_info = wx.StaticText(self, label="", size=(380, 150))
        self.lbl_msg = wx.StaticText(self, label="")
        self.btn_save = wx.Button(self, label="生成封装")
        self.btn_place = wx.Button(self, label="生成并复制（去粘贴）")
        self.btn_close = wx.Button(self, id=wx.ID_CANCEL, label="关闭")

        self._build_layout()
        self._bind_events()
        self._refresh()

    # ---------------- 布局 ----------------
    def _build_layout(self):
        form = wx.FlexGridSizer(rows=0, cols=2, vgap=5, hgap=8)
        form.AddGrowableCol(1, 1)

        def row(label, ctrl):
            form.Add(wx.StaticText(self, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            form.Add(ctrl, 1, wx.EXPAND)

        row("封装名称", self.txt_name)
        row("匝数 N", self.sp_turns)
        row("天线长 L (mm)", self.sp_len)
        row("天线宽 W (mm)", self.sp_wid)
        row("线宽 (mm)", self.sp_cw)
        row("线间距 (mm)", self.sp_cs)
        row("转角 45°倒角 (mm)", self.sp_ch)
        row("端子方式", self.cb_term)
        row("过孔钻径 (mm)", self.sp_via)
        row("焊盘钻径 (mm, 0=SMD)", self.sp_pdrill)
        row("焊盘直径 (mm)", self.sp_psize)
        row("铜厚 (µm)", self.sp_cu)
        row("丝印边距 (mm, <0 不画)", self.sp_silk)
        row("禁铺铜外扩 (mm)", self.sp_ko)
        row("封装库目录", self.txt_lib)
        form.Add(wx.StaticText(self, label=""))
        form.Add(self.btn_browse, 0)

        left = wx.BoxSizer(wx.VERTICAL)
        left.Add(wx.StaticText(self, label="① 参数"), 0, wx.BOTTOM, 4)
        left.Add(form, 1, wx.EXPAND)

        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(wx.StaticText(self, label="② 预览 / 估算"), 0, wx.BOTTOM, 4)
        right.Add(self.preview, 0, wx.EXPAND)
        right.Add(self.lbl_info, 0, wx.TOP | wx.EXPAND, 6)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        btns.Add(self.btn_save, 0)
        btns.Add(self.btn_place, 0, wx.LEFT, 8)
        btns.AddStretchSpacer(1)
        btns.Add(self.btn_close, 0)

        body = wx.BoxSizer(wx.HORIZONTAL)
        body.Add(left, 3, wx.EXPAND | wx.RIGHT, 10)
        body.Add(right, 4, wx.EXPAND)

        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(body, 1, wx.EXPAND | wx.ALL, 10)
        root.Add(self.lbl_msg, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.lbl_hint = wx.StaticText(
            self,
            label="提示：点「生成并复制」后回到 PCB 编辑器按 Ctrl+V —— 封装会跟随鼠标，单击左键放下。")
        root.Add(self.lbl_hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        root.Add(btns, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        self.SetSizerAndFit(root)
        self.SetSize((960, 620))
        self.Centre()

    def _bind_events(self):
        for ctrl in (self.sp_turns, self.sp_len, self.sp_wid, self.sp_cw, self.sp_cs,
                     self.sp_ch, self.sp_ko, self.sp_via, self.sp_pdrill,
                     self.sp_psize, self.sp_cu, self.sp_silk):
            ctrl.Bind(wx.EVT_SPINCTRLDOUBLE, lambda e: self._refresh())
            ctrl.Bind(wx.EVT_SPINCTRL, lambda e: self._refresh())
            ctrl.Bind(wx.EVT_TEXT, lambda e: self._refresh())
        self.txt_name.Bind(wx.EVT_TEXT, lambda e: self._refresh())
        self.cb_term.Bind(wx.EVT_CHOICE, lambda e: self._refresh())
        self.btn_browse.Bind(wx.EVT_BUTTON, self._on_browse)
        self.btn_save.Bind(wx.EVT_BUTTON, self._on_save)
        self.btn_place.Bind(wx.EVT_BUTTON, self._on_copy)

    # ---------------- 参数 ----------------
    def _params(self) -> G.AntennaParams:
        return G.AntennaParams(
            name=self.txt_name.GetValue().strip() or "NFC_ANT",
            turns=int(self.sp_turns.GetValue()),
            length=float(self.sp_len.GetValue()),
            width=float(self.sp_wid.GetValue()),
            conductor_width=float(self.sp_cw.GetValue()),
            conductor_space=float(self.sp_cs.GetValue()),
            chamfer=float(self.sp_ch.GetValue()),
            terminal="outer" if self.cb_term.GetSelection() == 1 else "inner",
            via_drill=float(self.sp_via.GetValue()),
            pad_drill=float(self.sp_pdrill.GetValue()),
            pad_size=float(self.sp_psize.GetValue()),
            copper_thickness=float(self.sp_cu.GetValue()),
            silk_margin=float(self.sp_silk.GetValue()),
            keepout_margin=float(self.sp_ko.GetValue()),
        )

    def _refresh(self):
        try:
            params = self._params()
            errs = G.validate(params)
            if errs:
                self.preview.set_geometry(None)
                self.lbl_info.SetLabel("")
                self._msg("；".join(errs), error=True)
                return None
            geo = G.build(params)
        except Exception as exc:  # noqa: BLE001
            self.preview.set_geometry(None)
            self.lbl_info.SetLabel("")
            self._msg(f"参数错误：{exc}", error=True)
            return None

        self.preview.set_geometry(geo)
        e = geo.estimate
        lines = [
            f"估算电感 L ≈ {e.inductance_uH:.3f} µH",
            f"交流电阻 R ≈ {e.resistance_ohm:.2f} Ω（直流 {e.resistance_dc_ohm:.2f} Ω）",
            f"Q ≈ {e.q:.0f}（理想上限，未含介质/邻近损耗）",
            f"13.56MHz 谐振电容 ≈ {e.tune_cap_pF:.1f} pF",
            f"导体总长 ≈ {e.trace_length_mm:.1f} mm",
            f"趋肤深度 ≈ {e.skin_depth_um:.1f} µm（铜厚 {params.copper_thickness:g} µm）",
            "",
            f"焊盘：1 @ ({geo.terminals['1'][0]:.2f}, {geo.terminals['1'][1]:.2f})   "
            f"2 @ ({geo.terminals['2'][0]:.2f}, {geo.terminals['2'][1]:.2f})",
        ]
        warn = G.warnings(params, e)
        if warn:
            lines.append("")
            lines.append("提示：")
            lines += [f"· {w}" for w in warn]
        self.lbl_info.SetLabel("\n".join(lines))
        self._msg("参数正常，可生成。" + (f"（{len(warn)} 条提示）" if warn else ""))
        self.Layout()
        return geo

    def _msg(self, text: str, error: bool = False):
        self.lbl_msg.SetLabel(text)
        self.lbl_msg.SetForegroundColour(wx.Colour(200, 40, 40) if error
                                         else wx.Colour(30, 110, 60))
        self.lbl_msg.Refresh()

    def _on_browse(self, _evt):
        dlg = wx.DirDialog(self, "选择封装库目录（*.pretty）",
                           defaultPath=os.path.dirname(self.txt_lib.GetValue()))
        if dlg.ShowModal() == wx.ID_OK:
            self.txt_lib.SetValue(dlg.GetPath())
        dlg.Destroy()

    # ---------------- 动作 ----------------
    def _on_save(self, _evt):
        geo = self._refresh()
        if not geo:
            return
        lib = self.txt_lib.GetValue().strip()
        if not lib:
            self._msg("请先指定封装库目录。", error=True)
            return
        try:
            if not os.path.isdir(lib):
                os.makedirs(lib, exist_ok=True)
            fp = build_footprint(geo, None)
            pcbnew.PCB_IO_KICAD_SEXPR().FootprintSave(lib, fp)
        except Exception as exc:  # noqa: BLE001
            self._msg(f"保存失败：{exc}", error=True)
            return
        path = os.path.join(lib, f"{geo.params.name}.kicad_mod")
        self._msg(f"已生成封装：{path}")
        wx.MessageBox(f"封装已保存到：\n{path}\n\n"
                      "可在 PCB 编辑器中用「添加封装」放置，"
                      "或在封装库表中添加该目录。",
                      "生成完成", wx.OK | wx.ICON_INFORMATION, self)

    def _on_copy(self, _evt):
        """把封装文本写进系统剪贴板；随后用户按 Ctrl+V，封装会跟随鼠标。"""
        geo = self._refresh()
        if not geo:
            return
        try:
            text = footprint_s_expr(geo)
            if not verify_s_expr(text):
                raise RuntimeError("导出文本未通过 KiCad 解析自检")
            copy_text_to_clipboard(text)
        except Exception as exc:  # noqa: BLE001
            self._msg(f"复制到剪贴板失败：{exc}", error=True)
            return
        self._copied = True
        self.EndModal(wx.ID_OK)


# ==========================================================================
#  插件注册
# ==========================================================================
class NfcAntennaGenerator(pcbnew.ActionPlugin):
    """NFC 天线生成器"""

    def defaults(self):
        self.name = "生成 NFC 天线"
        self.category = "RF / 天线"
        self.description = ("按 NFC 布线规范生成 NFC/HF 天线封装；"
                            "生成后复制到剪贴板，粘贴时跟随鼠标放置")
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(_HERE, "icon.png")

    def Run(self):
        if pcbnew.GetBoard() is None:
            wx.MessageBox("请先打开一个 PCB 文件。", "NFC 天线生成器",
                          wx.OK | wx.ICON_WARNING)
            return
        dlg = NfcAntennaDialog(None)
        dlg.ShowModal()
        copied = getattr(dlg, "_copied", False)
        dlg.Destroy()
        if copied:
            wx.MessageBox(
                "封装已复制到剪贴板。\n\n"
                "现在按 Ctrl+V 粘贴 —— 封装会跟着鼠标走，单击左键放下。\n\n"
                "提示：天线正上/正下与四周禁止铺铜（封装的 Dwgs.User 框已标出）。",
                "NFC 天线生成器", wx.OK | wx.ICON_INFORMATION)


try:
    NfcAntennaGenerator().register()
except Exception:  # noqa: BLE001 - 重复注册时静默忽略
    pass
