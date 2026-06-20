"""
qr_utils.py - 二维码生成工具模块
职责：把拿到的二维码链接转成 PNG 图片（Base64 编码），
      以便直接嵌进 HTML data:image/png;base64,... 里返回给前端。
"""
import base64
import io
from typing import Optional
try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L
except ImportError:  # pragma: no cover
    qrcode = None
    ERROR_CORRECT_L = 1
def generate_qr_base64(data: str, box_size: int = 10, border: int = 4) -> Optional[str]:
    """把任意字符串（通常是一个 URL）编码成 PNG 二维码并返回 Base64 字符串。
    Args:
        data: 要编码进二维码的内容（一般是扫码登录链接）。
        box_size: 每个二维码小方块的像素尺寸，越大图片越大。
        border: 白边框占多少个小方块。
    Returns:
        Base64 字符串（不带 data:image 前缀，调用方自己拼）。
        如果 qrcode 库没装，返回 None。
    """
    if qrcode is None:
        return None
    qr = qrcode.QRCode(
        version=None,                        # 自动选择最合适的版本 (1~40)
        error_correction=ERROR_CORRECT_L,    # L 级容错 7%，适合纯链接
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")
def generate_qr_png_bytes(data: str, box_size: int = 10, border: int = 4) -> Optional[bytes]:
    """同上，但直接返回 PNG 文件字节流，方便 Flask send_file 使用。"""
    if qrcode is None:
        return None
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_L,
                       box_size=box_size, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()