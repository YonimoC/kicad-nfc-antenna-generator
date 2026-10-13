"""
NFC 天线几何生成（纯数学，不依赖 pcbnew / wx）。

坐标系：封装局部坐标，单位 mm，原点位于天线铜箔外框左上角，
x 向右、y 向下（与 KiCad 封装坐标一致）。

设计依据（NFC 布线规范）
------------------------
* ST AN2972《How to design an antenna for your ST25R NFC reader》
  - §3.2 方形/矩形天线电感（Modified Wheeler）：
        L = K1 · µ0 · N² · d / (1 + K2 · p)
        d = (dout + din) / 2,  p = (dout − din) / (dout + din)
        方形 K1 = 2.34, K2 = 2.75
  - §3.4.1 芯片应尽量靠近天线（数 mm 内）。
  - §3.4.2 天线正上/正下与四周都不允许有铜（地平面）；天线周围不允许短路环。
* KiCad 官方库 RF_Antenna.pretty/Texas_SWRA117D_2.4GHz_Left.kicad_mod：
  天线铜箔用 **F.Cu 实心铜多边形 (fp_poly / fill solid)** 表达，两个焊盘用
  **net_tie_pad_groups** 声明为同一导体（天线本身即一个「净绑器件」），并设置
  exclude_from_pos_files / exclude_from_bom。本插件沿用该范式，因此不会产生
  「Pad near pad / 短路」类 DRC 误报。

拓扑说明
--------
单层平面螺旋天然只有「一端在内、一端在外」。
* terminal = "inner"（默认）：pad1 = 外侧端子，pad2 = 内侧端子。
  内侧端子可用过孔或把芯片直接放进线圈内连接（推荐 NFC 标签）。
* terminal = "outer"：内侧端子经 **B.Cu 回线 + 两个过孔**引到外侧，
  两个接线焊盘都在天线外（推荐 NFC 阅读器，便于接匹配网络）。

注意：KiCad 8 的封装文件不允许包含过孔（已实测），因此本插件用「通孔焊盘」
充当过孔，与官方封装做法一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Point = Tuple[float, float]
EPS = 1e-9

# ---- 物理常数 ----
MU0 = 4 * math.pi * 1e-7
F_NFC = 13.56e6                    # Hz
OMEGA = 2 * math.pi * F_NFC
RHO_CU = 1.72e-8                   # Ω·m @20°C

# Modified Wheeler 系数（方形，ST AN2972 Table 2）
K1_SQUARE = 2.34
K2_SQUARE = 2.75


@dataclass
class AntennaParams:
    """天线参数（长度单位 mm，铜厚单位 µm）。"""

    name: str = "NFC_ANT"
    turns: int = 4                 # 匝数 N
    length: float = 45.0           # 铜箔外框长 (X)
    width: float = 45.0            # 铜箔外框宽 (Y)
    conductor_width: float = 0.6   # 线宽
    conductor_space: float = 0.5   # 线间距
    chamfer: float = 0.0           # 转角 45° 倒角长度，0 = 直角
    exit_edge: str = "short"       # 出线边: "long"(长边=上下边) | "short"(宽边=左右边)
    exit_pos: str = "down"         # 出线位置: "up"(上端) | "mid"(中点) | "down"(下端)
    terminal: str = "inner"        # "inner" | "outer"
    via_drill: float = 0.3         # outer 模式过孔钻径
    pad_drill: float = 0.0         # 接线焊盘钻径，0 = SMD
    pad_size: float = 1.2          # 接线焊盘直径
    copper_thickness: float = 35.0 # 铜厚 (µm)，1oz = 35
    silk_margin: float = 1.0       # 丝印到铜箔外沿距离，<0 = 不画
    keepout_margin: float = 2.0    # 禁铺铜提示框外扩距离，<0 = 不画

    def __post_init__(self) -> None:
        if self.terminal not in ("inner", "outer"):
            raise ValueError("terminal 必须是 'inner' 或 'outer'")
        if self.exit_edge not in ("long", "short"):
            raise ValueError("exit_edge 必须是 'long' 或 'short'")
        if self.exit_pos not in ("up", "mid", "down"):
            raise ValueError("exit_pos 必须是 'up' / 'mid' / 'down'")


@dataclass
class Estimate:
    """天线电参数估算（设计参考，非精确仿真）。"""

    inductance_uH: float
    resistance_ohm: float          # 含趋肤效应的交流电阻
    resistance_dc_ohm: float       # 直流电阻
    skin_depth_um: float
    q: float                       # Q 为理想上限（未含介质/邻近损耗）
    tune_cap_pF: float
    trace_length_mm: float
    outer_avg_mm: float
    inner_avg_mm: float


@dataclass
class AntennaGeometry:
    params: AntennaParams
    coil_polygon: List[Point]                       # F.Cu 天线铜箔多边形
    terminals: Dict[str, Point]                     # "1"/"2" -> 位置
    return_polygon: List[Point] = field(default_factory=list)   # B.Cu 回线
    return_centerline: List[Point] = field(default_factory=list)  # 回线中心线（自定义焊盘用）
    vias: List[Point] = field(default_factory=list) # 过孔位置（outer）
    silk: List[Tuple[Point, Point]] = field(default_factory=list)
    fab: List[Tuple[Point, Point]] = field(default_factory=list)
    courtyard: List[Tuple[Point, Point]] = field(default_factory=list)
    keepout: List[Tuple[Point, Point]] = field(default_factory=list)
    ref_pos: Point = (0.0, 0.0)
    value_pos: Point = (0.0, 0.0)
    estimate: Optional[Estimate] = None
    back_corner_k: float = 0.0


# --------------------------------------------------------------------------
#  螺旋中心线
# --------------------------------------------------------------------------
def _next_corner_cw(P: Point, xL: float, xR: float, yT: float, yB: float,
                    tol: float = 1e-6) -> Tuple[Point, Point]:
    """顺时针绕行时，从点 P 沿当前边遇到的下一个角点及其前进方向。"""
    x, y = P
    on_left = abs(x - xL) <= tol
    on_right = abs(x - xR) <= tol
    on_top = abs(y - yT) <= tol
    on_bottom = abs(y - yB) <= tol
    if on_left and on_top:
        return (xR, yT), (1.0, 0.0)
    if on_right and on_top:
        return (xR, yB), (0.0, 1.0)
    if on_right and on_bottom:
        return (xL, yB), (-1.0, 0.0)
    if on_left and on_bottom:
        return (xL, yT), (0.0, -1.0)
    if on_left:
        return (xL, yT), (0.0, -1.0)      # 左边向上
    if on_top:
        return (xR, yT), (1.0, 0.0)       # 顶边向右
    if on_right:
        return (xR, yB), (0.0, 1.0)       # 右边向下
    if on_bottom:
        return (xL, yB), (-1.0, 0.0)      # 底边向左
    # 浮点误差导致不在边上：投影到最近的边再重试
    dl, dr = abs(x - xL), abs(x - xR)
    dt, db = abs(y - yT), abs(y - yB)
    m = min(dl, dr, dt, db)
    if m == dl:
        x = xL
    elif m == dr:
        x = xR
    elif m == dt:
        y = yT
    else:
        y = yB
    return _next_corner_cw((x, y), xL, xR, yT, yB, tol)


def _walk_rect_cw(pts: List[Point], start: Point, xL: float, xR: float,
                  yT: float, yB: float, arc_limit: float) -> Tuple[Point, Point]:
    """从 start 沿矩形顺时针绕行 arc_limit 长度，把途经角点与终点追加到 pts。

    返回 (终点, 终点处的行进方向)。
    """
    P = start
    traveled = 0.0
    guard = 0
    while guard < 64:
        guard += 1
        nxt, d = _next_corner_cw(P, xL, xR, yT, yB)
        seg = math.hypot(nxt[0] - P[0], nxt[1] - P[1])
        if seg <= EPS:
            P = nxt
            continue
        if traveled + seg >= arc_limit:
            remain = arc_limit - traveled
            u = ((nxt[0] - P[0]) / seg, (nxt[1] - P[1]) / seg)
            end = (P[0] + u[0] * remain, P[1] + u[1] * remain)
            if math.hypot(end[0] - pts[-1][0], end[1] - pts[-1][1]) > EPS:
                pts.append(end)
            return end, u
        if math.hypot(nxt[0] - pts[-1][0], nxt[1] - pts[-1][1]) > EPS:
            pts.append(nxt)
        traveled += seg
        P = nxt
    return P, (1.0, 0.0)


def spiral_centerline(p: AntennaParams) -> List[Point]:
    """生成单层矩形螺旋中心线（外端子 → 内端子）。

    起点（= 接线焊盘所在位置）由 p.exit_edge / p.exit_pos 决定。

    原理：沿同心矩形 R(k)（每层向内缩一个 pitch）**顺时针**绕行，
    每层走「周长 − pitch」后垂直向内跨一个 pitch 进入下一层。
    因为换层的接缝始终落在起点的角向位置，所以无论起点在边的哪个
    位置，绕向都一致、不会自交。

    坐标：KiCad 封装局部坐标，y 向下。
    """
    L, W = p.length, p.width
    cW, cS, N = p.conductor_width, p.conductor_space, p.turns
    step = cW + cS
    half = cW / 2.0

    def off(k: float) -> float:
        return half + k * step

    t = {"up": 0.0, "mid": 0.5, "down": 1.0}[p.exit_pos]
    if p.exit_edge == "long":       # 长边 = 底边，x 从左到右
        start: Point = (off(0) + t * (L - 2 * off(0)), W - off(0))
    else:                            # 宽边 = 左边，y 从上到下
        start = (off(0), off(0) + t * (W - 2 * off(0)))

    pts: List[Point] = [start]
    cur = start
    for k in range(N):
        xL, xR = off(k), L - off(k)
        yT, yB = off(k), W - off(k)
        perimeter = 2.0 * ((xR - xL) + (yB - yT))
        arc = max(perimeter - step, 0.0)
        cur, d = _walk_rect_cw(pts, cur, xL, xR, yT, yB, arc)
        if k < N - 1:
            # 换层：优先用 45° 斜坡（一个斜段代替两个直角，更平滑、少电流拥挤）；
            # 起点在角上时斜坡会越界，退回垂直跨步（与标准矩形螺旋一致）。
            inward = (-d[1], d[0])
            xL1, xR1 = off(k + 1), L - off(k + 1)
            yT1, yB1 = off(k + 1), W - off(k + 1)
            diag = (cur[0] + d[0] * step + inward[0] * step,
                    cur[1] + d[1] * step + inward[1] * step)
            if (xL1 - 1e-6 <= diag[0] <= xR1 + 1e-6 and
                    yT1 - 1e-6 <= diag[1] <= yB1 + 1e-6):
                nxt = diag
            else:
                nxt = (cur[0] + inward[0] * step, cur[1] + inward[1] * step)
            pts.append(nxt)
            cur = nxt
    return pts


def _unit(a: Point, b: Point) -> Point:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    if n < EPS:
        return (0.0, 0.0)
    return (dx / n, dy / n)


def _left_normal(d: Point) -> Point:
    return (-d[1], d[0])


def _line_intersect(p: Point, d: Point, q: Point, e: Point) -> Optional[Point]:
    denom = d[0] * e[1] - d[1] * e[0]
    if abs(denom) < 1e-12:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / denom
    return (p[0] + t * d[0], p[1] + t * d[1])


def chamfer_polyline(pts: List[Point], ch: float) -> List[Point]:
    """把折线的内部转角切成 45° 倒角。"""
    if ch <= EPS or len(pts) < 3:
        return list(pts)
    out: List[Point] = [pts[0]]
    for i in range(1, len(pts) - 1):
        p_prev, p, p_next = pts[i - 1], pts[i], pts[i + 1]
        d_in = _unit(p_prev, p)
        d_out = _unit(p, p_next)
        len_in = math.hypot(p[0] - p_prev[0], p[1] - p_prev[1])
        len_out = math.hypot(p_next[0] - p[0], p_next[1] - p[1])
        c = min(ch, len_in / 2.0, len_out / 2.0)
        if c <= EPS:
            out.append(p)
            continue
        out.append((p[0] - d_in[0] * c, p[1] - d_in[1] * c))
        out.append((p[0] + d_out[0] * c, p[1] + d_out[1] * c))
    out.append(pts[-1])
    return out


# --------------------------------------------------------------------------
#  折线 → 实心带状多边形
# --------------------------------------------------------------------------
def _offset_side(pts: List[Point], w: float, side: int) -> List[Point]:
    """把折线按宽度 w 向 side(+1 左 / -1 右)偏移。"""
    half = w / 2.0
    res: List[Point] = []
    n = len(pts)
    for i, P in enumerate(pts):
        if i == 0:
            d = _unit(pts[0], pts[1])
            nx, ny = _left_normal(d)
            res.append((P[0] + nx * side * half, P[1] + ny * side * half))
        elif i == n - 1:
            d = _unit(pts[i - 1], P)
            nx, ny = _left_normal(d)
            res.append((P[0] + nx * side * half, P[1] + ny * side * half))
        else:
            d_in = _unit(pts[i - 1], P)
            d_out = _unit(P, pts[i + 1])
            n_in = _left_normal(d_in)
            n_out = _left_normal(d_out)
            cross = d_in[0] * d_out[1] - d_in[1] * d_out[0]
            is_inner = (cross * side) > 0
            p_in = (P[0] + n_in[0] * side * half, P[1] + n_in[1] * side * half)
            p_out = (P[0] + n_out[0] * side * half, P[1] + n_out[1] * side * half)
            if is_inner:
                inter = _line_intersect(p_in, d_in, p_out, d_out)
                res.append(inter if inter else p_in)
            else:
                res.append(p_in)
                res.append(p_out)
    return res


def ribbon_polygon(pts: List[Point], width: float) -> List[Point]:
    """把折线（中心线）扩展成带宽度的实心多边形。"""
    if len(pts) < 2:
        return []
    left = _offset_side(pts, width, +1)
    right = _offset_side(pts, width, -1)
    return left + list(reversed(right))


def polyline_length(pts: List[Point]) -> float:
    return sum(math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
               for i in range(len(pts) - 1))


# --------------------------------------------------------------------------
#  电参数估算（ST AN2972）
# --------------------------------------------------------------------------
def estimate(p: AntennaParams, trace_length_mm: float) -> Estimate:
    L, W = p.length, p.width
    cW, cS, N = p.conductor_width, p.conductor_space, p.turns
    step = cW + cS

    d_out = (L + W) / 2.0
    d_in = max(d_out - cW - 2 * (N - 1) * step, 1e-3)
    d_avg = (d_out + d_in) / 2.0
    rho = (d_out - d_in) / (d_out + d_in)

    l_h = K1_SQUARE * MU0 * (N ** 2) * (d_avg * 1e-3) / (1.0 + K2_SQUARE * rho)

    # 电阻：直流 + 趋肤效应。高频时电流集中在上下表层（各一层趋肤深度）。
    w_m = cW * 1e-3
    t_m = p.copper_thickness * 1e-6
    delta = math.sqrt(RHO_CU / (math.pi * F_NFC * MU0))
    a_dc = w_m * t_m
    a_ac = w_m * min(t_m, 2.0 * delta)
    r_dc = RHO_CU * (trace_length_mm * 1e-3) / a_dc if a_dc > 0 else float("inf")
    r_ac = RHO_CU * (trace_length_mm * 1e-3) / a_ac if a_ac > 0 else float("inf")
    q = OMEGA * l_h / r_ac if r_ac > 0 else 0.0
    c = 1.0 / (OMEGA ** 2 * l_h) if l_h > 0 else 0.0

    return Estimate(
        inductance_uH=l_h * 1e6,
        resistance_ohm=r_ac,
        resistance_dc_ohm=r_dc,
        skin_depth_um=delta * 1e6,
        q=q,
        tune_cap_pF=c * 1e12,
        trace_length_mm=trace_length_mm,
        outer_avg_mm=d_out,
        inner_avg_mm=d_in,
    )


# --------------------------------------------------------------------------
#  校验
# --------------------------------------------------------------------------
def validate(p: AntennaParams) -> List[str]:
    errs: List[str] = []
    if not p.name.strip():
        errs.append("封装名称不能为空。")
    if p.turns < 1:
        errs.append("匝数必须 ≥ 1。")
    if p.conductor_width <= 0:
        errs.append("线宽必须 > 0。")
    if p.conductor_space <= 0:
        errs.append("线间距必须 > 0。")
    elif p.conductor_space < 0.15:
        errs.append("线间距 < 0.15 mm，多数板厂工艺做不到（建议 ≥ 0.2 mm）。")
    if p.copper_thickness <= 0:
        errs.append("铜厚必须 > 0 µm。")
    if p.pad_drill and p.pad_drill >= p.pad_size:
        errs.append("焊盘钻孔必须小于焊盘直径。")
    if p.chamfer < 0:
        errs.append("倒角不能为负。")
    if p.chamfer > p.conductor_width + p.conductor_space + EPS:
        errs.append("倒角过大（不应超过「线宽 + 线距」）。")
    if p.terminal == "outer" and p.via_drill <= 0:
        errs.append("outer 模式下过孔钻径必须 > 0。")

    step = p.conductor_width + p.conductor_space
    need_l = p.conductor_width + (2 * p.turns - 1) * step
    need_w = p.conductor_width + 2 * (p.turns - 1) * step
    if p.length <= need_l:
        errs.append(f"长度太小：{p.turns} 匝至少需要 {need_l:.2f} mm。")
    if p.width <= need_w:
        errs.append(f"宽度太小：{p.turns} 匝至少需要 {need_w:.2f} mm。")
    return errs


def warnings(p: AntennaParams, est: Estimate) -> List[str]:
    """给出「不合常规但不是错误」的提示。"""
    msgs: List[str] = []
    if not (3 <= p.turns <= 6):
        msgs.append(f"匝数 {p.turns} 不在常见范围 3~6（NFC/HF 天线）。")
    if est.inductance_uH < 0.5 or est.inductance_uH > 5.0:
        msgs.append(f"估算电感 {est.inductance_uH:.2f} µH 超出常见 0.5~5 µH，"
                    "请核对匝数与尺寸。")
    if est.q < 15:
        msgs.append(f"估算 Q ≈ {est.q:.0f} 偏低（<15），读卡距离可能不足；可加粗线宽或加厚铜。")
    if p.conductor_width < 0.3:
        msgs.append("线宽 < 0.3 mm，直流电阻与损耗偏大。")
    if max(p.length, p.width) > 1.5 * min(p.length, p.width):
        msgs.append("长宽比偏大，磁场分布会不对称。")
    return msgs


# --------------------------------------------------------------------------
#  组装
# --------------------------------------------------------------------------
def build(p: AntennaParams) -> AntennaGeometry:
    """生成完整几何。参数非法时抛 ValueError。"""
    errs = validate(p)
    if errs:
        raise ValueError("\n".join(errs))

    L, W = p.length, p.width
    cW, cS, N = p.conductor_width, p.conductor_space, p.turns
    step = cW + cS
    margin = p.silk_margin if p.silk_margin > 0 else 0.0

    centerline = spiral_centerline(p)
    if p.chamfer > 0:
        centerline = chamfer_polyline(centerline, p.chamfer)

    coil_poly = ribbon_polygon(centerline, cW)
    trace_len = polyline_length(centerline)

    terminals: Dict[str, Point] = {"1": centerline[0]}
    ret_poly: List[Point] = []
    ret_center: List[Point] = []
    vias: List[Point] = []

    inner_end = centerline[-1]
    if p.terminal == "inner":
        terminals["2"] = inner_end
    else:
        # 焊盘需避开丝印框，避免 silk_over_copper
        gap = (margin + p.pad_size + 1.0) if p.silk_margin >= 0 else (p.pad_size + 1.5)
        if p.exit_edge == "long":          # 接线在长边（底边）→ 回线向下引出
            pad2_pos = (inner_end[0], W + gap)
            ret_center = [inner_end, (inner_end[0], pad2_pos[1])]
        else:                               # 接线在宽边（左边）→ 回线向左引出
            pad2_pos = (-gap, inner_end[1])
            ret_center = [inner_end, (pad2_pos[0], inner_end[1])]
        trace_len += polyline_length(ret_center)
        ret_poly = ribbon_polygon(ret_center, max(cW, p.via_drill + 0.2))
        vias = [inner_end, pad2_pos]
        terminals["2"] = pad2_pos

    est = estimate(p, trace_len)

    def rect(x0: float, y0: float, x1: float, y1: float) -> List[Tuple[Point, Point]]:
        return [
            ((x0, y0), (x1, y0)),
            ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)),
            ((x0, y1), (x0, y0)),
        ]

    # 铜箔总外接框（含外侧焊盘），用于外框与禁布区
    xs = [pt[0] for pt in coil_poly] + [t[0] for t in terminals.values()]
    ys = [pt[1] for pt in coil_poly] + [t[1] for t in terminals.values()]
    bx0, bx1 = min(xs), max(xs)
    by0, by1 = min(ys), max(ys)

    silk = rect(-margin, -margin, L + margin, W + margin) if p.silk_margin >= 0 else []
    fab = rect(-margin, -margin, L + margin, W + margin)
    cy = max(margin, 0.25) + 0.25
    courtyard = rect(bx0 - cy, by0 - cy, bx1 + cy, by1 + cy)
    ko = p.keepout_margin
    keepout = rect(bx0 - ko, by0 - ko, bx1 + ko, by1 + ko) if ko >= 0 else []

    return AntennaGeometry(
        params=p,
        coil_polygon=coil_poly,
        terminals=terminals,
        return_polygon=ret_poly,
        return_centerline=ret_center,
        vias=vias,
        silk=silk,
        fab=fab,
        courtyard=courtyard,
        keepout=keepout,
        ref_pos=(L / 2.0, -(margin + 1.8)),
        value_pos=(L / 2.0, W + margin + 1.8),
        estimate=est,
        back_corner_k=N * step - cS + margin,
    )
