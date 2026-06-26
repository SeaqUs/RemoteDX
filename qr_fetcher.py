"""
qr_fetcher.py - 微信自动化控制器 v2（修复版）
完整流程：微信窗口 -> More 按钮（窗口 BOTTOM LEFT）打开底部菜单 BizMenuView ->
点击「玩家二维码」 BizMenuButton -> 返回聊天列表最新舞萌DX卡片（ChatBubbleItemView）->
3 步点击序列 -> 微信内置浏览器弹出示例二维码 -> 全屏截图 + OpenCV 解码。

v2 修复点（关键 bug）：
  A) More 按钮之前用 offset=(27,1407) 是相对窗口 TOP，但 More 在窗口 BOTTOM。
     现在改为相对窗口 bottom: (L+30, B-25)，无论窗口多大都对。
  B) 删除 activate_window() 里所有 pyautogui 操作 —— 之前每次激活都
     把鼠标挪去窗口中心，多次外层重试就导致鼠标在屏幕乱飞。
     现在 activate_window 只做 ShowWindow + SetForeground + BringWindowToTop + SetFocus，
     纯 Win32 手段，不碰鼠标。鼠标只在真实 pyautogui.click 前才 moveTo。
  C) ShowWindow 之后循环等窗口 rect 稳定（bottom > 800）再继续；
     如果微信之前最小化在 -32000，ShowWindow 可能要 200ms 才能展开。
  D) More 按钮点击也要先 moveTo 再 click，跟其他点击一致。
  E) Flask 重复触发保护：已有任务在跑时 api_get_qr 返回 409，不再堆后台线程。
"""
import logging, io, base64, time, random, os, sys, traceback, datetime, threading
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

# 把 comtypes 生成文件放到项目目录，避免 Flask 后台线程无权限写 site-packages。
# 同时把项目目录插入到 comtypes.gen.__path__ 最前面，确保优先使用项目内生成的模块，
# 否则若系统 site-packages 里存在旧版生成的模块，会被优先加载并抛出
# "Typelib different than module" ImportError。
_COMTYPES_GEN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "comtypes_gen")
try:
    os.makedirs(_COMTYPES_GEN_DIR, exist_ok=True)
    import comtypes
    import comtypes.client
    import comtypes.gen as _comtypes_gen
    comtypes.gen_dir = _COMTYPES_GEN_DIR
    comtypes.client.gen_dir = _COMTYPES_GEN_DIR
    if _COMTYPES_GEN_DIR not in _comtypes_gen.__path__:
        _comtypes_gen.__path__.insert(0, _COMTYPES_GEN_DIR)
except Exception:
    pass

try:
    import uiautomation as auto
except ImportError:
    auto = None
try:
    import pyautogui
    pyautogui.FAILSAFE = True
except ImportError:
    pyautogui = None
try:
    import ctypes
except ImportError:
    ctypes = None
try:
    from PIL import ImageGrab, Image
except ImportError:
    ImageGrab = None
    Image = None
try:
    import pythoncom
except ImportError:
    pythoncom = None
try:
    import cv2
    CV2_OK = True
except ImportError:
    CV2_OK = False
try:
    import numpy as np
    NP_OK = True
except ImportError:
    NP_OK = False
try:
    import win32gui
except ImportError:
    win32gui = None
try:
    from pyzbar.pyzbar import decode as _pyzbar_decode
    PYZBAR_OK = True
except ImportError:
    _pyzbar_decode = None
    PYZBAR_OK = False

# ====== 日志系统 ======
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FILE = os.path.join(_LOG_DIR, f"qr_{_TS}.log")
_FILE_HANDLER = logging.FileHandler(_LOG_FILE, encoding="utf-8")
_FILE_HANDLER.setFormatter(logging.Formatter(
    "[%(asctime)s] %(levelname)s %(name)s | %(message)s", datefmt="%H:%M:%S"))
