"""
qr_fetcher.py - 微信自动化控制器
完整流程：微信窗口 -> More 按钮打开底部菜单 BizMenuView -> 点击「玩家二维码」 BizMenuButton -> 
返回聊天列表里的最新舞萌DX二维码卡片（ChatBubbleItemView） -> 3步点击序列 -> 微信内置浏览器弹出示例二维码 -> 全屏截图 + OpenCV 解码。

关键：
1) uiautomation 依赖 COM (IUIAutomation)。Flask 后台线程里必须显式 CoInitialize()，
   否则 [WinError -2147221008] 尚未调用 CoInitialize。
2) 「玩家二维码」不在聊天列表，而是底部 BizMenuView 里的 BizMenuButton。
   必须先点聊天窗口左下角 More 按钮打开这个菜单，再从里面点「玩家二维码」。
3) 卡片点击必须走 3 步序列：pyautogui.click → pyautogui.doubleClick → uiautomation.Click()。
   只走其中任意一步，微信都不会触发内置浏览器弹窗。
4) 二维码弹窗在微信内置浏览器浮窗，不在主窗口 BoundingRectangle 里，必须整屏截图。
"""
import logging, io, base64, time, random
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

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("qr_fetcher")

WECHAT_WINDOW_NAME = "微信"
QR_BUTTON_NAME = "玩家二维码"
CARD_CLASSNAME = "mmui::ChatBubbleItemView"
CARD_KEYWORD = "舞萌DX"

ERROR_KEYWORDS = ("服务出现故障", "服务异常", "稍后再试", "公众号提供的服务",
                  "二维码已过期", "重新扫码", "expired")


@dataclass
class FetchConfig:
    wait_after_button_click: float = 4.0       # 玩家二维码 按钮点开后，服务器发卡片大约需要 3~6 秒
    wait_after_card_click: float = 10.0        # 卡片点开后，微信内置浏览器加载二维码约 5~10 秒
    wait_before_scan: float = 2.5
    retry_count: int = 2
    retry_interval: float = 2.0
    card_wait_timeout: float = 20.0
    qr_region: Optional[Tuple[int, int, int, int]] = None
    max_outer_retries: int = 5
    outer_retry_jitter: Tuple[float, float] = (5.0, 8.0)
    more_button_offset: Tuple[int, int] = (27, 1407)  # 聊天窗口左下角 More 按钮位置（相对窗口左上角）


def _co_init():
    """在当前 OS 线程里初始化 COM（Flask 后台线程必须调用，否则 uiautomation 报 CoInitialize 未调用）。"""
    if pythoncom is not None:
        try:
            pythoncom.CoInitialize()
            log.info("pythoncom.CoInitialize OK")
            return True
        except Exception as e:
            log.warning(f"pythoncom.CoInitialize failed: {e}")
    if auto is not None and hasattr(auto, "InitializeUIAutomationInCurrentThread"):
        try:
            auto.InitializeUIAutomationInCurrentThread()
            log.info("InitializeUIAutomationInCurrentThread OK")
            return True
        except Exception as e:
            log.warning(f"InitializeUIAutomationInCurrentThread failed: {e}")
    return False


def _get_wechat_window():
    """用 uiautomation 查找主微信窗口（ClassName 已知是 mmui::MainWindow）。"""
    if auto is None:
        log.error("uiautomation 未安装")
        return None
    try:
        win = auto.WindowControl(searchDepth=1, Name=WECHAT_WINDOW_NAME)
        win.GetRuntimeId()
        return win
    except Exception as e:
        log.warning(f"按 Name=微信 没找到窗口：{e}")
    try:
        win = auto.WindowControl(searchDepth=1, ClassName="mmui::MainWindow")
        win.GetRuntimeId()
        return win
    except Exception as e:
        log.warning(f"按 ClassName=mmui::MainWindow 也没找到：{e}")
    return None


def _get_window_region(win) -> Optional[Tuple[int, int, int, int]]:
    """拿到微信窗口的屏幕坐标（left, top, right, bottom）。"""
    if win is None:
        return None
    try:
        rect = win.BoundingRectangle
        return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception as e:
        log.warning(f"取窗口坐标失败：{e}")
        return None


def _activate_window(win) -> bool:
    """
    把微信窗口真正打到前台：uiautomation.ShowWindow + Win32 ShowWindow(Restore)+SetForeground+BringWindowToTop。
    实测只调用 uiautomation.SetActive() 微信不会收到键盘/鼠标消息。
    """
    try:
        if auto is not None and hasattr(auto, "SW"):
            try:
                win.ShowWindow(auto.SW.Restore)
            except Exception:
                pass
            try:
                win.ShowWindow(auto.SW.Show)
            except Exception:
                pass
        if ctypes is not None:
            user32 = ctypes.windll.user32
            try:
                hwnd = win.NativeWindowHandle
                if hwnd:
                    user32.ShowWindow(hwnd, 9)   # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    user32.BringWindowToTop(hwnd)
            except Exception:
                pass
        try:
            win.SetActive()
        except Exception:
            pass
        try:
            win.SetFocus()
        except Exception:
            pass
        time.sleep(0.6)
        return True
    except Exception as e:
        log.warning(f"激活窗口失败：{e}")
        return False


