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
    via_size: float = 0.6          # outer 模式过孔外径（需 ≤ 线宽才不会侵占线距）
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
    centerline: List[Point] = field(default_factory=list)  # 螺旋中心线
    terminals: Dict[str, Point] = field(default_factory=dict)  # "1"/"2" -> 位置
    return_polygon: List[Point] = field(default_factory=list)   # B.Cu 回线
    return_centerline: List[Point] = field(default_factory=list)  # 回线中心线（自定义焊盘用）
    vias: List[Point] = field(default_factory=list) # 过孔位置（outer）
    # 每个焊盘的 (x尺寸, y尺寸)：沿出线边方向被压到 ≤ 线宽，
    # 这样它与相邻那一匝的间距正好还是 conductor_space；垂直方向加长以保证可焊接。
    pad_sizes: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    pad_angles: Dict[str, float] = field(default_factory=dict)   # 焊盘旋转角(度)
    via_sizes: List[Tuple[float, float]] = field(default_factory=list)
    via_inner: Point = (0.0, 0.0)                   # 内端子过孔位置（outer）
    coil_start: Point = (0.0, 0.0)
    coil_end: Point = (0.0, 0.0)
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


def _walk_rect_cw(pts: Optional[List[Point]], start: Point, xL: float, xR: float,
                  yT: float, yB: float, arc_limit: float) -> Tuple[Point, Point]:
    """从 start 沿矩形顺时针绕行 arc_limit 长度，把途经角点与终点追加到 pts。

    pts 传 None 则只做仿真、不记录。返回 (终点, 终点处的行进方向)。
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
            if pts is not None and math.hypot(end[0] - pts[-1][0],
                                              end[1] - pts[-1][1]) > EPS:
                pts.append(end)
            return end, u
        if pts is not None and math.hypot(nxt[0] - pts[-1][0],
                                          nxt[1] - pts[-1][1]) > EPS:
            pts.append(nxt)
        traveled += seg
        P = nxt
    return P, (1.0, 0.0)


def _clamp_step_landing(P: Point, d: Point, xL: float, xR: float, yT: float,
                        yB: float, step: float) -> Point:
    """把换层踏步的落点修正到矩形上。

    落点是将「本层走线末端」垂直向里挪一个 pitch 得到的。如果它离前方那段
    直边的「后方角点」不足一个 pitch，或者干脆跑到矩形外（说明该层对应边
    已经短到装不下），就把它吸附到那个角点：走线在转角处向内跨层，连接线
    自然变成斜线——真实螺旋就是这么走的，而且不会把相邻那一匝挤窄。
    """
    corners = [(xL, yT), (xR, yT), (xR, yB), (xL, yB)]
    coords = [c[0] * d[0] + c[1] * d[1] for c in corners]
    u = P[0] * d[0] + P[1] * d[1]
    lo, hi = min(coords), max(coords)
    if u < lo + step - EPS:
        cand = [c for c, cu in zip(corners, coords) if abs(cu - lo) < EPS]
        return min(cand, key=lambda c: math.hypot(c[0] - P[0], c[1] - P[1]))
    if u > hi + EPS:
        cand = [c for c, cu in zip(corners, coords) if abs(cu - hi) < EPS]
        return min(cand, key=lambda c: math.hypot(c[0] - P[0], c[1] - P[1]))
    return P


def spiral_centerline(p: AntennaParams, with_sharp: bool = False):
    """生成单层矩形螺旋中心线（外端子 → 内端子）。

    起点（= 接线焊盘所在位置）由 p.exit_edge / p.exit_pos 决定。

    原理：沿同心矩形 R(k)（每层向内缩一个 pitch）**顺时针**绕行，
    每层走「周长 − pitch」后垂直向内跨一个 pitch 进入下一层。
    因为换层的接缝始终落在起点的角向位置，所以无论起点在边的哪个
    位置，绕向都一致、不会自交。

    with_sharp=True 时额外返回「换层踏步的顶点索引」——这些角不能倒角，
    否则那条长度只有一个 pitch 的踏步短边会被切成 45° 斜线，斜线之间的
    垂距只有 pitch/√2，线距就不等了。

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
    sharp: set = set()
    cur = start
    for k in range(N):
        xL, xR = off(k), L - off(k)
        yT, yB = off(k), W - off(k)
        perimeter = 2.0 * ((xR - xL) + (yB - yT))
        # 接缝（本层走完到下一层起步之间那道缺口）留一个 pitch：
        # 缺口两端铜箔的间距就正好等于线距。缺口的角点处理见下面换层处。
        gap = step
        arc = max(perimeter - gap, 0.0)
        while gap < perimeter / 2.0:
            end, ed = _walk_rect_cw(None, cur, xL, xR, yT, yB, arc)
            if math.hypot(end[0] - cur[0], end[1] - cur[1]) >= step - EPS:
                break
            gap += 0.05
            arc = max(perimeter - gap, 0.0)
        cur, d = _walk_rect_cw(pts, cur, xL, xR, yT, yB, arc)
        if k < N - 1:
            # 换层：垂直向内跨一个 pitch。
            # 注：曾试过用 45° 斜坡“更平滑”，但相邻两层的斜坡是平行斜线，
            # 垂直间距只有 pitch/√2 ≈ 0.707×pitch，会破坏线距；
            # 数学上只要每层都用 45° 斜坡就无法保持 pitch 间距。
            # 所以这里坚持垂直跨步，保证各匝线距严格等于 conductor_space。
            sharp.add(len(pts) - 1)
            inward = (-d[1], d[0])
            nxt = _clamp_step_landing(
                (cur[0] + inward[0] * step, cur[1] + inward[1] * step), d,
                off(k + 1), L - off(k + 1), off(k + 1), W - off(k + 1), step)
            if math.hypot(nxt[0] - cur[0], nxt[1] - cur[1]) > EPS:
                pts.append(nxt)
            sharp.add(len(pts) - 1)
            cur = nxt
    return (pts, sharp) if with_sharp else pts


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