# 给 console handler 加一层转码包装，避免 PowerShell gbk 输出特殊符号时抛 UnicodeEncodeError。
# 第一次直接输出；失败时把格式化后的消息按目标编码过滤掉无法显示的字符再写。
class _SafeStreamHandler(logging.StreamHandler):
    """安全控制台日志 Handler：先直接输出；遇到 UnicodeEncodeError 时按目标编码过滤后再写。

    注意：不重写 super().emit()，因为 logging.StreamHandler.emit 内部捕获异常后会自行调用
    handleError() 并打印 "Logging error"，产生刷屏。这里直接操作 self.stream，把 gbk 无法显示
    的字符安全过滤掉。
    """
    def emit(self, record):
        try:
            msg = self.format(record)
            self.stream.write(msg + self.terminator)
            self.flush()
            return
        except UnicodeEncodeError:
            pass
        except Exception:
            self.handleError(record)
            return
        # 安全回退：按当前流能接受的编码做 ignore 过滤（常见为 gbk / utf-8）
        try:
            msg = self.format(record)
        except Exception:
            msg = str(record.getMessage())
        enc = getattr(self.stream, "encoding", None) or sys.stdout.encoding or "gbk"
        try:
            safe = msg.encode(enc, "ignore").decode(enc, "ignore")
        except Exception:
            safe = msg.encode("ascii", "ignore").decode("ascii", "ignore")
        try:
            self.stream.write(safe + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

_CONSOLE_HANDLER = _SafeStreamHandler(sys.stdout)
_CONSOLE_HANDLER.setFormatter(logging.Formatter(
    "[%(asctime)s] %(levelname)s %(name)s | %(message)s", datefmt="%H:%M:%S"))
logging.basicConfig(level=logging.DEBUG, handlers=[_FILE_HANDLER, _CONSOLE_HANDLER])
log = logging.getLogger("qr_fetcher")

_STEP_COUNTER = {"n": 0}
_STEP_LOCK = threading.Lock()


def _next_step() -> int:
    with _STEP_LOCK:
        _STEP_COUNTER["n"] += 1
        return _STEP_COUNTER["n"]


def _save_screen(step_label: str) -> Optional[str]:
    """截一张全屏保存到 logs/。"""
    if ImageGrab is None:
        return None
    step = _next_step()
    safe = step_label.replace(" ", "_").replace("/", "_")
    path = os.path.join(_LOG_DIR, f"L{step:02d}_{safe}_{_TS}.png")
    try:
        img = ImageGrab.grab()
        img.save(path)
        log.info(f"[SCREEN] 截图 step#{step} => {path}  size={img.size}")
        return path
    except Exception as e:
        log.warning(f"[SCREEN] 截图 step#{step} 失败：{e}")
        return None


def _dump_all_top_windows() -> List[Tuple[int, str, str, int, int, int, int]]:
    if win32gui is None:
        return []
    wins = []
    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            width, height = r - l, b - t
            # 过滤掉坐标在 (-32000, -32000) 的最小化占位窗口（wx/QQ/MuMu 托盘图标都会占可见位）
            if width > 0 and height > 0 and l > -10000 and t > -10000:
                wins.append((hwnd, title, cls, l, t, r, b))
    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as e:
        log.warning(f"EnumWindows 异常：{e}")
    return wins


def _log_all_top_windows(prefix: str = "TOP_WINS"):
    wins = _dump_all_top_windows()
    wins.sort(key=lambda w: w[3])
    log.debug(f"=== {prefix} 共 {len(wins)} 个可见顶层窗口 ===")
    for hwnd, title, cls, l, t, r, b in wins:
        width, height = r - l, b - t
        tag = " MIN" if width <= 0 or height <= 0 else ""
        log.debug(f"  hwnd={hwnd:>8} cls={cls:<35} title={title[:35]:<35} rect=({l},{t},{r},{b}) size={width}x{height}{tag}")


def _walk_controls(node, max_depth: int) -> list:
    result = []
    if max_depth <= 0:
        return result
    try:
        children = node.GetChildren()
    except Exception:
        return result
    for c in children:
        result.append(c)
        result.extend(_walk_controls(c, max_depth - 1))
    return result


def _log_wechat_subtree(win, prefix: str = "WECHAT_TREE"):
    def _dump(node, depth, max_depth=6, lines=None):
        if lines is None:
            lines = []
        if depth > max_depth:
            return lines
        indent = "  " * depth
        try:
            name = getattr(node, "Name", "") or ""
            cls = getattr(node, "ClassName", "") or ""
            ct = getattr(node, "ControlType", None)
            try:
                ctn = ct.Name if ct is not None else ""
            except Exception:
                ctn = str(ct) if ct is not None else ""
            try:
                r = node.BoundingRectangle
                rect = f"({r.left},{r.top},{r.right},{r.bottom})"
            except Exception:
                rect = "?"
            # 用纯 ASCII 树形标记，避免 PowerShell gbk 控制台输出特殊符号时报错
            lines.append(f"{indent}+-- Name={name[:40]:<40} ClassName={cls:<40} Type={ctn:<18} Rect={rect}")
            for ch in node.GetChildren():
                _dump(ch, depth + 1, max_depth, lines)
        except Exception as e:
            lines.append(f"{indent}!ERR: {e}")
        return lines
    try:
        lines = _dump(win, 0, max_depth=6)
        log.debug(f"=== {prefix} 共 {len(lines)} 行 (前 120 行) ===")
        for line in lines[:120]:
            log.debug(line)
    except Exception as e:
        log.warning(f"dump UIA 树失败：{e}")


# ====== 常量 ======
WECHAT_WINDOW_NAME = "微信"
QR_BUTTON_NAME = "玩家二维码"
CARD_CLASSNAME = "mmui::ChatBubbleItemView"
CARD_KEYWORD = "舞萌DX"
ERROR_KEYWORDS = ("服务出现故障", "服务异常", "稍后再试", "公众号提供的服务",
                  "二维码已过期", "重新扫码", "expired")


@dataclass
class FetchConfig:
    wait_after_button_click: float = 4.0
    wait_after_card_click: float = 10.0
    wait_before_scan: float = 2.5
    retry_count: int = 2
    retry_interval: float = 2.0
    card_wait_timeout: float = 20.0
    qr_region: Optional[Tuple[int, int, int, int]] = None
    max_outer_retries: int = 5
    outer_retry_jitter: Tuple[float, float] = (5.0, 8.0)
    # More 按钮位置：相对窗口 BOTTOM-LEFT 的偏移（相对 bottom 和 left 的距离，不是 top）
    # 例如 (30, 25) = 窗口左边 +30 像素，窗口底部 -25 像素
    more_button_offset_from_bottom: Tuple[int, int] = (30, 25)


def _co_init():
    tid = threading.current_thread().ident
    log.info(f"[COM] 线程 id={tid} CoInitialize ...")
    ok = False
    if pythoncom is not None:
        try:
            pythoncom.CoInitialize()
            log.info("[COM] pythoncom.CoInitialize OK")
            ok = True
        except Exception as e:
            log.warning(f"[COM] pythoncom.CoInitialize FAIL {e}")
    if not ok and auto is not None and hasattr(auto, "InitializeUIAutomationInCurrentThread"):
        try:
            auto.InitializeUIAutomationInCurrentThread()
            log.info("[COM] InitializeUIAutomationInCurrentThread OK")
            ok = True
        except Exception as e:
            log.warning(f"[COM] InitializeUIAutomationInCurrentThread FAIL {e}")
    if not ok:
        log.error("[COM] 所有 CoInitialize 策略失败！uiautomation 可能不可用")
    return ok


def _get_wechat_window():
    _log_all_top_windows("SEARCH_WIN_PRE")
    if auto is None:
        log.error("uiautomation 未安装")
        return None
    # 新进程首次调用 uiautomation 时，comtypes 可能要现场生成 UIAutomationCore 的包装模块，
    # 第一次 WindowControl 会抛 ImportError；生成完成后重试即可。
    for attempt in range(3):
        log.info(f"[WIN] 按 Name='微信' 查窗口... (attempt {attempt + 1})")
        for name, kwargs in [
            (WECHAT_WINDOW_NAME, {"Name": WECHAT_WINDOW_NAME}),
            ("mmui::MainWindow", {"ClassName": "mmui::MainWindow"}),
        ]:
            try:
                win = auto.WindowControl(searchDepth=1, **kwargs)
                win.GetRuntimeId()
                log.info(f"[WIN] OK 找到 Name={win.Name} ClassName={win.ClassName} Handle={win.NativeWindowHandle}")
                return win
            except ImportError as e:
                log.warning(f"[WIN] 按 {name} 没找到：ImportError（comtypes 生成中）{e}")
            except Exception as e:
                log.info(f"[WIN] 按 {name} 没找到：{type(e).__name__}")
        if attempt < 2:
            log.info("[WIN] comtypes 生成未完成，等待 1.5s 后重试...")
            time.sleep(1.5)
    _log_all_top_windows("SEARCH_WIN_POST")
    return None


def _get_window_rect(win) -> Optional[Tuple[int, int, int, int]]:
    try:
        r = win.BoundingRectangle
        return (r.left, r.top, r.right, r.bottom)
    except Exception as e:
        log.warning(f"[WIN] rect 读取失败：{e}")
        return None


def _wait_window_rect_stable(win, min_bottom=800, max_wait=3.0):
    """
    ShowWindow 之后，最小化窗口展开到正常位置大概需要 100-300ms。
    这里循环读 rect 直到 bottom > min_bottom 或者超时。
    返回 (rect, seconds_waited)。
    """
    t0 = time.time()
    last_rect = None
    for _ in range(60):  # 60 * 0.05s = 3s
        r = _get_window_rect(win)
        if r is not None and r[3] > min_bottom:
            last_rect = r
            break
        time.sleep(0.05)
    dt = time.time() - t0
    if last_rect is None:
        log.warning(f"[ACT] ShowWindow 后 {dt:.2f}s 窗口 rect 始终没稳定（可能还最小化）")
        return _get_window_rect(win), dt
    log.info(f"[ACT] ShowWindow 后 {dt:.2f}s 窗口 rect 稳定：{last_rect}")
    return last_rect, dt


def _activate_window(win) -> Tuple[bool, Optional[Tuple[int, int, int, int]]]:
    """
    纯 Win32 激活，**绝不移动/缩放窗口**，也**不碰鼠标**。
    使用 AttachThreadInput 让当前线程抢占前台队列再 SetForegroundWindow（Win32 标准 Trick）。
    返回 (ok, rect_after)。
    """
    hwnd = win.NativeWindowHandle
    log.info(f"[ACT] activate hwnd={hwnd} ...")

    if ctypes is not None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        try:
            foreground_hwnd = user32.GetForegroundWindow()
            current_tid = kernel32.GetCurrentThreadId()
            foreground_tid = user32.GetWindowThreadProcessId(foreground_hwnd, 0)
            attached = False
            if current_tid != foreground_tid:
                attached = bool(user32.AttachThreadInput(current_tid, foreground_tid, True))
        except Exception as e:
            log.warning(f"[ACT] AttachThreadInput prepare: {e}")
            attached = False
        try:
            user32.ShowWindow(hwnd, 5)  # SW_SHOW only — 绝不 SW_RESTORE / SW_MAXIMIZE
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            log.info("[ACT] ShowWindow(SW_SHOW) + SetForegroundWindow + BringWindowToTop ok")
        except Exception as e:
            log.warning(f"[ACT] ShowWindow/SetForeground fail: {e}")
        finally:
            try:
                if attached:
                    user32.AttachThreadInput(current_tid, foreground_tid, False)
            except Exception:
                pass

    if auto is not None and hasattr(auto, "SW"):
        try:
            win.ShowWindow(auto.SW.Show)
        except Exception as e:
            log.info(f"[ACT] uiautomation.ShowWindow fail: {e}")
    for fn_name, fn in [("SetActive", lambda: win.SetActive()),
                        ("SetFocus", lambda: win.SetFocus())]:
        try:
            fn()
        except Exception as e:
            log.info(f"[ACT] {fn_name} fail: {e}")

    rect, dt = _wait_window_rect_stable(win, min_bottom=400, max_wait=3.0)
    _log_all_top_windows("ACT_AFTER")

    if rect:
        log.info(f"[ACT] ok rect={rect} (took {dt:.2f}s)")
    else:
        log.warning("[ACT] no rect after activate")
    return rect is not None, rect


def _py_click(cx, cy, label="click"):
    """移动鼠标到 (cx,cy) 然后左键单击（一次，干净利落）。"""
    if pyautogui is None:
        log.warning(f"[{label}] pyautogui 不可用")
        return
    try:
        pyautogui.moveTo(cx, cy, duration=0.12)
    except Exception as e:
        log.warning(f"[{label}] moveTo FAIL {e}")
        return
    time.sleep(0.15)
    try:
        pyautogui.click(cx, cy)
        log.info(f"[{label}] OK click @({cx},{cy})")
    except Exception as e:
        log.warning(f"[{label}] click FAIL {e}")


def _three_step_click(cx, cy, label, sleep_between=2.0):
    """3 步序列：pyautogui.click → pyautogui.doubleClick（都在同一坐标 moveTo 一次即可）。"""
    log.info(f"[3STEP:{label}] target ({cx},{cy}) sleep={sleep_between}")
    if pyautogui is None:
        return
    try:
        pyautogui.moveTo(cx, cy, duration=0.12)
    except Exception as e:
        log.warning(f"[3STEP:{label}] moveTo FAIL {e}")
        return
    time.sleep(0.15)

    try:
        pyautogui.click(cx, cy)
        log.info(f"[3STEP:{label}] #1 click OK")
    except Exception as e:
        log.warning(f"[3STEP:{label}] #1 click FAIL {e}")
    time.sleep(sleep_between)

    try:
        pyautogui.doubleClick(cx, cy)
        log.info(f"[3STEP:{label}] #2 doubleClick OK")
    except Exception as e:
        log.warning(f"[3STEP:{label}] #2 doubleClick FAIL {e}")
    time.sleep(0.3)


def _click_control_alt(ctrl, label="ctrl"):
    """兜底：InvokePattern → .Click()。"""
    log.info(f"[ALT_CLICK:{label}]")
    if auto is None:
        return False
    try:
        ctrl.GetInvokePattern().Invoke()
        log.info(f"[ALT_CLICK:{label}] InvokePattern OK")
        return True
    except Exception as e:
        log.info(f"[ALT_CLICK:{label}] Invoke FAIL {e}")
    try:
        ctrl.Click()
        log.info(f"[ALT_CLICK:{label}] .Click() OK")
        return True
    except Exception as e:
        log.info(f"[ALT_CLICK:{label}] .Click FAIL {e}")
    return False


def _ensure_biz_menu_view(win, win_rect, cfg: FetchConfig):
    """
    找到/打开底部 BizMenuView。
    More 按钮位置用 cfg.more_button_offset_from_bottom = (dx_from_left, dy_from_bottom)
    """
    log.info(f"[BIZ] 检查 BizMenuView 是否可见 ...")
    _save_screen("01_before_biz_menu")

    # 1) 先扫 UIA 树
    all_nodes = _walk_controls(win, 25)
    for c in all_nodes:
        try:
            if "BizMenuView" in (getattr(c, "ClassName", "") or ""):
                r = c.BoundingRectangle
                log.info(f"[BIZ] OK BizMenuView 已可见 rect=({r.left},{r.top},{r.right},{r.bottom})")
                _save_screen("02_biz_menu_already_visible")
                return True
        except Exception:
            continue

    # 2) 点左下角 More（用窗口 rect 的 bottom-left + offset 计算绝对坐标）
    if not win_rect:
        log.warning("[BIZ] 不知道窗口 rect，没法算 More 按钮坐标")
        return False
    L, T, R, B = win_rect
    dx, dy = cfg.more_button_offset_from_bottom  # 相对 left/ bottom 的距离
    mx = L + dx
    my = B - dy
    log.info(f"[BIZ] BizMenuView 不可见，点 More 按钮 rect=({L},{T},{R},{B}) offset=({dx},{dy}) => 绝对({mx},{my})")
    _save_screen("02a_before_click_more")
    _py_click(mx, my, label="MoreButton")
    time.sleep(3.0)
    _save_screen("03_after_click_more")

    # 3) 再扫
    all_nodes = _walk_controls(win, 25)
    for c in all_nodes:
        try:
            if "BizMenuView" in (getattr(c, "ClassName", "") or ""):
                r = c.BoundingRectangle
                log.info(f"[BIZ] OK 展开后 BizMenuView 出现 rect=({r.left},{r.top},{r.right},{r.bottom})")
                _save_screen("04_biz_menu_opened")
                return True
        except Exception:
            continue

    log.warning("[BIZ] FAIL 点了 More 但 BizMenuView 仍不存在")
    _log_wechat_subtree(win, prefix="BIZ_FAIL_TREE")
    _save_screen("05_no_biz_menu")
    return False


def _find_qr_button(win, require_biz_menu=True):
    log.info(f"[BTN] 寻找 玩家二维码 (require_biz_menu={require_biz_menu}) ...")
    all_nodes = _walk_controls(win, 40)
    log.info(f"[BTN] UIA 树节点共 {len(all_nodes)}")

    if require_biz_menu:
        for c in all_nodes:
            try:
                if "BizMenuView" not in (getattr(c, "ClassName", "") or ""):
                    continue
                for sub in _walk_controls(c, 15):
                    try:
                        nm = getattr(sub, "Name", "") or ""
                        sc = getattr(sub, "ClassName", "") or ""
                        if nm == QR_BUTTON_NAME:
                            r = sub.BoundingRectangle
                            log.info(f"[BTN] OK BizMenuView 内找到：ClassName={sc} rect=({r.left},{r.top},{r.right},{r.bottom})")
                            return sub
                    except Exception:
                        continue
            except Exception:
                continue

    log.info("[BTN] BizMenuView 下没找到，兜底全树扫 Name=玩家二维码 ...")
    candidates = []
    for c in all_nodes:
        try:
            nm = getattr(c, "Name", "") or ""
            sc = getattr(c, "ClassName", "") or ""
            if nm == QR_BUTTON_NAME:
                r = None
                try:
                    r = c.BoundingRectangle
                except Exception:
                    pass
                log.info(f"[BTN] 兜底扫到 ClassName={sc} rect={r}")
                candidates.append(c)
        except Exception:
            continue
    if candidates:
        def _score(c):
            sc = getattr(c, "ClassName", "") or ""
            return 0 if ("Button" in sc or "XButton" in sc) else 1
        candidates.sort(key=_score)
        log.info(f"[BTN] OK 兜底选中（候选 {len(candidates)} 个）")
        return candidates[0]

    log.warning("[BTN] FAIL 整个 UIA 树都没有 玩家二维码")
    _log_wechat_subtree(win, prefix="BTN_FAIL_TREE")
    _save_screen("06_no_qr_button")
    return None


def _find_latest_qr_card(win, timeout=15.0):
    log.info(f"[CARD] 找最新卡片 (ClassName={CARD_CLASSNAME}, keyword={CARD_KEYWORD}) timeout={timeout}s")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        candidates = []
        for c in _walk_controls(win, 40):
            try:
                cn = getattr(c, "ClassName", "") or ""
                nm = getattr(c, "Name", "") or ""
                if CARD_CLASSNAME in cn and CARD_KEYWORD in str(nm):
                    r = None
                    try:
                        r = c.BoundingRectangle
                    except Exception:
                        pass
                    candidates.append((c, r))
            except Exception:
                continue
        if candidates:
            latest = max(candidates, key=lambda t: (t[1].bottom if t[1] else 0))
            c, r = latest
            log.info(f"[CARD] OK {len(candidates)} 张候选，最新一张：Name={getattr(c,'Name','')[:80]} rect=({r.left},{r.top},{r.right},{r.bottom})")
            for ci, ri in candidates:
                log.info(f"[CARD]   候选：Name={getattr(ci,'Name','')[:50]} rect=({ri.left},{ri.top},{ri.right},{ri.bottom})")
            return c
        left = int(deadline - time.time())
        if attempt % 5 == 0 or left <= 3:
            log.info(f"[CARD] 第 {attempt} 次扫描仍无卡片（剩余 {left}s）")
            if attempt % 15 == 0:
                _log_wechat_subtree(win, prefix=f"CARD_WAIT_{attempt}")
        time.sleep(1)
    log.warning(f"[CARD] FAIL 等了 {timeout}s 没找到任何卡片")
    _log_wechat_subtree(win, prefix="CARD_TIMEOUT")
    _save_screen("07_no_card_after_timeout")
    return None


def _screenshot(region=None, label="shot"):
    if ImageGrab is None and pyautogui is None:
        raise RuntimeError("Pillow 或 pyautogui 至少装一个")
    if ImageGrab is not None:
        img = ImageGrab.grab(bbox=region) if region else ImageGrab.grab()
    else:
        L, T, R, B = region
        img = pyautogui.screenshot(region=(L, T, R - L, B - T))
    log.info(f"[SCREEN] {label} region={region} size={img.size}")
    return img


def _pil_to_cv2(pil_img):
    arr = np.array(pil_img.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def _decode_qr_image(pil_img) -> Tuple[Optional[str], Optional[str]]:
    """用 OpenCV QRCodeDetector + pyzbar 对一张 PIL 图做多种预处理/放大尝试。"""
    if not CV2_OK:
        return None, None
    try:
        bgr = _pil_to_cv2(pil_img)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        thresh = cv2.adaptiveThreshold(clahe, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, 11, 2)
        det = cv2.QRCodeDetector()
        strategies = [(bgr, "bgr"), (gray, "gray"), (clahe, "clahe"), (thresh, "thresh")]
        # 聊天卡片里的二维码通常很小，需要放大识别；全屏截图放大倍数过高会很慢，
        # 这里放到 16x，调用方应优先裁剪卡片区域后再传进来。
        for scale in [2, 3, 4, 6, 8, 10, 12, 16]:
            h, w = gray.shape
            big = cv2.resize(gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
            strategies.append((big, f"gray x{scale}"))
        total = len(strategies)
        for idx, (img, label) in enumerate(strategies, 1):
            try:
                link, pts, straight = det.detectAndDecode(img)
            except Exception as e:
                log.debug(f"[QR] [{idx}/{total}] {label} FAIL {e}")
                continue
            if link:
                log.info(f"[QR] OK [{idx}/{total}] {label} => {link}")
                return link, label
            log.debug(f"[QR] [{idx}/{total}] {label} => (empty)")

        # pyzbar 兜底：对同样的策略再扫一次
        if PYZBAR_OK and _pyzbar_decode is not None:
            for idx, (img, label) in enumerate(strategies, 1):
                try:
                    for d in _pyzbar_decode(img):
                        data = getattr(d, "data", None)
                        if data:
                            link = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else str(data)
                            log.info(f"[QR] OK pyzbar [{idx}/{total}] {label} => {link}")
                            return link, f"pyzbar {label}"
                except Exception as e:
                    log.debug(f"[QR] pyzbar [{idx}/{total}] {label} FAIL {e}")

        log.info(f"[QR] FAIL 全部 {total} 种策略都没识别到二维码")
    except Exception as e:
        log.warning(f"[QR] FAIL cv2 异常：{type(e).__name__}: {e}")
    return None, None


def _decode_qr_from_region(region: Tuple[int, int, int, int], label: str = "region") -> Optional[str]:
    """截图指定区域并尝试识别二维码。"""
    log.info(f"[QR] 从区域 {region} 识别二维码 ({label})")
    try:
        img = _screenshot(region, label=f"qr_region_{label}")
        link, hit = _decode_qr_image(img)
        if link:
            log.info(f"[QR] 区域 {label} 识别成功：{link}")
            return link
    except Exception as e:
        log.warning(f"[QR] 区域 {label} 识别异常：{e}")
    return None


def _try_decode_card_qr(card) -> Optional[str]:
    """找到聊天卡片后，直接裁剪卡片区域识别二维码，不依赖内置浏览器加载。"""
    try:
        r = card.BoundingRectangle
        region = (r.left, r.top, r.right, r.bottom)
    except Exception as e:
        log.warning(f"[QR_CARD] 读卡片 rect 失败：{e}")
        return None
    return _decode_qr_from_region(region, label="card")


def _try_find_qr_link_on_screen(cfg: FetchConfig, attempts=6, interval=2.0):
    """按 cfg.qr_region 裁剪（未指定则全屏），循环截图识别二维码。"""
    region = cfg.qr_region
    log.info(f"[QR] 循环截图+解码 attempts={attempts} interval={interval}s region={region}")
    for i in range(attempts):
        log.info(f"[QR] --- 尝试 {i+1}/{attempts} ---")
        try:
            img = _screenshot(region, label=f"qr_attempt_{i+1}")
            link, hit_label = _decode_qr_image(img)
            if link:
                log.info(f"[QR] OK 第 {i+1} 次识别成功！link={link}")
                _save_screen(f"08_QR_OK_attempt{i+1}")
                return link
        except Exception as e:
            log.warning(f"[QR] 截图/解码异常：{e}")
        time.sleep(interval)
    return None


def _save_png_base64(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _detect_error_card(win) -> Optional[str]:
    if win is None or auto is None:
        return None
    for c in _walk_controls(win, 25):
        try:
            nm = str(getattr(c, "Name", "") or "")
            if not nm:
                continue
            low = nm.lower()
            for kw in ERROR_KEYWORDS:
                if kw.lower() in low:
                    log.warning(f"[ERR] 疑似错误提示：{nm[:200]}")
                    return nm
        except Exception:
            continue
    return None


def _click_qr_button(win, button) -> bool:
    log.info(f"[BTN_CLICK] 点 玩家二维码 ...")
    r = None
    try:
        r = button.BoundingRectangle
        cx = int((r.left + r.right) / 2)
        cy = int((r.top + r.bottom) / 2)
        log.info(f"[BTN_CLICK] rect=({r.left},{r.top},{r.right},{r.bottom}) center=({cx},{cy})")
    except Exception as e:
        log.warning(f"[BTN_CLICK] BoundingRectangle FAIL {e}")
        return False

    _save_screen("09_before_click_qr_button")
    _three_step_click(cx, cy, "QR_BUTTON", sleep_between=0.5)
    _click_control_alt(button, "QR_BUTTON")
    _save_screen("10_after_click_qr_button")
    return True


def _click_card(win, card) -> None:
    log.info("[CARD_CLICK] clicking card ...")
    try:
        r = card.BoundingRectangle
        cx = int((r.left + r.right) / 2)
        cy = int((r.top + r.bottom) / 2)
        rect_h = r.bottom - r.top
        log.info(f"[CARD_CLICK] rect=({r.left},{r.top},{r.right},{r.bottom}) center=({cx},{cy}) height={rect_h}")
    except Exception as e:
        log.warning(f"[CARD_CLICK] BoundingRectangle fail: {e}")
        return
    _save_screen("11_before_click_card")

    try:
        if hasattr(card, "Click"):
            log.info("[CARD_CLICK] card.Click() uia native")
            card.Click()
    except Exception as e:
        log.warning(f"[CARD_CLICK] card.Click() fail: {e}")
    time.sleep(0.6)

    if pyautogui is not None:
        candidates = [
            (cx, cy, "center"),
            (cx, cy - int(rect_h * 0.25), "upper_quarter"),
            (cx, cy + int(rect_h * 0.25), "lower_quarter"),
        ]
        for tx, ty, tname in candidates:
            try:
                pyautogui.moveTo(tx, ty, duration=0.08)
                time.sleep(0.1)
                pyautogui.click(tx, ty)
                log.info(f"[CARD_CLICK] pyautogui.click @({tx},{ty}) {tname} ok")
                time.sleep(0.5)
                break
            except Exception as e:
                log.warning(f"[CARD_CLICK] pyautogui.click @({tx},{ty}) fail: {e}")

    _click_control_alt(card, "CARD")
    _save_screen("12_after_click_card")


def _single_pass(win, button, pass_index, cfg) -> Dict:
    out = {"ok": False, "link": None, "stage": f"outer_pass_{pass_index}_start",
           "card_found": False, "card_clicked": False, "error": None}
    log.info(f"======= _single_pass #{pass_index}/{cfg.max_outer_retries} =======")

    out["stage"] = f"outer_pass_{pass_index}_click_button"
    _click_qr_button(win, button)

    # 点一次按钮后，等 cfg.wait_after_button_click + cfg.card_wait_timeout 秒，期间不重复点按钮
    out["stage"] = f"outer_pass_{pass_index}_wait_card"
    time.sleep(cfg.wait_after_button_click)
    card = _find_latest_qr_card(win, timeout=cfg.card_wait_timeout)

    if card is None:
        out["stage"] = f"outer_pass_{pass_index}_no_card"
        err_txt = _detect_error_card(win)
        if err_txt:
            out["error"] = f"疑似服务故障：{err_txt}"
        else:
            out["error"] = f"点了玩家二维码后 {cfg.card_wait_timeout}s 内没出现二维码卡片"
        return out

    out["card_found"] = True

    # 优先直接裁剪聊天卡片区域识别二维码：卡片里已经包含登录二维码，
    # 这样即使内置浏览器打不开/报服务故障也能拿到链接。
    out["stage"] = f"outer_pass_{pass_index}_decode_card_qr"
    link = _try_decode_card_qr(card)
    if link:
        out["ok"] = True
        out["link"] = link
        out["stage"] = "done"
        out["card_clicked"] = True  # 未真正点击卡片，但 UI 历史里标记为已定位
        _save_screen(f"08_QR_OK_card_attempt{pass_index}")
        log.info(f"[PASS{pass_index}] 卡片区域直接识别成功")
        return out

    # 兜底：点击卡片，用内置浏览器打开后再识别全屏/指定区域二维码
    out["stage"] = f"outer_pass_{pass_index}_click_card"
    _click_card(win, card)
    out["card_clicked"] = True

    log.info(f"[PASS{pass_index}] 睡 {cfg.wait_after_card_click}s 等内置浏览器加载 ...")
    time.sleep(cfg.wait_after_card_click)
    _save_screen("13_after_card_wait")
    _log_all_top_windows("AFTER_CARD_WAIT")

    out["stage"] = f"outer_pass_{pass_index}_ocr"
    link = _try_find_qr_link_on_screen(cfg, attempts=6, interval=2.0)
    if link:
        out["ok"] = True
        out["link"] = link
        out["stage"] = "done"
    else:
        out["error"] = (out.get("error") or "屏幕没识别到二维码") + " - 可能卡在内置浏览器"
        _save_screen("14_ocr_failed")
        _log_all_top_windows("OCR_FAILED")
    return out


# ====== 外部锁：Flask 入口和 fetch_qr_code 共用 ======
_RUNNING_LOCK = threading.Lock()
_RUNNING_OWNER = {"thread": None}


def is_running() -> bool:
    return _RUNNING_OWNER["thread"] is not None and _RUNNING_OWNER["thread"].is_alive()


def try_borrow_lock() -> bool:
    """非阻塞尝试锁；成功后返回 True，用完 release_lock。"""
    return _RUNNING_LOCK.acquire(blocking=False)


def release_lock():
    try:
        _RUNNING_LOCK.release()
    except Exception:
        pass


def fetch_qr_code(cfg=None) -> Dict:
    """主流程。"""
    cfg = cfg or FetchConfig()
    log.info("=" * 80)
    log.info(f"[START] fetch_qr_code cfg={cfg}")
    log.info(f"[START] 日志文件: {_LOG_FILE}")

    # 防重入：若 Flask 已经在跑另一个 fetch_qr_code，这里阻塞等它结束
    if not try_borrow_lock():
        log.warning("[START] FAIL 已经有一个 fetch_qr_code 在跑，新的请求将直接退出并返回 error")
        return {"ok": False, "error": "已有任务在执行，请等待完成后再点一次",
                "stage": "running_already", "log_file": _LOG_FILE, "outer_attempts": 0}

    result = {
        "ok": False, "link": None, "stage": "init",
        "error": None, "raw_png_base64": None,
        "outer_attempts": cfg.max_outer_retries,
        "outer_finished": 0,
        "pass_history": [],
        "last_error_text": None,
        "note": "",
        "log_file": _LOG_FILE,
    }

    try:
        _co_init()

        result["stage"] = "locate_window"
        win = _get_wechat_window()
        if win is None:
            result["error"] = "找不到微信窗口，请确认 PC 微信已登录"
            _save_screen("FAIL_no_wechat_window")
            return result
        _save_screen("00_wechat_window_found")

        result["stage"] = "activate"
        ok, rect = _activate_window(win)
        if not ok:
            result["error"] = "微信窗口激活失败（最小化/权限不足）"
            return result
        win_region = rect
        _log_wechat_subtree(win, prefix="WECHAT_TREE_TOP")

        result["stage"] = "open_biz_menu"
        if not _ensure_biz_menu_view(win, win_region, cfg):
            result["error"] = "无法打开底部 玩家二维码 菜单（More 按钮坐标错误）"
            return result

        button = None
        for i in range(cfg.retry_count + 1):
            result["stage"] = f"find_button_try_{i+1}"
            button = _find_qr_button(win, require_biz_menu=True)
            if button is not None:
                break
            if i < cfg.retry_count:
                time.sleep(cfg.retry_interval)
                # 重新激活 + 重新确保菜单还在（激活会把弹窗关掉）
                _activate_window(win)
                win_region = _get_window_rect(win)
                _ensure_biz_menu_view(win, win_region, cfg)

        if button is None:
            result["error"] = "找不到 玩家二维码 按钮（BizMenuView 里没有 BizMenuButton）"
            return result

        for pass_index in range(1, cfg.max_outer_retries + 1):
            # 每轮都要重新打开 BizMenuView（点按钮会收起它）
            result["stage"] = f"outer_pass_{pass_index}_reopen_menu"
            _activate_window(win)
            win_region = _get_window_rect(win)
            _ensure_biz_menu_view(win, win_region, cfg)
            # 按钮可能已经失效（菜单重建了），重新找一下
            button = _find_qr_button(win, require_biz_menu=True)
            if button is None:
                log.warning(f"[pass {pass_index}] 按钮在菜单重建后丢了，跳过本轮")
                result["pass_history"].append({"n": pass_index, "ok": False,
                    "card_found": False, "card_clicked": False, "stage": "lost_button",
                    "error": "BizMenuView 打开但按钮找不到"})
                result["outer_finished"] = pass_index
                continue

            one = _single_pass(win, button, pass_index, cfg)
            result["pass_history"].append({
                "n": pass_index, "ok": one["ok"], "card_found": one["card_found"],
                "card_clicked": one["card_clicked"], "stage": one["stage"],
                "error": one.get("error"),
            })
            result["outer_finished"] = pass_index
            if one["ok"]:
                result["ok"] = True
                result["link"] = one["link"]
                result["stage"] = "done"
                result["note"] = f"成功（第 {pass_index}/{cfg.max_outer_retries} 次）"
                _save_screen("15_SUCCESS_QR")
                log.info(f"[SUCCESS] 最终 link = {one['link']}")
                return result

            jitter = random.uniform(*cfg.outer_retry_jitter)
            err_txt = one.get("error") or "未知"
            result["last_error_text"] = err_txt
            log.warning(f"[pass {pass_index}/{cfg.max_outer_retries}] 失败：{err_txt}；{jitter:.1f}s 后再试")
            if pass_index < cfg.max_outer_retries:
                result["stage"] = f"outer_pass_{pass_index}_sleep_{int(jitter)}s"
                time.sleep(jitter)

        # 全挂
        result["stage"] = "all_retries_exhausted"
        final = result["last_error_text"] or "未知"
        result["error"] = "已 %d 次仍失败。最后：%s。详细日志：%s" % (
            cfg.max_outer_retries, final, _LOG_FILE)
        result["note"] = result["error"]
        try:
            img = _screenshot(None, "final_fail")
            result["raw_png_base64"] = _save_png_base64(img)
            _save_screen("16_FAIL_FINAL_FULLSCREEN")
        except Exception as e:
            log.warning(f"保存失败截图也出错：{e}")
        log.error(f"[FINAL] FAIL 全部 {cfg.max_outer_retries} 次失败。最终错误：{final}")

    except Exception as e:
        log.exception(f"fetch_qr_code 抛异常：{type(e).__name__}: {e}")
        result["error"] = f"{type(e).__name__}: {e}"
        result["note"] = traceback.format_exc()
    finally:
        release_lock()
    return result


if __name__ == "__main__":
    import json
    r = fetch_qr_code()
    safe = {}
    for k, v in r.items():
        if isinstance(v, str) and len(v) > 200:
            safe[k] = v[:200] + "..."
        else:
            safe[k] = v
    print(json.dumps(safe, ensure_ascii=False, indent=2))
