"""
qr_fetcher.py - 微信自动化控制器
完整流程：微信窗口 -> 玩家二维码按钮 -> 链接卡片 -> 内置浏览器 -> OCR取链接
Debug 策略：外层最多跑 max_outer_retries 次完整流程，
每次如果出现「没等到链接卡片」或「屏幕扫不到二维码」这类疑似服务器故障，
会等 5~8 秒后再点一次 玩家二维码 按钮重新拉卡片，直到拿到链接或耗尽次数。
"""
import logging, io, base64, time, random
from dataclasses import dataclass, field
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

# 关键字匹配失败卡片 / 错误提示，便于区分服务器故障 vs 普通未收到
ERROR_KEYWORDS = ("服务出现故障", "服务异常", "稍后再试", "公众号提供的服务",
                  "expired", "expired", "expired", "二维码已过期", "重新扫码")


@dataclass
class FetchConfig:
    wait_after_button_click: float = 2.0
    wait_after_card_click: float = 3.0
    wait_before_scan: float = 2.5
    retry_count: int = 2               # 单次流程内找按钮/等卡片的内部重试
    retry_interval: float = 2.0
    card_wait_timeout: float = 15.0
    qr_region: Optional[Tuple[int, int, int, int]] = None
    max_outer_retries: int = 5          # 外层完整流程最多跑几次（针对服务器故障）
    outer_retry_jitter: Tuple[float, float] = (5.0, 8.0)  # 两次外层重试之间随机等 5~8s


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
            latest = max(candidates,
                         key=lambda c: (getattr(getattr(c, "BoundingRectangle", None),
                                                 "bottom", None) or 0))
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


def _detect_error_card(win) -> Optional[str]:
    """在当前微信可见 UI 里扫有没有「服务出现故障」之类的错误提示，
    有就返回那段文字，没有返回 None。"""
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
                        log.warning(f"检测到疑似错误提示卡片：{nm[:120]}")
                        return nm
            except Exception:
                continue
    except Exception as e:
        log.warning(f"扫错误卡片时 UIA 抛异常：{e}")
    return None


def _single_pass(win, button, pass_index: int, cfg: FetchConfig) -> Dict:
    """跑一次完整子流程：点按钮 → 等卡片 → 点卡片 → OCR。"""
    out = {"ok": False, "link": None, "stage": f"outer_pass_{pass_index}_start",
           "card_found": False, "card_clicked": False, "error": None}

    out["stage"] = f"outer_pass_{pass_index}_wait_card"
    time.sleep(cfg.wait_after_button_click)

    card = None
    for i in range(cfg.retry_count + 1):
        card = _find_latest_qr_card(win, timeout=cfg.card_wait_timeout)
        if card is not None:
            break
        log.info(f"[pass {pass_index}] 等链接卡片，内部 retry {i+1}/{cfg.retry_count+1}")
        if i < cfg.retry_count and button is not None:
            log.info(f"[pass {pass_index}] 再点一次玩家二维码按钮重试拉卡片...")
            _click_control(button)
            time.sleep(cfg.wait_after_button_click)

    if card is None:
        out["stage"] = f"outer_pass_{pass_index}_no_card"
        err_txt = _detect_error_card(win)
        if err_txt:
            out["error"] = f"疑似公众号服务故障：{err_txt}"
        else:
            out["error"] = "没等到链接卡片（可能公众号返回失败消息，也可能消息还没到达）"
        return out

    out["card_found"] = True
    out["stage"] = f"outer_pass_{pass_index}_click_card"
    _click_control(card)
    out["card_clicked"] = True

    time.sleep(cfg.wait_after_card_click)
    time.sleep(cfg.wait_before_scan)

    out["stage"] = f"outer_pass_{pass_index}_ocr"
    link = _try_find_qr_link_on_screen(region=cfg.qr_region, attempts=4, interval=1.5)
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
    """Debug 版主流程：外层最多跑 max_outer_retries 次完整子流程。
    每次跑完把 stage 字段带上当前 pass 编号，前端可以显示「第 N / M 次尝试」。
    """
    cfg = cfg or FetchConfig()
    result = {
        "ok": False, "link": None,
        "stage": "init",
        "error": None,
        "raw_png_base64": None,
        # ---- debug 额外字段，前端 / API 可以直接展示 ----
        "outer_attempts": cfg.max_outer_retries,       # 总共会尝试几次
        "outer_finished": 0,                           # 实际跑了几次
        "pass_history": [],                            # 每次 pass 的简要结果
        "last_error_text": None,                       # 最后一次拿到的错误提示原文
        "note": "",                                    # 给前端显示的简短提示
    }

    result["stage"] = "locate_window"
    win = _get_wechat_window()
    if win is None:
        result["error"] = "找不到微信窗口，请确认 PC 微信已登录"
        result["note"] = result["error"]
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
        log.warning("没找到玩家二维码按钮，外层重试也没法继续，直接放弃")
        result["error"] = "找不到玩家二维码按钮，请先让消息流里出现该按钮"
        result["note"] = result["error"]
        return result

    # ========== 外层重试循环 ==========
    for pass_index in range(1, cfg.max_outer_retries + 1):
        result["stage"] = f"outer_pass_{pass_index}_click_button"
        _click_control(button)
        time.sleep(0.8)

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

        # 还没成功 -> 准备下一次外层重试
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

    # ========== 全部 pass 都失败 ==========
    result["stage"] = "all_retries_exhausted"
    final_reason = result["last_error_text"] or "未知原因"
    result["error"] = (
        f"已连续尝试 {cfg.max_outer_retries} 次仍未拿到二维码卡片，"
        f"最后一次返回：{final_reason}。"
        f"请检查公众号是否真的返回了卡片，或稍后再点一次「玩家二维码」。"
    )
    result["note"] = (
        f"⚠️ 连续 {cfg.max_outer_retries} 次重试仍失败。"
        f"最后一次：{final_reason}。请先手动打开微信确认公众号有没有真的发卡片，"
        f"或检查是否有网络 / 公众号服务故障。"
    )

    # 不管怎样，留一张当前屏幕截图方便用户肉眼看看到底显示了什么
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
    safe = {}
    for k, v in r.items():
        if isinstance(v, str) and len(v) > 120:
            safe[k] = v[:120] + "..."
        else:
            safe[k] = v
    print(json.dumps(safe, ensure_ascii=False, indent=2))
