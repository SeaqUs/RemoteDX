"""qr_utils.py - 二维码生成工具"""
import base64, io
from typing import Optional
try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L
except ImportError:
    qrcode = None
    ERROR_CORRECT_L = 1
def generate_qr_base64(data, box_size=10, border=4) -> Optional[str]:
    if qrcode is None:
        return None
    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_L, box_size=box_size, border=border)
    qr.add_data(data); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")