"""
qr_fetcher.py - 微信自动化控制器
完整流程：微信窗口 -> 玩家二维码按钮 -> 链接卡片 -> 内置浏览器 -> OCR取链接
"""
import logging, io, base64, time
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
    from pyzbar.pyzbar import decode as pyzbar_decode
except ImportError:
    pyzbar_decode = None
try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("qr_fetcher")
WECHAT_WINDOW_NAME = "微信"
QR_BUTTON_NAME = "玩家二维码"
CARD_CLASSNAME = "mmui::ChatBubbleItemView"
CARD_KEYWORD = "舞萌DX"
@dataclass
class FetchConfig:
    wait_after_button_click: float = 2.0
    wait_after_card_click: float = 3.0
    wait_before_scan: float = 2.5
    retry_count: int = 2
    retry_interval: float = 2.0
    card_wait_timeout: float = 15.0
    qr_region: Optional[Tuple[int, int, int, int]] = None
def _get_wechat_window():
    if auto is None:
        log.error("uiautomation 未安装")
        return None
    try:
        win = auto.WindowControl(searchDepth=1, Name=WECHAT_WINDOW_NAME)
        win.GetRuntimeId()
        return win
    except Exception as e:
        log.warning(f"找不到微信窗口：{e}")
        return None
def _activate_window(win) -> bool:
    try:
        win.SetActive()
        time.sleep(0.6)
        return True
    except Exception as e:
        log.warning(f"激活窗口失败：{e}")
        return False
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
def _click_control(ctrl) -> bool:
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
            log.info(f"pyautogui 点击中心 ({cx},{cy})")
            return True
        except Exception as e:
            log.warning(f"pyautogui 坐标点击也失败：{e}")
    return False
def _find_qr_button(win):
    if win is None:
        return None
    try:
        btn = win.ButtonControl(searchDepth=15, Name=QR_BUTTON_NAME)
        btn.GetRuntimeId()
        log.info(f"找到按钮 Name={btn.Name}, ClassName={btn.ClassName}")
        return btn
    except Exception as e:
        log.info(f"ButtonControl 按 Name 没找到：{e}")
    for depth in (10, 20, 30):
        for c in _walk_controls(win, depth):
            try:
                if getattr(c, "Name", None) == QR_BUTTON_NAME:
                    log.info(f"遍历找到玩家二维码按钮 (depth={depth}) ClassName={c.ClassName}")
                    return c
            except Exception:
                continue
    log.warning("所有策略都没定位到 玩家二维码 按钮")
    return None
def _find_latest_qr_card(win, timeout=15.0):
    """在消息列表里找最新的舞萌DX链接卡片（取 y 最大即最靠下那条）。"""
    if win is None:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = []
        if auto is not None:
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
            latest = max(candidates, key=lambda c: (getattr(getattr(c, "BoundingRectangle", None), "bottom", None) or 0))
            log.info(f"找到链接卡片：Name={latest.Name[:60]}... ClassName={latest.ClassName}")
            return latest
        log.info(f"还没看到链接卡片，1s 后再找 (剩余 {int(deadline - time.time())}s)")
        time.sleep(1)
    log.warning(f"等了 {timeout}s 还是没找到链接卡片")
    return None
def _screenshot(region=None):
    if ImageGrab is None and pyautogui is None:
        raise RuntimeError("Pillow 或 pyautogui 至少得装一个才能截图")
    if ImageGrab is not None:
        return ImageGrab.grab(bbox=region) if region else ImageGrab.grab()
    if region:
        left, top, right, bottom = region
        return pyautogui.screenshot(region=(left, top, right - left, bottom - top))
    return pyautogui.screenshot()
def _decode_qr_image(pil_img):
    if pyzbar_decode is None:
        log.error("pyzbar 没装，没法从截图识别二维码")
        return None
    try:
        results = pyzbar_decode(pil_img)
        if not results:
            return None
        data = results[0].data
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="ignore")
        return data
    except Exception as e:
        log.warning(f"pyzbar 解码失败：{e}")
        return None
def _try_find_qr_link_on_screen(region=None, attempts=3, interval=1.5):
    for i in range(attempts):
        log.info(f"二维码识别尝试 {i+1}/{attempts} ...")
        try:
            img = _screenshot(region)
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
def fetch_qr_code(cfg=None) -> Dict:
    """完整跑一遍 点按钮 -> 点链接卡片 -> 取二维码链接。"""
    cfg = cfg or FetchConfig()
    result = {"ok": False, "link": None, "stage": "init",
              "error": None, "raw_png_base64": None}
    result["stage"] = "locate_window"
    win = _get_wechat_window()
    if win is None:
        result["error"] = "找不到微信窗口，请确认 PC 微信已登录"
        return result
    result["stage"] = "activate"
    _activate_window(win)
    button = None
    for i in range(cfg.retry_count + 1):
        result["stage"] = f"find_button_try_{i+1}"
        button = _find_qr_button(win)
        if button is not None:
            break
        if i < cfg.retry_count:
            time.sleep(cfg.retry_interval)
    if button is None:
        log.warning("没找到玩家二维码按钮，跳过点击，假设你已经手动点过了")
    else:
        result["stage"] = "click_button"
        clicked = _click_control(button)
        if not clicked:
            result["error"] = "找到玩家二维码按钮但点击失败"
            return result
    result["stage"] = "wait_card"
    time.sleep(cfg.wait_after_button_click)
    card = None
    for i in range(cfg.retry_count + 1):
        card = _find_latest_qr_card(win, timeout=cfg.card_wait_timeout)
        if card is not None:
            break
        log.info(f"等链接卡片，retry {i+1}/{cfg.retry_count+1}")
        if i < cfg.retry_count and button is not None:
            log.info("重试点击玩家二维码按钮...")
            _click_control(button)
            time.sleep(cfg.wait_after_button_click)
    if card is None:
        log.warning("没等到链接卡片，跳过点击，假设二维码已经在屏幕上")
    else:
        result["stage"] = "click_card"
        _click_control(card)
    time.sleep(cfg.wait_after_card_click)
    time.sleep(cfg.wait_before_scan)
    result["stage"] = "ocr_screen"
    link = _try_find_qr_link_on_screen(region=cfg.qr_region, attempts=4, interval=1.5)
    if link:
        result["ok"] = True
        result["link"] = link
        result["stage"] = "done"
        return result
    result["error"] = "UI 操作完成但从屏幕没识别到二维码，请确认微信内置浏览器已经弹出并显示二维码"
    try:
        img = _screenshot(cfg.qr_region)
        result["raw_png_base64"] = _save_png_base64(img)
    except Exception as e:
        log.warning(f"保存失败截图也出错：{e}")
    return result
if __name__ == "__main__":
    print("=== 手动测试：fetch_qr_code() ===")
    r = fetch_qr_code()
    import json
    safe = {k: (v[:120] + "..." if isinstance(v, str) and len(v) > 120 else v) for k, v in r.items()}
    print(json.dumps(safe, ensure_ascii=False, indent=2))