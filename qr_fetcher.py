"""
qr_fetcher.py - 微信自动化控制器
完整流程：微信窗口 -> More 按钮打开底部菜单 BizMenuView -> 点击「玩家二维码」 BizMenuButton -> 
返回聊天列表里的最新舞萌DX二维码卡片（ChatBubbleItemView） -> 3步点击序列 -> 微信内置浏览器弹出示例二维码 -> 全屏截图 + OpenCV 解码。

关键：
1) uiautomation 依赖 COM (IUIAutomation)。Flask 后台线程里必须显式 CoInitialize()，
   否则 [WinError -2147221008] 尚未调用 CoInitialize。
2) 「玩家二维码」不在聊天列表，而是底部 BizMenuView 里的 BizMenuButton。
   必须先点聊天窗口左下角 More 按钮打开这个菜单，再从里面点「玩家二维码」。
3) 卡片点击必须走 3 步序列：pyautogui.click -> pyautogui.doubleClick -> uiautomation.Click()。
   只走其中任意一步，微信都不会触发内置浏览器弹窗。
4) 二维码弹窗在微信内置浏览器浮窗，不在主窗口 BoundingRectangle 里，必须整屏截图。

DEBUG 增强：
- 每条日志同时输出到控制台 + 时间戳命名的 ./logs/qr_YYYYmmdd_HHMMSS.log
- 关键步骤会保存一张屏幕截图到 ./logs/ 目录（L01_...png, L02_...png...）
- 扫描 UIA 树时把命中的所有控件完整 dump 出来
- 每一步打印精确坐标、窗口大小、当前前台窗口句柄
"""
import logging, io, base64, time, random, os, sys, traceback, datetime, threading
import numpy as np
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict

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
    import win32gui  # pywin32
except ImportError:
    win32gui = None

# ====== 日志系统：同时写控制台 + 时间戳文件 ======
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
_LOG_FILE = os.path.join(_LOG_DIR, f"qr_{_TS}.log")
_FILE_HANDLER = logging.FileHandler(_LOG_FILE, encoding="utf-8")
_FILE_HANDLER.setFormatter(logging.Formatter(
    "[%(asctime)s] %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
))
_CONSOLE_HANDLER = logging.StreamHandler(sys.stdout)
_CONSOLE_HANDLER.setFormatter(logging.Formatter(
    "[%(asctime)s] %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
))
logging.basicConfig(level=logging.DEBUG, handlers=[_FILE_HANDLER, _CONSOLE_HANDLER])
log = logging.getLogger("qr_fetcher")

# 全局步骤计数器 + 截图目录（让调试者知道每一步对应哪张图）
_STEP_COUNTER = {"n": 0}
_STEP_LOCK = threading.Lock()


def _next_step() -> int:
    """原子自增 step 计数器，返回新值。"""
    with _STEP_LOCK:
        _STEP_COUNTER["n"] += 1
        return _STEP_COUNTER["n"]


def _save_screen(step_label: str) -> Optional[str]:
    """截一张全屏，保存到 logs/ 目录，返回绝对路径（失败返回 None）。"""
    if ImageGrab is None:
        return None
    step = _next_step()
    safe_label = step_label.replace(" ", "_").replace("/", "_")
    out_path = os.path.join(_LOG_DIR, f"L{step:02d}_{safe_label}_{_TS}.png")
    try:
        img = ImageGrab.grab()
        img.save(out_path)
        log.info(f"📸 截图 step#{step} 保存 => {out_path}  大小={os.path.getsize(out_path)}  尺寸={img.size}")
        return out_path
    except Exception as e:
        log.warning(f"📸 截图 step#{step} 失败：{e}")
        return None


def _dump_all_top_windows() -> List[Tuple[int, str, str, int, int, int, int]]:
    """列出当前系统所有可见顶层窗口 (hwnd, title, class, L,T,R,B)。"""
    if win32gui is None:
        return []
    wins = []
    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            if title or cls:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                wins.append((hwnd, title, cls, l, t, r, b))
    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as e:
        log.warning(f"EnumWindows 异常：{e}")
    return wins