def chamfer_polyline(pts: List[Point], ch: float, sharp=()) -> List[Point]:
    """把折线的内部转角切成 45° 倒角（去掉重合点，避免零长线段）。

    sharp 里的索引是换层踏步的角，保持直角不切。
    """
    if ch <= EPS or len(pts) < 3:
        return list(pts)
    out: List[Point] = [pts[0]]
    for i in range(1, len(pts) - 1):
        p_prev, p, p_next = pts[i - 1], pts[i], pts[i + 1]
        if i in sharp:
            if math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > EPS:
                out.append(p)
            continue
        d_in = _unit(p_prev, p)
        d_out = _unit(p, p_next)
        len_in = math.hypot(p[0] - p_prev[0], p[1] - p_prev[1])
        len_out = math.hypot(p_next[0] - p[0], p_next[1] - p[1])
        c = min(ch, len_in / 2.0, len_out / 2.0)
        if c <= EPS:
            out.append(p)
            continue
        for q in ((p[0] - d_in[0] * c, p[1] - d_in[1] * c),
                  (p[0] + d_out[0] * c, p[1] + d_out[1] * c)):
            # 相邻转角切点可能重合（短线段两侧都被切完），只保留一个
            if math.hypot(q[0] - out[-1][0], q[1] - out[-1][1]) > EPS:
                out.append(q)
    if math.hypot(pts[-1][0] - out[-1][0], pts[-1][1] - out[-1][1]) > EPS:
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
#  净距计算（保证线距处处相等的依据）
# --------------------------------------------------------------------------
def _pt_seg_dist(p: Point, a: Point, b: Point) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-18:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _seg_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    def cr(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2, d3, d4 = cr(c, d, a), cr(c, d, b), cr(a, b, c), cr(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _seg_seg_dist(a: Point, b: Point, c: Point, d: Point) -> float:
    if _seg_intersect(a, b, c, d):
        return 0.0
    return min(_pt_seg_dist(a, c, d), _pt_seg_dist(b, c, d),
               _pt_seg_dist(c, a, b), _pt_seg_dist(d, a, b))


def _stadium_core(cx: float, cy: float, sx: float, sy: float,
                  rot_deg: float) -> Tuple[Point, Point, float]:
    """KiCad 的 OVAL 焊盘是「跑道形」= 一段线段按半径 r 膨胀。

    返回 (线段起点, 线段终点, r)；圆焊盘退化为一个点。
    """
    r = min(sx, sy) / 2.0
    half = (max(sx, sy) - min(sx, sy)) / 2.0
    ax, ay = (1.0, 0.0) if sx >= sy else (0.0, 1.0)
    ca, sa = math.cos(math.radians(rot_deg)), math.sin(math.radians(rot_deg))
    dx, dy = ax * ca - ay * sa, ax * sa + ay * ca
    return ((cx - dx * half, cy - dy * half),
            (cx + dx * half, cy + dy * half), r)


def shape_clearance(segs, cx: float, cy: float, sx: float, sy: float,
                    rot_deg: float, cW: float, skip=()) -> float:
    """焊盘/过孔（跑道形）相对一组铜箔带（中心线膨胀 cW/2）的最小净距。"""
    p, q, r = _stadium_core(cx, cy, sx, sy, rot_deg)
    best = float("inf")
    for i, (a, b) in enumerate(segs):
        if i in skip:
            continue
        best = min(best, _seg_seg_dist(p, q, a, b))
    return best - r - cW / 2.0


def polyline_clearance(pts: List[Point], cW: float, skip_adjacent: int = 3) -> float:
    """折线自身的铜箔净距（跳过相邻段与相接处）。"""
    segs = list(zip(pts, pts[1:]))
    best = float("inf")
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if j - i <= skip_adjacent:
                continue
            best = min(best, _seg_seg_dist(segs[i][0], segs[i][1],
                                           segs[j][0], segs[j][1]))
    return best - cW


def terminal_skip(pts: List[Point], at_end: bool, span: float) -> Tuple[int, ...]:
    """端子附近「本来就与它连在一起」的段索引：沿路径累加长度不超过 span。

    端子焊盘会与走线末端自然融在一起（同一个导体），所以这一段不能
    当成「侵占线距」来算。注意 span 很小（焊盘长度量级），不会遮住远端。
    """
    segs = list(zip(pts, pts[1:]))
    idx: List[int] = []
    acc = 0.0
    order = range(len(segs) - 1, -1, -1) if at_end else range(len(segs))
    for i in order:
        idx.append(i)
        acc += math.hypot(segs[i][1][0] - segs[i][0][0],
                          segs[i][1][1] - segs[i][0][1])
        if acc >= span:
            break
    return tuple(idx)


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
    # 为保证与相邻那一匝的线距，焊盘沿走线方向只能到 min(焊盘尺寸, 线宽)
    if p.pad_drill and min(p.pad_size, p.conductor_width) < p.pad_drill + 0.2 - EPS:
        errs.append(
            f"焊盘钻孔 Ø{p.pad_drill:g} 放不下：焊盘沿走线方向最多"
            f" {min(p.pad_size, p.conductor_width):g} mm"
            "（= min(焊盘尺寸, 线宽)，这样才不会侵占相邻匝的间距），"
            "环宽需 ≥ 0.1 mm。请减小钻孔或加大线宽。")
    if p.chamfer < 0:
        errs.append("倒角不能为负。")
    if p.chamfer > p.conductor_width + p.conductor_space + EPS:
        errs.append("倒角过大（不应超过「线宽 + 线距」）。")
    if p.terminal == "outer" and p.via_drill <= 0:
        errs.append("outer 模式下过孔钻径必须 > 0。")
    if p.pad_size <= 0:
        errs.append("焊盘尺寸必须 > 0。")
    if p.terminal == "outer":
        if p.via_size <= p.via_drill:
            errs.append("过孔外径必须大于钻径。")
        elif p.via_size - p.via_drill < 0.2:
            errs.append("过孔环宽 < 0.1 mm，多数板厂工艺做不到。")
        w_narrow = min(p.pad_size, p.conductor_width)
        if w_narrow < p.via_drill + 0.2 - EPS:
            errs.append(
                f"外侧焊盘沿走线方向只有 {w_narrow:g} mm（= min(焊盘尺寸, 线宽)），"
                f"放不下 Ø{p.via_drill:g} 的过孔（环宽需 ≥ 0.1 mm）。\n"
                "请减小过孔钻径或加大线宽。")
        if p.via_size > p.conductor_width + EPS:
            errs.append(
                f"过孔外径 {p.via_size:g} mm 大于线宽 {p.conductor_width:g} mm，"
                f"放不进 {p.conductor_width + p.conductor_space:g} mm 的节距。"
                "请减小过孔外径、加大线宽或加大线间距。")

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
    if p.pad_size > p.conductor_width + EPS:
        msgs.append(f"焊盘沿出线边方向已自动收窄到线宽 {p.conductor_width:g} mm"
                    f"（实际 {p.conductor_width:g}×{p.pad_size:g} mm 椭圆），"
                    "以保证与相邻一匝的间距仍等于线距。")
    if p.terminal == "outer" and p.via_size > p.conductor_width + EPS:
        msgs.append(f"过孔外径 {p.via_size:g} mm 大于线宽 {p.conductor_width:g} mm，"
                    "与相邻一匝的间距会被压缩；建议过孔外径 ≤ 线宽。")
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

    centerline, sharp = spiral_centerline(p, with_sharp=True)
    if p.chamfer > 0:
        centerline = chamfer_polyline(centerline, p.chamfer, sharp)

    coil_poly = ribbon_polygon(centerline, cW)
    trace_len = polyline_length(centerline)
    coil_start, coil_end = centerline[0], centerline[-1]
    center_pt = (L / 2.0, W / 2.0)

    # 焊盘尺寸：沿「走线方向」窄（≤ 线宽），沿「法线方向」长。
    # 这样焊盘不会侵占与相邻那一匝的间距（保持 = conductor_space），
    # 同时向自由侧伸长以保证可焊接面积。
    w_long = max(p.pad_size, cW)
    w_narrow = min(p.pad_size, cW)

    # 起点（外端子）朝外方向：行进方向的“外向法线”
    d0 = _unit(coil_start, centerline[1])
    out1 = (d0[1], -d0[0])
    # 内端子朝线圈中心方向（在两条法线里选指向中心的那条）
    dN = _unit(centerline[-2], coil_end)
    fans = ((-dN[1], dN[0]), (dN[1], -dN[0]))
    to_center = (center_pt[0] - coil_end[0], center_pt[1] - coil_end[1])
    in2 = max(fans, key=lambda v: v[0] * to_center[0] + v[1] * to_center[1])

    # 焊盘局部 X 轴沿走线方向，于是尺寸 = (窄, 长)
    ang1 = math.degrees(math.atan2(d0[1], d0[0]))
    ang2 = math.degrees(math.atan2(dN[1], dN[0]))

    segs = list(zip(centerline, centerline[1:]))
    n_seg = len(segs)

    def place(near_pt: Point, nrm: Point, ang: float,
              connected: Tuple[int, ...]) -> Tuple[float, Point]:
        """把椭圆焊盘沿法线伸出：近边落在端子铜箔远边，长边尽量长，
        若伸出去会侵占线距则自动缩短（最短退化为圆）。
        """
        length = w_long
        while True:
            off_try = length / 2.0 - cW / 2.0
            pos = (near_pt[0] + nrm[0] * off_try, near_pt[1] + nrm[1] * off_try)
            gap = shape_clearance(segs, pos[0], pos[1], w_narrow, length,
                                  ang, cW, connected)
            if gap >= cS - EPS or length <= w_narrow + EPS:
                return length, pos
            length = max(length - 0.05, w_narrow)

    len1, pad1_pos = place(coil_start, out1, ang1,
                           terminal_skip(centerline, False, w_long + cW))
    # 过孔外径 ≤ 线宽时中心正好落在走线端点上
    off_via = max(p.via_size / 2.0 - cW / 2.0, 0.0)
    via_inner = (coil_end[0] + in2[0] * off_via, coil_end[1] + in2[1] * off_via)

    terminals: Dict[str, Point] = {"1": pad1_pos}
    pad_sizes: Dict[str, Tuple[float, float]] = {"1": (w_narrow, len1)}
    pad_angles: Dict[str, float] = {"1": ang1}
    ret_poly: List[Point] = []
    ret_center: List[Point] = []
    vias: List[Point] = []
    via_sizes: List[Tuple[float, float]] = []

    if p.terminal == "inner":
        len2, pad2_pos = place(coil_end, in2, ang2,
                               terminal_skip(centerline, True, w_long + cW))
        terminals["2"] = pad2_pos
        pad_sizes["2"] = (w_narrow, len2)
        pad_angles["2"] = ang2
    else:
        # 外侧焊盘与 pad1 落在同一条「出线位置」上（沿出线边对齐），
        # 回线从内侧过孔经一个拐点绕到它。
        gap = (margin + p.pad_size + 1.0) if p.silk_margin >= 0 else (p.pad_size + 1.5)
        if p.exit_edge == "long":          # 接线在长边（底边）→ 回线向下引出
            pad2_pos = (pad1_pos[0], W + gap)
            elbow = (via_inner[0], pad2_pos[1])
        else:                               # 接线在宽边（左边）→ 回线向左引出
            pad2_pos = (-gap, pad1_pos[1])
            elbow = (pad2_pos[0], via_inner[1])
        ret_center = [via_inner]
        for pt in (elbow, pad2_pos):
            if math.hypot(pt[0] - ret_center[-1][0], pt[1] - ret_center[-1][1]) > EPS:
                ret_center.append(pt)
        trace_len += polyline_length(ret_center)
        ret_poly = ribbon_polygon(ret_center, max(cW, p.via_size))
        # 过孔放在线圈内端子处（外侧焊盘本身用通孔，兼作回线另一端的过孔）
        vias = [via_inner]
        via_sizes = [(p.via_size, p.via_size)]
        terminals["2"] = pad2_pos
        # 外侧焊盘与 pad1 同形同向（避免出现一大块方焊盘），只改成通孔
        pad_sizes["2"] = pad_sizes["1"]
        pad_angles["2"] = pad_angles["1"]

    # ---- 几何自检：所有铜箔之间必须保持 ≥ 线间距 ----
    audit = [polyline_clearance(centerline, cW)]
    tail_skip = terminal_skip(centerline, True, w_long + cW)
    head_skip = terminal_skip(centerline, False, w_long + cW)
    for name, pos in terminals.items():
        if p.terminal == "outer" and name == "2":
            skip: Tuple[int, ...] = ()
        else:
            skip = head_skip if name == "1" else tail_skip
        sx, sy = pad_sizes[name]
        audit.append(shape_clearance(segs, pos[0], pos[1], sx, sy,
                                     pad_angles[name], cW, skip))
    for (vx, vy) in vias:
        audit.append(shape_clearance(segs, vx, vy, p.via_size, p.via_size,
                                     0.0, cW, tail_skip))
    worst = min(audit)
    if worst < cS - 1e-6:
        raise ValueError(
            f"几何自检未通过：最小净距 {worst:.3f} mm，小于设定的线间距 {cS:g} mm。\n"
            "原因是「换层接缝」在这组尺寸里放不下：每绕一层，接缝会沿走线方向\n"
            f"后移 2×({cW:g}+{cS:g})={2 * (cW + cS):g} mm，"
            f"{N} 匝共需约 {(2 * N - 1) * (cW + cS):g} mm 的直线段，\n"
            "而出线位置到该边端点的距离不够。\n"
            "可尝试：加大线间距、减少匝数、把出线位置改到靠边（上端/下端），"
            "或加长天线尺寸。")

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

    silk = (rect(bx0 - margin, by0 - margin, bx1 + margin, by1 + margin)
            if p.silk_margin >= 0 else [])
    fab = rect(bx0 - margin, by0 - margin, bx1 + margin, by1 + margin)
    cy = max(margin, 0.25) + 0.25
    courtyard = rect(bx0 - cy, by0 - cy, bx1 + cy, by1 + cy)
    ko = p.keepout_margin
    keepout = rect(bx0 - ko, by0 - ko, bx1 + ko, by1 + ko) if ko >= 0 else []

    cx_mid = (bx0 + bx1) / 2.0
    return AntennaGeometry(
        params=p,
        coil_polygon=coil_poly,
        centerline=centerline,
        terminals=terminals,
        return_polygon=ret_poly,
        return_centerline=ret_center,
        vias=vias,
        silk=silk,
        fab=fab,
        courtyard=courtyard,
        keepout=keepout,
        pad_sizes=pad_sizes,
        pad_angles=pad_angles,
        via_sizes=via_sizes,
        via_inner=via_inner,
        coil_start=coil_start,
        coil_end=coil_end,
        ref_pos=(cx_mid, by0 - margin - 1.8),
        value_pos=(cx_mid, by1 + margin + 1.8),
        estimate=est,
        back_corner_k=N * step - cS + margin,
    )