def _walk_controls(node, max_depth: int) -> list:
    """BFS/DFS 遍历 UIA 控件树。"""
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


def _click_control(ctrl) -> bool:
    """兜底点击：先走 Invoke Pattern，再走 uiautomation.Click，最后才 pyautogui 坐标点击。"""
    if auto is not None:
        try:
            ctrl.GetInvokePattern().Invoke()
            log.info("Invoke Pattern 点击成功")
            return True
        except Exception:
            pass
        try:
            ctrl.Click()
            log.info(".Click() 点击成功")
            return True
        except Exception as e:
            log.warning(f"uiautomation 点击失败：{e}")
    if pyautogui is not None:
        try:
            rect = ctrl.BoundingRectangle
            cx = int((rect.left + rect.right) / 2)
            cy = int((rect.top + rect.bottom) / 2)
            pyautogui.click(cx, cy)
            log.info(f"pyautogui 坐标点击 ({cx},{cy})")
            return True
        except Exception as e:
            log.warning(f"pyautogui 坐标点击失败：{e}")
    return False


def _ensure_biz_menu_view(win, win_region: Tuple[int, int, int, int], cfg: FetchConfig):
    """
    找到/打开底部 BizMenuView（包含 我的记录 / 玩家二维码 / 资讯 的菜单）。
    这个菜单默认关闭，需要点聊天窗口左下角 More 按钮才能展开。
    """
    # 先尝试直接在控件树里找到（可能已经展开了）
    for c in _walk_controls(win, 20):
        try:
            if "BizMenuView" in (getattr(c, "ClassName", "") or ""):
                log.info("底部菜单 BizMenuView 已可见")
                return True
        except Exception:
            continue

    # 没找到 -> 点左下角 More 按钮把它展开
    left, top, _, bottom = win_region
    mx = left + cfg.more_button_offset[0]
    my = top + cfg.more_button_offset[1]
    log.info(f"底部菜单未展开，点 More 按钮 @({mx},{my})")
    _activate_window(win); time.sleep(0.3)
    if pyautogui is not None:
        pyautogui.click(mx, my)
    time.sleep(3.0)

    # 再次扫描
    for c in _walk_controls(win, 20):
        try:
            if "BizMenuView" in (getattr(c, "ClassName", "") or ""):
                log.info("展开后 BizMenuView 找到")
                return True
        except Exception:
            continue
    log.warning("点了 More 但没扫描到 BizMenuView")
    return False


def _find_qr_button(win, require_biz_menu: bool = True):
    """
    定位「玩家二维码」按钮。
    - require_biz_menu=True：限定必须是 BizMenuView 下的 BizMenuButton（底部菜单里那个）。
      原因：聊天列表里也可能出现玩家二维码 文本（但那是之前的卡片，不是按钮）。
    """
    if win is None:
        return None

    all_nodes = _walk_controls(win, 30)

    # 先扫 BizMenuView 的直接子节点（最快，且避免和聊天列表里旧卡片混淆）
    for c in all_nodes:
        try:
            cls = getattr(c, "ClassName", "") or ""
            if "BizMenuView" in cls:
                for sub in _walk_controls(c, 10):
                    try:
                        nm = getattr(sub, "Name", "") or ""
                        sc = getattr(sub, "ClassName", "") or ""
                        if nm == QR_BUTTON_NAME and "BizMenuButton" in sc:
                            log.info(f"BizMenuView 内找到 玩家二维码 按钮，ClassName={sc}")
                            return sub
                    except Exception:
                        continue
        except Exception:
            continue

    # 兜底：全树扫 Name+Class 组合
    for c in all_nodes:
        try:
            nm = getattr(c, "Name", "") or ""
            sc = getattr(c, "ClassName", "") or ""
            if nm == QR_BUTTON_NAME and ("BizMenuButton" in sc or "XButton" in sc):
                log.info(f"全局找到 玩家二维码 按钮 ClassName={sc}")
                return c
        except Exception:
            continue

    log.warning("没定位到 玩家二维码 按钮")
    return None