def _log_all_top_windows(prefix: str = "TOP_WINS"):
    """把当前可见顶层窗口全部打印出来，便于发现微信是否被遮挡/最小化。"""
    wins = _dump_all_top_windows()
    wins.sort(key=lambda w: w[3])  # 按 left 排序
    log.debug(f"=== {prefix} 共 {len(wins)} 个可见顶层窗口 ===")
    for hwnd, title, cls, l, t, r, b in wins:
        width = r - l
        height = b - t
        visible_tag = "  MINIMIZED" if width <= 0 or height <= 0 else ""
        log.debug(f"  hwnd={hwnd:>8} cls={cls:<40} title={title[:40]:<40} rect=({l},{t},{r},{b}) size={width}x{height}{visible_tag}")


def _dump_uia_tree(node, depth: int, max_depth: int = 6, lines: List[str] = None):
    """
    打印 UIA 树的一部分（最多 max_depth 层）。
    对每个节点，把 Name / ClassName / ControlType / BoundingRectangle 都打出来。
    """
    if lines is None:
        lines = []
    if depth > max_depth:
        return lines
    indent = "  " * depth
    try:
        name = getattr(node, "Name", None) or ""
        cls = getattr(node, "ClassName", None) or ""
        ctrl_type = getattr(node, "ControlType", None)
        try:
            ctrl_type_name = ctrl_type.Name if ctrl_type is not None else ""
        except Exception:
            ctrl_type_name = str(ctrl_type) if ctrl_type is not None else ""
        rect = None
        try:
            r = node.BoundingRectangle
            rect = f"({r.left},{r.top},{r.right},{r.bottom})"
        except Exception:
            rect = "?"
        txt = f"{indent}⌜Name={name[:40]:<40} ClassName={cls:<45} Type={ctrl_type_name:<20} Rect={rect}"
        lines.append(txt)
        children = node.GetChildren()
        for c in children:
            _dump_uia_tree(c, depth + 1, max_depth, lines)
    except Exception as e:
        lines.append(f"{indent}!ERROR: {e}")
    return lines


def _log_wechat_subtree(win, prefix: str = "WECHAT_TREE"):
    """dump 微信窗口下前 6 层 UIA 树，只在 debug 级别打。"""
    try:
        lines = _dump_uia_tree(win, 0, max_depth=6)
        log.debug(f"=== {prefix} 共 {len(lines)} 行 (前 200 行) ===")
        for line in lines[:200]:
            log.debug(line)
        if len(lines) > 200:
            log.debug(f"... (剩余 {len(lines) - 200} 行略，请到 {_LOG_FILE} 查看完整)")
    except Exception as e:
        log.warning(f"dump UIA 树失败：{e}")


def _log_all_class_names(win, keyword: str = ""):
    """扫 UIA 树，把所有 ClassName 含 keyword 的节点都列出来（含坐标）。"""
    all_nodes = _walk_controls(win, 40)
    seen = {}
    for c in all_nodes:
        try:
            cls = getattr(c, "ClassName", "") or ""
            if keyword and keyword not in cls:
                continue
            nm = getattr(c, "Name", "") or ""
            try:
                r = c.BoundingRectangle
                rect = f"({r.left},{r.top},{r.right},{r.bottom})"
            except Exception:
                rect = "?"
            key = cls
            if key not in seen:
                seen[key] = []
            if len(seen[key]) < 30:
                seen[key].append((nm[:60], rect))
        except Exception:
            continue
    log.info(f"=== ClassName 包含 '{keyword}' 的节点（最多每种 30 个） ===")
    for cls, items in sorted(seen.items()):
        log.info(f"  [{cls}]  count={len(items)}/...")
        for nm, rect in items:
            log.info(f"    Name={nm:<60} Rect={rect}")


# ====== 以下基本功能函数都加上详细日志 ======

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
    more_button_offset: Tuple[int, int] = (27, 1407)


def _co_init():
    """在当前 OS 线程里初始化 COM（Flask 后台线程必须调用，否则 uiautomation 报 CoInitialize 未调用）。"""
    tid = threading.current_thread().ident
    log.info(f"[COM] 当前线程 id={tid} 开始 CoInitialize...")
    ok = False
    if pythoncom is not None:
        try:
            pythoncom.CoInitialize()
            log.info("[COM] pythoncom.CoInitialize ✅")
            ok = True
        except Exception as e:
            log.warning(f"[COM] pythoncom.CoInitialize ❌ {type(e).__name__}: {e}")
    if not ok and auto is not None and hasattr(auto, "InitializeUIAutomationInCurrentThread"):
        try:
            auto.InitializeUIAutomationInCurrentThread()
            log.info("[COM] InitializeUIAutomationInCurrentThread ✅")
            ok = True
        except Exception as e:
            log.warning(f"[COM] InitializeUIAutomationInCurrentThread ❌ {type(e).__name__}: {e}")
    if not ok:
        log.error("[COM] 所有 CoInitialize 策略都失败！uiautomation 大概率没法用。")
    return ok


