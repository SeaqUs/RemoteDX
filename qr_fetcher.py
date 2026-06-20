"""
qr_fetcher.py - 微信自动化控制器
=================================
流程：
    激活微信窗口 -> 点击舞萌 DX 公众号里的 玩家二维码 按钮 ->
    等待弹出二维码面板 -> 截图 + pyzbar 解码 -> 返回登录链接。
控件定位依据（来自 Inspect.exe 探针数据）：
    窗口 : Name = "微信"
    按钮 : Name = "玩家二维码", ControlType = Button,
           ClassName = "mmui::BizMenuButton", FrameworkId = "Qt"
设计原则：
    1. uiautomation 优先（稳定、有语义）；
    2. 找不到时回退到 pyautogui 坐标模拟；
    3. 拿不到链接时回退到 pyzbar 屏幕 OCR（最兜底的路径）；
    4. 每一步都打日志，方便定位哪一步失败。
"""
import logging
import io
import base64
import time
from dataclasses import dataclass
from typing import Optional, Tuple, List, Dict
# ============== 可选依赖（不装也能跑，但会自动降级） ==============
try:
    import uiautomation as auto   # type: ignore
except ImportError:
    auto = None
try:
    import pyautogui              # type: ignore
    pyautogui.FAILSAFE = True     # 鼠标甩到左上角时自动中止，防止失控
except ImportError:
    pyautogui = None
try:
    from pyzbar.pyzbar import decode as pyzbar_decode  # type: ignore
except ImportError:
    pyzbar_decode = None
try:
    from PIL import ImageGrab    # type: ignore
except ImportError:
    ImageGrab = None
# ============== 日志 ==============
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("qr_fetcher")
# ============== 常量（来自 Inspect 探针） ==============
WECHAT_WINDOW_NAME = "微信"
# ============== 配置 ==============
@dataclass
class FetchConfig:
    """一次扫码任务的参数，全部带默认值。"""
    wait_after_click: float = 2.0       # 点完按钮后等它渲染
    wait_before_scan: float = 3.0       # 再等一会儿，确保二维码画面稳定
    retry_count: int = 2                # 找按钮失败时的重试次数
    retry_interval: float = 2.0         # 重试之间间隔
    qr_region: Optional[Tuple[int, int, int, int]] = None
    # qr_region 格式 (left, top, right, bottom)；None 表示全屏扫
    # 可以先不传，肉眼看见二维码后再填进来
# ============== 微信窗口 / 按钮定位 ==============
def _get_wechat_window() -> Optional["auto.WindowControl"]:
    """找到名字叫 '微信' 的顶层窗口，找不到返回 None。"""
    if auto is None:
        log.error("uiautomation 未安装，无法定位微信窗口")
        return None
    try:
        win = auto.WindowControl(searchDepth=1, Name=WECHAT_WINDOW_NAME)
        win.GetRuntimeId()  # 触发一次实际查找，不存在会抛异常
        return win
    except Exception as e:
        log.warning(f"找不到微信窗口 微信：{e}")
        return None
def _activate_window(win) -> bool:
    """把窗口拉到前台。"""
    try:
        win.SetActive()
        time.sleep(0.6)
        return True
    except Exception as e:
        log.warning(f"激活窗口失败：{e}")
        return False
def _find_qr_button(win) -> Optional["auto.ButtonControl"]:
    """在微信窗口里找 玩家二维码 按钮。
    思路：FrameworkId 是 Qt，uiautomation 未必能递归枚举到 Qt WebEngine
    里的子控件；我们直接用 Name 匹配。
    """
    if win is None:
        return None
    # 策略 1：直接在窗口下按 Name + Button 类型找
    try:
        btn = win.ButtonControl(searchDepth=10, Name="玩家二维码")
        btn.GetRuntimeId()
        log.info(f"找到按钮：Name={btn.Name}, ClassName={btn.ClassName}")
        return btn
    except Exception as e:
        log.info(f"uiautomation 按 Name 没找到：{e}")
    # 策略 2：递归遍历整个窗口子树，按 Name 挑
    controls = []
    def walk(node, depth):
        if depth <= 0:
            return
        try:
            for c in node.GetChildren():
                controls.append(c)
                walk(c, depth - 1)
        except Exception:
            pass
    for depth in (5, 10, 20):
        controls.clear()
        walk(win, depth)
        for c in controls:
            try:
                if getattr(c, "Name", None) == "玩家二维码":
                    log.info(f"遍历找到按钮 (depth={depth})：ClassName={c.ClassName}")
                    return c
            except Exception:
                continue
    log.warning("所有 uiautomation 策略都没定位到 玩家二维码 按钮")
    return None
