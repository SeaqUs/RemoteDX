"""app.py - RemoteDX Flask HTTP 服务"""
import logging, threading, time
from typing import Dict, Optional
from flask import Flask, jsonify, request, Response
from qr_fetcher import fetch_qr_code, FetchConfig
from qr_utils import generate_qr_base64
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s | %(message)s")
log = logging.getLogger("app")
_state_lock = threading.Lock()
STATE = {
    "running": False, "started_at": None, "finished_at": None,
    "ok": None, "stage": None, "link": None,
    "qr_png_base64": None, "raw_png_base64": None, "error": None,
}
def _run_in_background(cfg: FetchConfig) -> None:
    with _state_lock:
        STATE["running"] = True
        for k in list(STATE.keys()):
            STATE[k] = None
        STATE["started_at"] = time.time()
    try:
        result = fetch_qr_code(cfg)
        link = result.get("link")
        qr_png_b64 = generate_qr_base64(link, box_size=12, border=4) if link else None
        with _state_lock:
            STATE["running"] = False
            STATE["finished_at"] = time.time()
            STATE["ok"] = result.get("ok", False)
            STATE["stage"] = result.get("stage")
            STATE["link"] = link
            STATE["qr_png_base64"] = qr_png_b64
            STATE["raw_png_base64"] = result.get("raw_png_base64")
            STATE["error"] = result.get("error")
    except Exception as e:
        log.exception("fetch_qr_code 抛异常了")
        with _state_lock:
            STATE["running"] = False
            STATE["finished_at"] = time.time()
            STATE["ok"] = False
            STATE["error"] = f"{type(e).__name__}: {e}"
def _parse_region(raw) -> Optional[tuple]:
    if not raw:
        return None
    try:
        parts = [int(x) for x in str(raw).split(",")]
        if len(parts) == 4:
            return tuple(parts)
    except Exception:
        pass
    return None
@app.route("/api/get_qr", methods=["POST"])
def api_get_qr():
    with _state_lock:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "已有任务在跑"}), 409
    data = request.get_json(silent=True) or {}
    cfg = FetchConfig(
        wait_after_button_click=float(data.get("wait_after_button_click", 2.0)),
        wait_after_card_click=float(data.get("wait_after_card_click", 3.0)),
        wait_before_scan=float(data.get("wait_before_scan", 2.5)),
        qr_region=_parse_region(data.get("qr_region")),
    )
    threading.Thread(target=_run_in_background, args=(cfg,), daemon=True).start()
    log.info(f"已触发任务 cfg={cfg}")
    return jsonify({"ok": True, "message": "任务已触发，请轮询 /api/qr_result"})
@app.route("/api/qr_result", methods=["GET"])
def api_qr_result():
    with _state_lock:
        return jsonify({k: STATE[k] for k in STATE})
@app.route("/api/reset", methods=["POST"])
def api_reset():
    with _state_lock:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "有任务在跑，无法重置"}), 409
        for k in list(STATE.keys()):
            STATE[k] = None
        STATE["running"] = False
    return jsonify({"ok": True})
@app.route("/api/current_link", methods=["GET"])
def api_current_link():
    with _state_lock:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "任务还在进行中"}), 404
        if not STATE["link"]:
            return jsonify({"ok": False, "error": "还没拿到链接"}), 404
        return jsonify({"ok": True, "link": STATE["link"]})