def _get_wechat_window():
    """用 uiautomation 查找主微信窗口。"""
    _log_all_top_windows("SEARCH_WIN_PRE")
    if auto is None:
        log.error("uiautomation 未安装")
        return None
    log.info("[WIN] 按 Name='微信' 查窗口...")
    try:
        win = auto.WindowControl(searchDepth=1, Name=WECHAT_WINDOW_NAME)
        win.GetRuntimeId()
        log.info(f"[WIN] ✅ 按 Name 找到: Name={win.Name} ClassName={win.ClassName} Handle={win.NativeWindowHandle}")
        return win
    except Exception as e:
        log.info(f"[WIN] 按 Name 没找到：{type(e).__name__}: {e}")
    log.info("[WIN] 按 ClassName='mmui::MainWindow' 查窗口...")
    try:
        win = auto.WindowControl(searchDepth=1, ClassName="mmui::MainWindow")
        win.GetRuntimeId()
        log.info(f"[WIN] ✅ 按 ClassName 找到: Name={win.Name} ClassName={win.ClassName} Handle={win.NativeWindowHandle}")
        return win
    except Exception as e:
        log.info(f"[WIN] 按 ClassName 没找到：{type(e).__name__}: {e}")
    _log_all_top_windows("SEARCH_WIN_POST")
    return None


def _get_window_region(win) -> Optional[Tuple[int, int, int, int]]:
    if win is None:
        return None
    try:
        rect = win.BoundingRectangle
        r = (rect.left, rect.top, rect.right, rect.bottom)
        log.info(f"[WIN] 微信窗口 rect={r}  size=({rect.right - rect.left}x{rect.bottom - rect.top})")
        return r
    except Exception as e:
        log.warning(f"[WIN] 取窗口坐标失败：{e}")
        return None


def _activate_window(win) -> bool:
    """把微信窗口真正打到前台。"""
    log.info(f"[ACT] 开始激活窗口 Handle={win.NativeWindowHandle} ...")
    ok = False
    try:
        # 1) uiautomation ShowWindow
        if auto is not None and hasattr(auto, "SW"):
            for sw_name, sw_val in [("Restore", auto.SW.Restore), ("Show", auto.SW.Show)]:
                try:
                    win.ShowWindow(sw_val)
                    log.info(f"[ACT]   uiautomation.ShowWindow({sw_name}={sw_val}) ✅")
                except Exception as e:
                    log.warning(f"[ACT]   ShowWindow({sw_name}) ❌ {e}")
        # 2) Win32 ShowWindow / SetForeground / BringWindowToTop
        if ctypes is not None:
            user32 = ctypes.windll.user32
            hwnd = win.NativeWindowHandle
            log.info(f"[ACT]   Win32 hwnd={hwnd}")
            if hwnd:
                try:
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE=9
                    log.info("[ACT]   user32.ShowWindow(hwnd, 9) ✅")
                except Exception as e:
                    log.warning(f"[ACT]   user32.ShowWindow ❌ {e}")
                try:
                    user32.SetForegroundWindow(hwnd)
                    log.info("[ACT]   user32.SetForegroundWindow ✅")
                except Exception as e:
                    log.warning(f"[ACT]   user32.SetForegroundWindow ❌ {e}")
                try:
                    user32.BringWindowToTop(hwnd)
                    log.info("[ACT]   user32.BringWindowToTop ✅")
                except Exception as e:
                    log.warning(f"[ACT]   user32.BringWindowToTop ❌ {e}")
        # 3) uiautomation SetActive / SetFocus
        for fn_name, fn in [("SetActive", lambda: win.SetActive()),
                            ("SetFocus", lambda: win.SetFocus())]:
            try:
                fn()
                log.info(f"[ACT]   win.{fn_name}() ✅")
            except Exception as e:
                log.warning(f"[ACT]   win.{fn_name}() ❌ {e}")
        ok = True
        time.sleep(0.6)
        # 4) 看一下激活后的顶层窗口列表，确认微信是否真在最前
        _log_all_top_windows("ACT_AFTER")
        # 5) pyautogui 也挪一下鼠标到微信中心（有的窗口 SetForeground 了但不响应）
        if pyautogui is not None:
            try:
                r = win.BoundingRectangle
                cx = (r.left + r.right) // 2
                cy = (r.top + r.bottom) // 2
                pyautogui.moveTo(cx, cy, duration=0.1)
                log.info(f"[ACT]   pyautogui.moveTo 微信中心 ({cx},{cy}) ✅")
            except Exception as e:
                log.warning(f"[ACT]   pyautogui.moveTo ❌ {e}")
    except Exception as e:
        log.warning(f"[ACT] 激活窗口整体异常：{type(e).__name__}: {e}")
        traceback.print_exc()
    return ok


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


