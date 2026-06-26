"""app.py - RemoteDX Flask HTTP 服务"""
import logging, sys, threading, time
from typing import Dict, Optional
from flask import Flask, jsonify, request, Response
from qr_fetcher import fetch_qr_code, FetchConfig
from qr_utils import generate_qr_base64

app = Flask(__name__)

# 给 console handler 加一层转码包装，避免 PowerShell gbk 输出特殊符号时抛 UnicodeEncodeError。
# 第一次直接输出；失败时把格式化后的消息按目标编码过滤掉无法显示的字符再写。
class _SafeStreamHandler(logging.StreamHandler):
    """安全控制台日志 Handler：先直接输出；遇到 UnicodeEncodeError 时按目标编码过滤后再写。"""
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

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s %(name)s | %(message)s",
                    handlers=[_SafeStreamHandler(sys.stdout)])
log = logging.getLogger("app")

_state_lock = threading.Lock()
STATE = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "stage": None,
    "link": None,
    "qr_png_base64": None,
    "raw_png_base64": None,
    "error": None,
    # ---- debug 字段 ----
    "outer_attempts": None,
    "outer_finished": None,
    "pass_history": None,
    "last_error_text": None,
    "note": None,
}


def _run_in_background(cfg: FetchConfig) -> None:
    """后台执行一次扫码任务，把 fetch_qr_code 返回的所有字段都塞回 STATE。"""
    with _state_lock:
        # 先清空所有字段，再把 running 置为 True，否则 loop 会把 running 覆盖成 None
        for k in list(STATE.keys()):
            STATE[k] = None
        STATE["running"] = True
        STATE["started_at"] = time.time()
    try:
        result = fetch_qr_code(cfg)
        link = result.get("link")
        log.info(f"[BG] fetch_qr_code 完成 link={bool(link)} ok={result.get('ok')} stage={result.get('stage')}")
        if link:
            try:
                qr_png_b64 = generate_qr_base64(link, box_size=12, border=4)
                log.info(f"[BG] generate_qr_base64 返回长度={len(qr_png_b64) if qr_png_b64 else 0}")
            except Exception as qe:
                log.exception("[BG] generate_qr_base64 失败")
                qr_png_b64 = None
        else:
            qr_png_b64 = None
        with _state_lock:
            STATE["running"] = False
            STATE["finished_at"] = time.time()
            STATE["ok"] = result.get("ok", False)
            STATE["stage"] = result.get("stage")
            STATE["link"] = link
            STATE["qr_png_base64"] = qr_png_b64
            STATE["raw_png_base64"] = result.get("raw_png_base64")
            STATE["error"] = result.get("error")
            STATE["outer_attempts"] = result.get("outer_attempts")
            STATE["outer_finished"] = result.get("outer_finished")
            STATE["pass_history"] = result.get("pass_history")
            STATE["last_error_text"] = result.get("last_error_text")
            STATE["note"] = result.get("note")
    except Exception as e:
        log.exception("fetch_qr_code 抛异常了")
        with _state_lock:
            STATE["running"] = False
            STATE["finished_at"] = time.time()
            STATE["ok"] = False
            STATE["error"] = f"{type(e).__name__}: {e}"
            STATE["note"] = f"执行异常：{type(e).__name__}: {e}"


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
    """触发一次扫码。JSON 参数可选：
        wait_after_button_click, wait_after_card_click, wait_before_scan,
        qr_region (格式：l,t,r,b),
        max_outer_retries (外层重试次数，默认 5),
        outer_retry_jitter_min / outer_retry_jitter_max (默认 5 / 8 秒)
    """
    with _state_lock:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "已有任务在跑"}), 409
    data = request.get_json(silent=True) or {}
    j_min = float(data.get("outer_retry_jitter_min", 5.0))
    j_max = float(data.get("outer_retry_jitter_max", 8.0))
    if j_max < j_min:
        j_max = j_min
    cfg = FetchConfig(
        wait_after_button_click=float(data.get("wait_after_button_click", 2.0)),
        wait_after_card_click=float(data.get("wait_after_card_click", 3.0)),
        wait_before_scan=float(data.get("wait_before_scan", 2.5)),
        qr_region=_parse_region(data.get("qr_region")),
        max_outer_retries=int(data.get("max_outer_retries", 5)),
        outer_retry_jitter=(j_min, j_max),
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
    "background:#0f1115;color:#e5e7eb;max-width:760px;margin:32px auto;padding:0 16px;}"
    "h1{font-size:22px;margin:0 0 8px;color:#fff;}"
    ".sub{color:#9ca3af;margin-bottom:20px;}"
    "button{background:#10b981;color:#fff;border:0;padding:12px 24px;"
    "border-radius:10px;font-size:15px;cursor:pointer;}"
    "button:disabled{background:#475569;cursor:not-allowed;}"
    ".card{background:#181b22;border-radius:12px;padding:16px;margin-top:16px;"
    "border:1px solid #23262f;}"
    ".muted{color:#94a3b8;font-size:12px;word-break:break-all;}"
    ".ok{color:#34d399;}.bad{color:#f87171;}.warn{color:#fbbf24;}"
    "canvas{display:block;background:#fff;border-radius:8px;margin-top:10px;}"
    ".row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}"
    "input[type=text],input[type=number]{padding:10px 12px;border-radius:8px;"
    "border:1px solid #2d3139;background:#121418;color:#e5e7eb;"
    "font-size:14px;min-width:120px;}"
    "label{font-size:13px;color:#94a3b8;display:flex;align-items:center;gap:6px;}"
    "a{color:#60a5fa;word-break:break-all;}"
    "table{font-size:12px;color:#cbd5e1;border-collapse:collapse;margin-top:10px;}"
    "td,th{border-bottom:1px solid #23262f;padding:4px 8px;}"
    "th{color:#94a3b8;text-align:left;font-weight:normal;}"
    "code{background:#0b0d12;padding:2px 6px;border-radius:4px;color:#cbd5e1;}"
    "</style></head><body>"
    "<h1>RemoteDX - 远程扫码登录</h1>"
    "<div class=\"sub\">微信窗口 -> 玩家二维码按钮 -> 链接卡片 -> 二维码图 -> 手机扫码"
    " · 最多 5 次外层重试 ·</div>"
    "<div class=\"card\">"
    "<div class=\"row\">"
    "<button id=\"go\">触发一次扫码</button>"
    "<button id=\"reset\">清空</button>"
    "</div>"
    "<div class=\"row\" style=\"margin-top:10px;\">"
    "<label>"
    "屏幕区域 (l,t,r,b)"
    "<input id=\"region\" type=\"text\" placeholder=\"例如 1300,400,1700,800\" value=\"\">"
    "</label>"
    "<label>"
    "外层重试"
    "<input id=\"retries\" type=\"number\" min=\"1\" max=\"20\" value=\"5\" style=\"width:70px;\">"
    "</label>"
    "</div></div>"
    "<div class=\"card\" id=\"status\"><div id=\"stage\">等待触发...</div>"
    "<div id=\"err\" class=\"muted\" style=\"margin-top:6px;\"></div>"
    "<div id=\"passes\" style=\"margin-top:8px;\"></div></div>"
    "<div class=\"card\" id=\"qrbox\" style=\"display:none;\">"
    "<div class=\"ok\">已识别到登录链接:</div>"
    "<a id=\"link\" target=\"_blank\" rel=\"noopener\"></a>"
    "<canvas id=\"qrcanvas\"></canvas>"
    "<div class=\"muted\" style=\"margin-top:6px;\">"
    "用手机微信扫这张码 / 点上方链接</div></div>"
    "<script>"
    "const $=id=>document.getElementById(id);"
    "const goBtn=$('go'),resetBtn=$('reset'),stage=$('stage'),err=$('err'),"
    "passes=$('passes'),qrbox=$('qrbox'),link=$('link'),qrcanvas=$('qrcanvas'),"
    "region=$('region'),retries=$('retries');"
    "const STAGE_MAP={"
    "'locate_window':'寻找微信窗口...',"
    "'activate':'激活微信窗口...',"
    "'find_button_try_1':'寻找玩家二维码按钮...',"
    "'find_button_try_2':'重试找按钮...',"
    "'find_button_try_3':'再次重试...',"
    "'outer_pass_':'点玩家二维码按钮并拉卡片...',"
    "'wait_card':'等待链接卡片出现...',"
    "'click_card':'点击链接卡片...',"
    "'ocr_screen':'屏幕识别二维码...',"
    "'done':'识别完成',"
    "'all_retries_exhausted':'全部重试耗尽',"
    "};"
    "function stageText(s){if(!s)return'';"
    "if(s.startsWith('outer_pass_')){"
    "const m=s.match(/outer_pass_(\\d+)_/);"
    "if(m){return '第 '+m[1]+' 次外层尝试...';}}"
    "if(s in STAGE_MAP) return STAGE_MAP[s];"
    "for(const k in STAGE_MAP) if(s.startsWith(k)) return STAGE_MAP[k];"
    "return s;}"
    "function renderPasses(passes,total,finished){if(!passes||!passes.length){passes.innerHTML='';return;}"
    "let html='<table><tr><th>#</th><th>卡片</th><th>点击</th><th>结果</th></tr>';"
    "for(const p of passes){"
    "const col=p.ok?'ok':(p.card_found?'warn':'bad');"
    "const card=p.card_found?'✓':'✗';"
    "const click=p.card_clicked?'✓':'—';"
    "const ok=p.ok?'拿到链接':(p.error?('失败：'+p.error.substring(0,40)):'失败');"
    "html+=`<tr><td>#${p.n}</td><td class=\"${col}\">${card}</td><td>${click}</td><td class=\"${col}\">${ok}</td></tr>`;}"
    "html+='</table>';"
    "if(finished!=null&&total!=null&&!passes[passes.length-1].ok){"
    "html+=`<div class=\"warn\" style=\"margin-top:6px;\">已跑 ${finished}/${total} 次，均未成功</div>`;}"
    "passes.innerHTML=html;}"
    "async function doGo(){goBtn.disabled=true;resetBtn.disabled=true;"
    "stage.textContent='已触发，后台执行中...';err.textContent='';passes.innerHTML='';"
    "qrbox.style.display='none';"
    "try{const body={};const r=region.value.trim();if(r)body.qr_region=r;"
    "const n=parseInt(retries.value,10);if(n>0)body.max_outer_retries=n;"
    "const resp=await fetch('/api/get_qr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});"
    "const j=await resp.json();if(!j.ok){stage.textContent='触发失败';err.textContent=j.error||'';doReset();}else poll();}"
    "catch(e){stage.textContent='请求失败';err.textContent=String(e);"
    "goBtn.disabled=false;resetBtn.disabled=false;}}"
    "async function doReset(){await fetch('/api/reset',{method:'POST'});"
    "stage.textContent='等待触发...';err.textContent='';passes.innerHTML='';"
    "qrbox.style.display='none';goBtn.disabled=false;resetBtn.disabled=false;}"
    "async function poll(){let last=null;"
    "while(true){const r=await fetch('/api/qr_result').then(x=>x.json());"
    "if(r.stage&&r.stage!==last){last=r.stage;"
    "stage.textContent=stageText(r.stage);}"
    "if(r.pass_history) renderPasses(r.pass_history,r.outer_attempts,r.outer_finished);"
    "if(!r.running){"
    "if(r.ok){stage.textContent='成功';"
    "link.href=r.link;link.textContent=r.link;"
    "if(r.qr_png_base64){const img=new Image();"
    "img.onload=()=>{const c=qrcanvas;c.width=img.width;c.height=img.height;"
    "c.getContext('2d').drawImage(img,0,0);};"
    "img.src='data:image/png;base64,'+r.qr_png_base64;"
    "qrbox.style.display='block';}"
    "}else{"
    "stage.innerHTML='<span class=\"bad\">失败</span>';"
    "err.innerHTML=(r.note?('<div class=\"warn\">'+r.note+'</div>'):'')+"
    "('原因：'+(r.error||'未知'));"
    "if(r.raw_png_base64){const img=new Image();"
    "img.onload=()=>{const c=document.createElement('canvas');"
    "c.width=img.width;c.height=img.height;"
    "c.getContext('2d').drawImage(img,0,0);"
    "status.appendChild(c);};"
    "img.src='data:image/png;base64,'+r.raw_png_base64;}}"
    "goBtn.disabled=false;resetBtn.disabled=false;return;}"
    "await new Promise(x=>setTimeout(x,1500));}}"
    "goBtn.onclick=doGo;resetBtn.onclick=doReset;"
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