def _click_control(ctrl) -> bool:
    """uiautomation 优先，失败回退到 pyautogui 中心坐标点击。"""
    # 第一选择：Invoke Pattern（官方推荐方式，比 Click() 更稳）
    if auto is not None:
        try:
            ctrl.GetInvokePattern().Invoke()
            log.info("通过 Invoke Pattern 点击成功")
            return True
        except Exception:
            pass
        try:
            ctrl.Click()
            log.info("通过 .Click() 点击成功")
            return True
        except Exception as e:
            log.warning(f"uiautomation 点击失败：{e}")
    # 第二选择：pyautogui 点中心
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
# ============== 二维码识别 ==============
def _screenshot(region: Optional[Tuple[int, int, int, int]] = None):
    """截指定区域（region 格式 left,top,right,bottom），region=None 就是全屏。返回 PIL.Image。"""
    if ImageGrab is None and pyautogui is None:
        raise RuntimeError("Pillow 或 pyautogui 至少得装一个才能截图")
    if ImageGrab is not None:
        return ImageGrab.grab(bbox=region) if region else ImageGrab.grab()
    if region:
        left, top, right, bottom = region
        return pyautogui.screenshot(region=(left, top, right - left, bottom - top))
    return pyautogui.screenshot()
def _decode_qr_image(pil_img) -> Optional[str]:
    """pyzbar 解码一张 PIL 图片。失败返回 None。"""
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
def _try_find_qr_link_on_screen(region: Optional[Tuple[int, int, int, int]] = None,
                                 attempts: int = 3, interval: float = 1.5) -> Optional[str]:
    """连续截几次屏幕，尝试识别到二维码。"""
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
def _save_png_base64(pil_img) -> str:
    """把 PIL.Image 转成 Base64 PNG 字符串。"""
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")
# ============== 对外主入口 ==============
def fetch_qr_code(cfg: Optional[FetchConfig] = None) -> Dict:
    """完整跑一遍 "点按钮 -> 取二维码链接"。
    Returns:
        dict = {
            "ok": bool,
            "link": str | None,
            "stage": str,          # 哪个阶段成功/失败
            "error": str | None,
            "raw_png_base64": str | None,  # 失败现场截图
        }
    """
    cfg = cfg or FetchConfig()
    result = {"ok": False, "link": None, "stage": "init",
              "error": None, "raw_png_base64": None}
    # ---- 1. 找微信窗口 ----
    result["stage"] = "locate_window"
    win = _get_wechat_window()
    if win is None:
        result["error"] = "找不到微信窗口，请确认 PC 微信已登录并且窗口名字就是 微信"
        return result
    # ---- 2. 激活 ----
    result["stage"] = "activate"
    _activate_window(win)
    # ---- 3. 找 玩家二维码 按钮 ----
    button = None
    for i in range(cfg.retry_count + 1):
        result["stage"] = f"find_button_try_{i+1}"
        button = _find_qr_button(win)
        if button is not None:
            break
        if i < cfg.retry_count:
            time.sleep(cfg.retry_interval)
    # ---- 4. 点击按钮 ----
    if button is not None:
        result["stage"] = "click"
        clicked = _click_control(button)
        if not clicked:
            result["error"] = "找到按钮但点击失败"
            return result
    else:
        # 没找到按钮时，假设二维码已经显示在屏幕上，直接 OCR
        log.warning("UI 没找到按钮，跳过点击阶段，直接尝试识别屏幕二维码")
    time.sleep(cfg.wait_after_click)
    time.sleep(cfg.wait_before_scan)
    # ---- 5. 从屏幕识别二维码 ----
    result["stage"] = "ocr_screen"
    link = _try_find_qr_link_on_screen(
        region=cfg.qr_region,
        attempts=3, interval=1.8,
    )
    if link:
        result["ok"] = True
        result["link"] = link
        result["stage"] = "done"
        return result
    result["error"] = "UI 点击成功但从屏幕没识别到二维码，请确认二维码已经弹出。"
    # 即使失败也保存一张截图，让前端能直观看到现场
    try:
        img = _screenshot(cfg.qr_region)
        result["raw_png_base64"] = _save_png_base64(img)
    except Exception as e:
        log.warning(f"保存失败截图也出错：{e}")
    return result
if __name__ == "__main__":
    # 直接 python qr_fetcher.py 可以快速验证整条链路
    print("=== 手动测试：fetch_qr_code() ===")
    r = fetch_qr_code()
    print(r)