def _click_control(ctrl, label: str = "ctrl") -> bool:
    """兜底点击（逐策略打日志）。"""
    log.info(f"[CLICK_{label}] begin")
    if auto is not None:
        try:
            ctrl.GetInvokePattern().Invoke()
            log.info(f"[CLICK_{label}]   InvokePattern ✅")
            return True
        except Exception as e:
            log.info(f"[CLICK_{label}]   InvokePattern ❌ {type(e).__name__}: {str(e)[:80]}")
        try:
            ctrl.Click()
            log.info(f"[CLICK_{label}]   .Click() ✅")
            return True
        except Exception as e:
            log.info(f"[CLICK_{label}]   .Click() ❌ {type(e).__name__}: {str(e)[:80]}")
    if pyautogui is not None:
        try:
            rect = ctrl.BoundingRectangle
            cx = int((rect.left + rect.right) / 2)
            cy = int((rect.top + rect.bottom) / 2)
            log.info(f"[CLICK_{label}]   pyautogui.click ({cx},{cy}) rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")
            pyautogui.click(cx, cy)
            return True
        except Exception as e:
            log.warning(f"[CLICK_{label}]   pyautogui.click ❌ {e}")
    return False


def _ensure_biz_menu_view(win, win_region: Tuple[int, int, int, int], cfg: FetchConfig):
    """找到/打开底部 BizMenuView。"""
    log.info(f"[BIZ] 检查 BizMenuView 是否可见 ...")
    _log_all_class_names(win, keyword="BizMenu")
    _save_screen("01_before_biz_menu")

    # 1) 先直接扫 UIA 树
    biz_found = False
    all_nodes = _walk_controls(win, 25)
    for c in all_nodes:
        try:
            if "BizMenuView" in (getattr(c, "ClassName", "") or ""):
                nm = getattr(c, "Name", "") or ""
                r = c.BoundingRectangle
                log.info(f"[BIZ] ✅ BizMenuView 已可见：Name='{nm}' ClassName={c.ClassName} rect=({r.left},{r.top},{r.right},{r.bottom})")
                biz_found = True
                break
        except Exception:
            continue
    if biz_found:
        _save_screen("02_biz_menu_already_visible")
        return True

    # 2) 点左下角 More
    left, top, _, bottom = win_region
    mx = left + cfg.more_button_offset[0]
    my = top + cfg.more_button_offset[1]
    log.info(f"[BIZ] BizMenuView 不可见，点 More 按钮 @({mx},{my}) offset={cfg.more_button_offset}")
    _activate_window(win)
    time.sleep(0.3)
    if pyautogui is not None:
        try:
            pyautogui.click(mx, my)
            log.info("[BIZ]   pyautogui.click(More) ✅")
        except Exception as e:
            log.warning(f"[BIZ]   pyautogui.click(More) ❌ {e}")
    time.sleep(3.0)
    _save_screen("03_after_click_more")

    # 3) 再扫一次
    all_nodes = _walk_controls(win, 25)
    for c in all_nodes:
        try:
            if "BizMenuView" in (getattr(c, "ClassName", "") or ""):
                nm = getattr(c, "Name", "") or ""
                r = c.BoundingRectangle
                log.info(f"[BIZ] ✅ 展开后 BizMenuView 出现：Name='{nm}' ClassName={c.ClassName} rect=({r.left},{r.top},{r.right},{r.bottom})")
                _save_screen("04_biz_menu_opened")
                return True
        except Exception:
            continue

    log.warning("[BIZ] ❌ 点了 More 但 UIA 树里还是没有 BizMenuView！")
    _log_wechat_subtree(win, prefix="WECHAT_TREE_NO_BIZMENU")
    _save_screen("05_no_biz_menu_after_more")
    return False