def _find_latest_qr_card(win, timeout=15.0):
    """在消息列表里找最新的舞萌DX 二维码卡片（取 y 最大即最靠下那条）。"""
    if win is None:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = []
        all_nodes = _walk_controls(win, 30)
        for c in all_nodes:
            try:
                cn = getattr(c, "ClassName", "") or ""
                nm = getattr(c, "Name", "") or ""
                if CARD_CLASSNAME in cn and CARD_KEYWORD in str(nm):
                    candidates.append(c)
            except Exception:
                continue
        if candidates:
            latest = max(candidates,
                         key=lambda c: (getattr(getattr(c, "BoundingRectangle", None),
                                                 "bottom", None) or 0))
            log.info(f"找到链接卡片：Name={latest.Name[:80]}")
            return latest
        log.info(f"还没看到链接卡片，1s 后再找 (剩余 {int(deadline - time.time())}s)")
        time.sleep(1)
    log.warning(f"等了 {timeout}s 还是没找到链接卡片")
    return None


def _screenshot(region=None):
    """截屏幕；region 是 (L,T,R,B) 像素坐标；不传则截全屏。"""
    if ImageGrab is None and pyautogui is None:
        raise RuntimeError("Pillow 或 pyautogui 至少得装一个才能截图")
    if ImageGrab is not None:
        return ImageGrab.grab(bbox=region) if region else ImageGrab.grab()
    if region:
        left, top, right, bottom = region
        return pyautogui.screenshot(region=(left, top, right - left, bottom - top))
    return pyautogui.screenshot()