INDEX_HTML = (
    "<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>RemoteDX - 远程扫码</title>"
    "<style>"
    "body{font-family:-apple-system,Segoe UI,Microsoft YaHei,Arial,sans-serif;"
    "background:#0f1115;color:#e5e7eb;max-width:720px;margin:32px auto;padding:0 16px;}"
    "h1{font-size:22px;margin:0 0 8px;color:#fff;}"
    ".sub{color:#9ca3af;margin-bottom:20px;}"
    "button{background:#10b981;color:#fff;border:0;padding:12px 24px;"
    "border-radius:10px;font-size:15px;cursor:pointer;}"
    "button:disabled{background:#475569;cursor:not-allowed;}"
    ".card{background:#181b22;border-radius:12px;padding:16px;margin-top:16px;"
    "border:1px solid #23262f;}"
    ".muted{color:#94a3b8;font-size:12px;word-break:break-all;}"
    ".ok{color:#34d399;}.bad{color:#f87171;}"
    "canvas{display:block;background:#fff;border-radius:8px;margin-top:10px;}"
    ".row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}"
    "input[type=text]{flex:1;padding:10px 12px;border-radius:8px;border:1px solid #2d3139;"
    "background:#121418;color:#e5e7eb;font-size:14px;min-width:240px;}"
    "label{font-size:13px;color:#94a3b8;}"
    "a{color:#60a5fa;word-break:break-all;}"
    "</style></head><body>"
    "<h1>RemoteDX - 远程扫码登录</h1>"
    "<div class=\"sub\">微信窗口 -> 玩家二维码按钮 -> 链接卡片 -> 二维码图 -> 手机扫码</div>"
    "<div class=\"card\">"
    "<div class=\"row\">"
    "<button id=\"go\">触发一次扫码</button>"
    "<button id=\"reset\">清空</button>"
    "</div>"
    "<div class=\"row\" style=\"margin-top:10px;\">"
    "<label style=\"display:flex;align-items:center;gap:6px;\">"
    "二维码屏幕区域 (left,top,right,bottom, 留空=全屏)"
    "<input id=\"region\" type=\"text\" placeholder=\"例如: 1300,400,1700,800\" value=\"\">"
    "</label></div></div>"
    "<div class=\"card\" id=\"status\"><div id=\"stage\">等待触发...</div>"
    "<div id=\"err\" class=\"muted\" style=\"margin-top:6px;\"></div></div>"
    "<div class=\"card\" id=\"qrbox\" style=\"display:none;\">"
    "<div class=\"ok\">已识别到登录链接:</div>"
    "<a id=\"link\" target=\"_blank\" rel=\"noopener\"></a>"
    "<canvas id=\"qrcanvas\"></canvas>"
    "<div class=\"muted\" style=\"margin-top:6px;\">"
    "用手机微信扫这张码 / 点上方链接</div></div>"
    "<script>"
    "const $=id=>document.getElementById(id);"
    "async function go(){go.disabled=true;reset.disabled=true;"
    "stage.textContent='已触发,后台执行中...';err.textContent='';"
    "qrbox.style.display='none';"
    "try{const body={};const r=region.value.trim();if(r)body.qr_region=r;"
    "const resp=await fetch('/api/get_qr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});"
    "const j=await resp.json();if(!j.ok){stage.textContent='触发失败';err.textContent=j.error||'';}else poll();}"
    "catch(e){stage.textContent='请求失败';err.textContent=String(e);"
    "go.disabled=false;reset.disabled=false;}}"
    "async function reset(){await fetch('/api/reset',{method:'POST'});"
    "stage.textContent='等待触发...';err.textContent='';qrbox.style.display='none';}"
    "async function poll(){let last=null;"
    "while(true){const r=await fetch('/api/qr_result').then(x=>x.json());"
    "if(r.stage&&r.stage!==last){last=r.stage;"
    "const map={'locate_window':'寻找微信窗口...','activate':'激活微信窗口...',"
    "'find_button_try_1':'寻找玩家二维码按钮...','find_button_try_2':'重试找按钮...','find_button_try_3':'再次重试...',"
    "'click_button':'点击玩家二维码按钮...','wait_card':'等待链接卡片出现...','click_card':'点击链接卡片...',"
    "'ocr_screen':'屏幕识别二维码...','done':'识别完成'};"
    "stage.textContent=map[r.stage]||r.stage;}"
    "if(!r.running){if(r.ok){stage.textContent='成功';"
    "link.href=r.link;link.textContent=r.link;"
    "if(r.qr_png_base64){const img=new Image();"
    "img.onload=()=>{const c=qrcanvas;"
    "c.width=img.width;c.height=img.height;"
    "c.getContext('2d').drawImage(img,0,0);};"
    "img.src='data:image/png;base64,'+r.qr_png_base64;"
    "qrbox.style.display='block';}}"
    "else{stage.innerHTML='失败 - <span class=\"bad\">'+(r.error||'未知')+'</span>';"
    "if(r.raw_png_base64){const img=new Image();"
    "img.onload=()=>{const c=document.createElement('canvas');"
    "c.width=img.width;c.height=img.height;"
    "c.getContext('2d').drawImage(img,0,0);"
    "status.appendChild(c);};"
    "img.src='data:image/png;base64,'+r.raw_png_base64;}}"
    "go.disabled=false;reset.disabled=false;return;}"
    "await new Promise(x=>setTimeout(x,1500));}}"
    "go.onclick=go;reset.onclick=reset;"
    "</script></body></html>"
)
@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html")
if __name__ == "__main__":
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    log.info(f"RemoteDX 监听 {host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)