def _find_qr_button(win, require_biz_menu: bool = True):
    """定位「玩家二维码」按钮。"""
    log.info(f"[BTN] 寻找 玩家二维码 按钮 (require_biz_menu={require_biz_menu}) ...")
    all_nodes = _walk_controls(win, 40)
    log.info(f"[BTN] UIA 树共遍历到 {len(all_nodes)} 个节点")

    # 1) 先 BizMenuView 下找（最准）
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
                            log.info(f"[BTN] ✅ BizMenuView 内找到 玩家二维码：ClassName={sc} rect=({r.left},{r.top},{r.right},{r.bottom})")
                            if "BizMenuButton" in sc or "XButton" in sc or "Button" in sc:
                                return sub
                            log.info(f"[BTN]   但 ClassName={sc} 看起来不像按钮（先当作按钮用）")
                            return sub
                    except Exception:
                        continue
            except Exception:
                continue

    # 2) 兜底：全树扫 Name+Class 组合
    log.info("[BTN] BizMenuView 下没找到，兜底全树扫 ...")
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
                log.info(f"[BTN] 兜底扫到 玩家二维码：ClassName={sc} rect={r} type={type(c).__name__}")
                candidates.append(c)
        except Exception:
            continue
    if candidates:
        # 优先 ClassName 含 Button 的
        def _score(c):
            sc = getattr(c, "ClassName", "") or ""
            return 0 if ("Button" in sc or "XButton" in sc) else 1
        candidates.sort(key=_score)
        log.info(f"[BTN] ✅ 兜底选最像按钮的那个（共 {len(candidates)} 个候选）")
        return candidates[0]

    log.warning("[BTN] ❌ 整个 UIA 树都没找到 玩家二维码 按钮")
    _log_all_class_names(win, keyword="")  # 全量 dump，方便看到所有 ClassName
    _save_screen("06_no_qr_button_found")
    return None


def _find_latest_qr_card(win, timeout=15.0):
    """在消息列表里找最新的舞萌DX二维码卡片。"""
    log.info(f"[CARD] 开始找最新二维码卡片，timeout={timeout}s (ClassName={CARD_CLASSNAME}, keyword={CARD_KEYWORD})")
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        candidates = []
        all_nodes = _walk_controls(win, 40)
        for c in all_nodes:
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
            # bottom 最大即最靠下最新
            latest = max(candidates, key=lambda t: (t[1].bottom if t[1] else 0))
            c, r = latest
            log.info(f"[CARD] ✅ 找到 {len(candidates)} 张候选，最新一张：Name={getattr(c,'Name','')[:80]} rect=({r.left},{r.top},{r.right},{r.bottom})")
            # 把其他候选也记一下
            for ci, ri in candidates:
                log.info(f"[CARD]   候选：Name={getattr(ci,'Name','')[:60]} rect=({ri.left},{ri.top},{ri.right},{ri.bottom})")
            return c
        left = int(deadline - time.time())
        if attempt % 3 == 0 or left <= 3:
            log.info(f"[CARD] 第 {attempt} 次扫描，仍无卡片（剩余 {left}s）")
            # 每 3 次 dump 一次微信 UIA 树（只前 4 层），看聊天列表到底长什么样
            if attempt % 6 == 0:
                _log_wechat_subtree(win, prefix=f"CARD_WAIT_ATTEMPT_{attempt}")
                _log_all_class_names(win, keyword="Chat")
        time.sleep(1)
    log.warning(f"[CARD] ❌ 等了 {timeout}s 都没找到任何 {CARD_CLASSNAME} 含 '{CARD_KEYWORD}' 的卡片")
    _log_wechat_subtree(win, prefix="CARD_TIMEOUT_TREE")
    _log_all_class_names(win, keyword="Chat")
    _log_all_class_names(win, keyword="mmui")
    _save_screen("07_no_card_after_timeout")
    return None


def _screenshot(region=None, label: str = "shot"):
    if ImageGrab is None and pyautogui is None:
        raise RuntimeError("Pillow 或 pyautogui 至少得装一个才能截图")
    t0 = time.time()
    if ImageGrab is not None:
        img = ImageGrab.grab(bbox=region) if region else ImageGrab.grab()
    else:
        left, top, right, bottom = region
        img = pyautogui.screenshot(region=(left, top, right - left, bottom - top))
    dt = time.time() - t0
    log.info(f"[SCREEN] {label} region={region} size={img.size} 用时={dt:.2f}s")
    return img