def _pil_to_cv2(pil_img):
    """Pillow -> OpenCV BGR。"""
    arr = np.array(pil_img.convert("RGB"))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def _decode_qr_image(pil_img) -> Optional[str]:
    """
    多策略 OpenCV QRCodeDetector：
    - 原始 BGR
    - 灰度
    - CLAHE + adaptiveThreshold
    - 2x/4x 放大
    """
    if not CV2_OK:
        return None

    try:
        bgr = _pil_to_cv2(pil_img)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        thresh = cv2.adaptiveThreshold(clahe, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, 11, 2)
        det = cv2.QRCodeDetector()

        for img, label in [
            (bgr, "bgr"), (gray, "gray"), (thresh, "thresh"), (clahe, "clahe"),
        ]:
            link, _, _ = det.detectAndDecode(img)
            if link:
                log.info(f"cv2[{label}] 识别到二维码：{link[:80]}")
                return link

        h, w = gray.shape
        for scale in [2, 3, 4, 6, 8]:
            big = cv2.resize(gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
            link, _, _ = det.detectAndDecode(big)
            if link:
                log.info(f"cv2[gray x{scale}] 识别到二维码：{link[:80]}")
                return link
    except Exception as e:
        log.warning(f"cv2 解码失败：{e}")
    return None


def _try_find_qr_link_on_screen(attempts=6, interval=2.0):
    """整屏截图+解码，最多 attempts 次。"""
    for i in range(attempts):
        log.info(f"二维码识别尝试 {i+1}/{attempts} ...")
        try:
            img = _screenshot(None)
            link = _decode_qr_image(img)
            if link:
                log.info(f"识别到链接：{link[:120]}")
                return link
        except Exception as e:
            log.warning(f"截图/解码异常：{e}")
        time.sleep(interval)
    return None


def _save_png_base64(pil_img):
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _detect_error_card(win) -> Optional[str]:
    """在当前微信可见 UI 里扫有没有「服务出现故障」之类的错误提示。"""
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
                        log.warning(f"疑似错误提示：{nm[:120]}")
                        return nm
            except Exception:
                continue
    except Exception as e:
        log.warning(f"扫错误卡片时 UIA 抛异常：{e}")
    return None


def _three_step_click_center(cx, cy, sleep_between=2.0):
    """
    实测唯一能让微信弹出内置浏览器的点击序列：
    1) pyautogui.click  ← 产生 WM_LBUTTONDOWN/UP
    2) pyautogui.doubleClick ← 覆盖只对双击响应的控件
    3) uiautomation.Click() ← 触发 InvokePattern / 内部事件
    中间 sleep_between 让微信有时间处理每一层事件。
    """
    if pyautogui is not None:
        pyautogui.moveTo(cx, cy, duration=0.1)
        time.sleep(0.2)
        try:
            pyautogui.click(cx, cy)
            log.info(f"  pyautogui.click ({cx},{cy})")
        except Exception as e:
            log.warning(f"  pyautogui.click 失败：{e}")
        time.sleep(sleep_between)
        try:
            pyautogui.doubleClick(cx, cy)
            log.info(f"  pyautogui.doubleClick ({cx},{cy})")
        except Exception as e:
            log.warning(f"  pyautogui.doubleClick 失败：{e}")
        time.sleep(sleep_between)


def _click_qr_button(win, button) -> bool:
    """点击 玩家二维码 按钮。"""
    _activate_window(win); time.sleep(0.5)
    r = button.BoundingRectangle
    cx = int((r.left + r.right) / 2)
    cy = int((r.top + r.bottom) / 2)
    _three_step_click_center(cx, cy, sleep_between=0.5)
    try:
        button.Click()
        log.info("  button.Click()")
    except Exception as e:
        log.warning(f"  button.Click() 失败，回退 Invoke：{e}")
        _click_control(button)
    return True


def _click_card(win, card) -> None:
    """
    点击二维码卡片，必须走 3 步序列：
    pyautogui.click → pyautogui.doubleClick → uiautomation.Click()。
    只走任意一步，实测都不会触发微信内置浏览器弹窗。
    """
    _activate_window(win); time.sleep(0.5)
    r = card.BoundingRectangle
    cx = int((r.left + r.right) / 2)
    cy = int((r.top + r.bottom) / 2)
    _three_step_click_center(cx, cy, sleep_between=2.0)
    try:
        card.Click()
        log.info("  card.Click()")
    except Exception as e:
        log.warning(f"  card.Click() 失败：{e}")
        _click_control(card)


def _single_pass(win, button, pass_index: int, cfg: FetchConfig) -> Dict:
    """跑一次完整子流程：点按钮 -> 等卡片 -> 点卡片 -> OCR。"""
    out = {"ok": False, "link": None, "stage": f"outer_pass_{pass_index}_start",
           "card_found": False, "card_clicked": False, "error": None}

    out["stage"] = f"outer_pass_{pass_index}_click_button"
    _click_qr_button(win, button)

    out["stage"] = f"outer_pass_{pass_index}_wait_card"
    card = None
    for i in range(cfg.retry_count + 1):
        card = _find_latest_qr_card(win, timeout=cfg.card_wait_timeout)
        if card is not None:
            break
        log.info(f"[pass {pass_index}] 等卡片 retry {i+1}/{cfg.retry_count+1}")
        if i < cfg.retry_count:
            time.sleep(cfg.retry_interval)
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

    time.sleep(cfg.wait_after_card_click)
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
    return out


def fetch_qr_code(cfg=None) -> Dict:
    """
    Debug 版主流程：外层最多跑 max_outer_retries 次完整子流程。
    每次如果没等到卡片 / 扫不到二维码，就等 jitter 秒后再点一次 玩家二维码 重来。
    """
    cfg = cfg or FetchConfig()
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
    }

    log.info("fetch_qr_code: locating WeChat window...")
    result["stage"] = "locate_window"
    win = _get_wechat_window()
    if win is None:
        result["error"] = "找不到微信窗口，请确认 PC 微信已登录"
        result["note"] = result["error"]
        return result

    log.info(f"fetch_qr_code: found window Name={win.Name} ClassName={win.ClassName}")
    result["stage"] = "activate"
    _activate_window(win)
    win_region = _get_window_region(win)
    log.info(f"微信窗口区域：{win_region}")

    result["stage"] = "open_biz_menu"
    if not _ensure_biz_menu_view(win, win_region, cfg):
        result["error"] = "无法打开底部 我的记录/玩家二维码 菜单"
        return result

    button = None
    for i in range(cfg.retry_count + 1):
        result["stage"] = f"find_button_try_{i+1}"
        button = _find_qr_button(win, require_biz_menu=True)
        if button is not None:
            break
        if i < cfg.retry_count:
            # 可能菜单关闭了 -> 重新展开
            _ensure_biz_menu_view(win, win_region, cfg)
            time.sleep(cfg.retry_interval)

    if button is None:
        log.warning("没找到玩家二维码按钮，外层重试也没法继续，直接放弃")
        result["error"] = "找不到玩家二维码按钮（底部菜单里的 BizMenuButton）"
        result["note"] = result["error"]
        return result

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

    result["stage"] = "all_retries_exhausted"
    final_reason = result["last_error_text"] or "未知原因"
    result["error"] = (
        "已连续尝试 %d 次仍未拿到二维码卡片。最后一次返回：%s。"
        "请检查公众号是否真的返回了卡片，或稍后再点一次 玩家二维码。"
        % (cfg.max_outer_retries, final_reason)
    )
    result["note"] = (
        "连续 %d 次重试仍失败。最后一次：%s。"
        "请先手动打开微信确认公众号有没有真的发卡片，"
        "或检查是否有网络 / 公众号服务故障。"
        % (cfg.max_outer_retries, final_reason)
    )

    # 失败时截一张整屏，方便排查
    try:
        img = _screenshot(None)
        result["raw_png_base64"] = _save_png_base64(img)
    except Exception as e:
        log.warning(f"保存失败截图也出错：{e}")

    return result


if __name__ == "__main__":
    print("=== 手动测试：fetch_qr_code() ===")
    r = fetch_qr_code()
    import json
    safe = {}
    for k, v in r.items():
        if isinstance(v, str) and len(v) > 120:
            safe[k] = v[:120] + "..."
        else:
            safe[k] = v
    print(json.dumps(safe, ensure_ascii=False, indent=2))