def _pil_to_cv2(pil_img):
    arr = np.array(pil_img.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def _decode_qr_image(pil_img) -> Tuple[Optional[str], Optional[str]]:
    """
    OpenCV QRCodeDetector 多策略。返回 (link, hit_label)。
    全部失败返回 (None, None)。
    """
    if not CV2_OK:
        log.warning("[QR] cv2 不可用，没法识别")
        return None, None
    try:
        bgr = _pil_to_cv2(pil_img)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        thresh = cv2.adaptiveThreshold(clahe, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, 11, 2)
        det = cv2.QRCodeDetector()
        strategies = [
            (bgr,                   "bgr"),
            (gray,                  "gray"),
            (clahe,                 "clahe"),
            (thresh,                "thresh"),
        ]
        for scale in [2, 3, 4, 6, 8, 10, 12]:
            h, w = gray.shape
            big = cv2.resize(gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
            strategies.append((big, f"gray x{scale}"))

        total = len(strategies)
        for idx, (img, label) in enumerate(strategies, 1):
            try:
                link, pts, straight = det.detectAndDecode(img)
            except Exception as e:
                log.info(f"[QR]   [{idx}/{total}] {label} detectAndDecode ❌ {e}")
                continue
            if link:
                log.info(f"[QR]   ✅ [{idx}/{total}] {label} => {link}")
                return link, label
            log.debug(f"[QR]   [{idx}/{total}] {label} => (empty)")
        log.info(f"[QR] ❌ 全部 {total} 种策略都没识别到二维码")
    except Exception as e:
        log.warning(f"[QR] ❌ cv2 解码整体异常：{type(e).__name__}: {e}")
        traceback.print_exc()
    return None, None


def _try_find_qr_link_on_screen(attempts=6, interval=2.0):
    """整屏截图+解码。"""
    log.info(f"[QR] 开始循环截图+解码 attempts={attempts} interval={interval}s")
    for i in range(attempts):
        log.info(f"[QR] --- 尝试 {i+1}/{attempts} ---")
        try:
            img = _screenshot(None, label=f"qr_attempt_{i+1}")
            link, hit_label = _decode_qr_image(img)
            if link:
                log.info(f"[QR] ✅ 第 {i+1} 次识别成功！策略={hit_label}  link={link}")
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
    try:
        all_nodes = _walk_controls(win, 25)
        for c in all_nodes:
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
    except Exception as e:
        log.warning(f"[ERR] 扫错误卡片时异常：{e}")
    return None


def _three_step_click_center(cx, cy, label: str, sleep_between=2.0):
    """3 步真实点击（pyautogui.click -> doubleClick -> uiautomation.Click）。"""
    log.info(f"[3STEP:{label}] 目标 ({cx},{cy}) sleep_between={sleep_between}")
    if pyautogui is None:
        log.warning(f"[3STEP:{label}] pyautogui 不可用！3 步点击做不了！")
        return
    try:
        pyautogui.moveTo(cx, cy, duration=0.1)
        log.info(f"[3STEP:{label}]   moveTo ✅")
    except Exception as e:
        log.warning(f"[3STEP:{label}]   moveTo ❌ {e}")
    time.sleep(0.2)
    # Step 1
    try:
        pyautogui.click(cx, cy)
        log.info(f"[3STEP:{label}]   click ✅ ({cx},{cy})")
    except Exception as e:
        log.warning(f"[3STEP:{label}]   click ❌ {e}")
    time.sleep(sleep_between)
    # Step 2
    try:
        pyautogui.doubleClick(cx, cy)
        log.info(f"[3STEP:{label}]   doubleClick ✅ ({cx},{cy})")
    except Exception as e:
        log.warning(f"[3STEP:{label}]   doubleClick ❌ {e}")
    time.sleep(sleep_between)


def _click_qr_button(win, button) -> bool:
    """点击 玩家二维码 按钮。"""
    log.info(f"[BTN_CLICK] 开始点 玩家二维码 按钮 ...")
    _activate_window(win)
    time.sleep(0.5)
    try:
        r = button.BoundingRectangle
        cx = int((r.left + r.right) / 2)
        cy = int((r.top + r.bottom) / 2)
        log.info(f"[BTN_CLICK] button rect=({r.left},{r.top},{r.right},{r.bottom}) center=({cx},{cy})")
    except Exception as e:
        log.warning(f"[BTN_CLICK] button.BoundingRectangle 失败：{e}")
        return False

    _save_screen("09_before_click_qr_button")
    _three_step_click_center(cx, cy, label="QR_BUTTON", sleep_between=0.5)
    try:
        button.Click()
        log.info("[BTN_CLICK]   button.Click() ✅")
    except Exception as e:
        log.warning(f"[BTN_CLICK]   button.Click() ❌ {e}，回退 _click_control")
        _click_control(button, label="QR_BUTTON_FALLBACK")
    _save_screen("10_after_click_qr_button")
    return True


def _click_card(win, card) -> None:
    """点击二维码卡片。"""
    log.info(f"[CARD_CLICK] 开始点卡片 ...")
    _activate_window(win)
    time.sleep(0.5)
    try:
        r = card.BoundingRectangle
        cx = int((r.left + r.right) / 2)
        cy = int((r.top + r.bottom) / 2)
        log.info(f"[CARD_CLICK] card rect=({r.left},{r.top},{r.right},{r.bottom}) center=({cx},{cy})")
    except Exception as e:
        log.warning(f"[CARD_CLICK] card.BoundingRectangle 失败：{e}")
        return
    _save_screen("11_before_click_card")
    _three_step_click_center(cx, cy, label="CARD", sleep_between=2.0)
    try:
        card.Click()
        log.info("[CARD_CLICK]   card.Click() ✅")
    except Exception as e:
        log.warning(f"[CARD_CLICK]   card.Click() ❌ {e}，回退 _click_control")
        _click_control(card, label="CARD_FALLBACK")
    _save_screen("12_after_click_card")


def _single_pass(win, button, pass_index: int, cfg: FetchConfig) -> Dict:
    """跑一次完整子流程。"""
    out = {"ok": False, "link": None, "stage": f"outer_pass_{pass_index}_start",
           "card_found": False, "card_clicked": False, "error": None}
    log.info(f"======= _single_pass #{pass_index}/{cfg.max_outer_retries} =======")

    out["stage"] = f"outer_pass_{pass_index}_click_button"
    _click_qr_button(win, button)

    out["stage"] = f"outer_pass_{pass_index}_wait_card"
    card = None
    for i in range(cfg.retry_count + 1):
        log.info(f"[PASS{pass_index}] 等待卡片，尝试 {i+1}/{cfg.retry_count+1} ...")
        card = _find_latest_qr_card(win, timeout=cfg.card_wait_timeout)
        if card is not None:
            break
        if i < cfg.retry_count:
            err = _detect_error_card(win)
            if err:
                log.warning(f"[PASS{pass_index}] 检测到错误卡片：{err}")
            time.sleep(cfg.retry_interval)
            log.info(f"[PASS{pass_index}] 再点一次玩家二维码按钮重试 ...")
            _click_qr_button(win, button)

    if card is None:
        out["stage"] = f"outer_pass_{pass_index}_no_card"
        err_txt = _detect_error_card(win)
        if err_txt:
            out["error"] = f"疑似公众号服务故障：{err_txt}"
        else:
            out["error"] = "没等到链接卡片（可能公众号返回失败消息 / 消息还没到达）"
        return out

    out["card_found"] = True
    out["stage"] = f"outer_pass_{pass_index}_click_card"
    _click_card(win, card)
    out["card_clicked"] = True

    log.info(f"[PASS{pass_index}] 点完卡片，睡 {cfg.wait_after_card_click}s 等内置浏览器加载 ...")
    time.sleep(cfg.wait_after_card_click)
    _save_screen("13_after_card_wait_10s")
    _log_all_top_windows("AFTER_CARD_WAIT")

    out["stage"] = f"outer_pass_{pass_index}_ocr"
    link = _try_find_qr_link_on_screen(attempts=6, interval=2.0)
    if link:
        out["ok"] = True
        out["link"] = link
        out["stage"] = "done"
    else:
        out["stage"] = f"outer_pass_{pass_index}_ocr_failed"
        out["error"] = (out.get("error") or "屏幕没识别到二维码") + \
                       " - 可能卡在内置浏览器 / 二维码未显示"
        _save_screen("14_ocr_failed")
        _log_all_top_windows("OCR_FAILED")
    return out


def fetch_qr_code(cfg=None) -> Dict:
    """主流程。"""
    cfg = cfg or FetchConfig()
    log.info("=" * 80)
    log.info(f"🚀 fetch_qr_code 启动 cfg={cfg}")
    log.info(f"📝 日志文件: {_LOG_FILE}")
    _co_init()

    result = {
        "ok": False, "link": None,
        "stage": "init",
        "error": None,
        "raw_png_base64": None,
        "outer_attempts": cfg.max_outer_retries,
        "outer_finished": 0,
        "pass_history": [],
        "last_error_text": None,
        "note": "",
        "log_file": _LOG_FILE,   # 让前端能直接拿到日志路径
    }

    # Step 1: 找微信窗口
    result["stage"] = "locate_window"
    win = _get_wechat_window()
    if win is None:
        result["error"] = "找不到微信窗口，请确认 PC 微信已登录"
        result["note"] = result["error"]
        _save_screen("FAIL_no_wechat_window")
        return result
    _save_screen("00_wechat_window_found")

    # Step 2: 激活
    result["stage"] = "activate"
    _activate_window(win)
    win_region = _get_window_region(win)
    _log_wechat_subtree(win, prefix="WECHAT_TREE_TOP")

    # Step 3: 打开底部菜单
    result["stage"] = "open_biz_menu"
    if not _ensure_biz_menu_view(win, win_region, cfg):
        result["error"] = "无法打开底部 我的记录/玩家二维码 菜单"
        return result

    # Step 4: 定位按钮
    button = None
    for i in range(cfg.retry_count + 1):
        result["stage"] = f"find_button_try_{i+1}"
        button = _find_qr_button(win, require_biz_menu=True)
        if button is not None:
            break
        if i < cfg.retry_count:
            time.sleep(cfg.retry_interval)
            _ensure_biz_menu_view(win, win_region, cfg)

    if button is None:
        log.warning("❌ 没法继续：玩家二维码按钮始终找不到")
        result["error"] = "找不到玩家二维码按钮（底部菜单里的 BizMenuButton）"
        result["note"] = result["error"]
        return result

    # Step 5: 外层循环
    for pass_index in range(1, cfg.max_outer_retries + 1):
        one = _single_pass(win, button, pass_index, cfg)
        result["pass_history"].append({
            "n": pass_index,
            "ok": one["ok"],
            "card_found": one["card_found"],
            "card_clicked": one["card_clicked"],
            "stage": one["stage"],
            "error": one.get("error"),
        })
        result["outer_finished"] = pass_index

        if one["ok"]:
            result["ok"] = True
            result["link"] = one["link"]
            result["stage"] = "done"
            result["note"] = f"成功获取二维码链接（第 {pass_index}/{cfg.max_outer_retries} 次尝试）"
            _save_screen("15_SUCCESS_QR")
            log.info(f"🎉 最终 link = {one['link']}")
            return result

        err_txt = one.get("error") or ""
        result["last_error_text"] = err_txt
        jitter = random.uniform(*cfg.outer_retry_jitter)
        log.warning(
            f"[pass {pass_index}/{cfg.max_outer_retries}] 失败：{err_txt}；"
            f"等 {jitter:.1f}s 后再点一次玩家二维码..."
        )
        if pass_index < cfg.max_outer_retries:
            result["stage"] = f"outer_pass_{pass_index}_sleep_{int(jitter)}s"
            time.sleep(jitter)

    # 失败收尾
    result["stage"] = "all_retries_exhausted"
    final_reason = result["last_error_text"] or "未知原因"
    result["error"] = (
        "已连续尝试 %d 次仍未拿到二维码卡片。最后一次返回：%s。"
        "请检查公众号是否真的返回了卡片，或稍后再点一次 玩家二维码。"
        % (cfg.max_outer_retries, final_reason)
    )
    result["note"] = (
        "连续 %d 次重试仍失败。最后一次：%s。"
        "详细日志：%s"
        % (cfg.max_outer_retries, final_reason, _LOG_FILE)
    )
    try:
        img = _screenshot(None, label="final_fail_fullscreen")
        result["raw_png_base64"] = _save_png_base64(img)
        _save_screen("16_FAIL_FINAL_FULLSCREEN")
    except Exception as e:
        log.warning(f"保存失败截图也出错：{e}")
    log.error(f"🚨 全部 {cfg.max_outer_retries} 次都失败了。最终错误：{final_reason}")
    return result


if __name__ == "__main__":
    print("=== 手动测试：fetch_qr_code() ===")
    r = fetch_qr_code()
    import json
    safe = {}
    for k, v in r.items():
        if isinstance(v, str) and len(v) > 200:
            safe[k] = v[:200] + "..."
        else:
            safe[k] = v
    print(json.dumps(safe, ensure_ascii=False, indent=2))
