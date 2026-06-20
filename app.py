"""app.py - RemoteDX Flask HTTP 服务"""
import logging, threading, time, traceback
from typing import Dict, Optional
from flask import Flask, jsonify, request, Response, stream_with_context
from qr_fetcher import fetch_qr_code, FetchConfig

# ====== Flask logger 配置 ======
_log_fmt = "[%(asctime)s] %(levelname)s %(name)s | %(message)s"
logging.basicConfig(level=logging.DEBUG, format=_log_fmt)
log = logging.getLogger("app")
# 额外路由访问日志
access = logging.getLogger("app.access")

app = Flask(__name__)

_STATE_LOCK = threading.Lock()
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
    "outer_attempts": None,
    "outer_finished": None,
    "pass_history": None,
    "last_error_text": None,
    "note": None,
}


def _run_in_background(cfg: FetchConfig) -> None:
    """后台执行一次扫码任务，把 fetch_qr_code 返回的所有字段都塞回 STATE。"""
    tid = threading.current_thread().ident
    log.info(f"[BG] 后台线程启动 tid={tid} cfg={cfg}")
    with _STATE_LOCK:
        STATE["running"] = True
        for k in list(STATE.keys()):
            STATE[k] = None
        STATE["started_at"] = time.time()
    try:
        log.info("[BG] 即将调用 fetch_qr_code() ...")
        result = fetch_qr_code(cfg)
        link = result.get("link")
        qr_png_b64 = None
        if link:
            try:
                from qr_utils import generate_qr_base64
                qr_png_b64 = generate_qr_base64(link, box_size=12, border=4)
            except Exception as e:
                log.warning(f"[BG] 生成二维码图失败：{e}")
        log.info(f"[BG] fetch_qr_code 返回 ok={result.get('ok')} link={str(link)[:80] if link else 'None'}")
        with _STATE_LOCK:
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
            if "log_file" in result:
                STATE["log_file"] = result["log_file"]
        log.info(f"[BG] 任务结束 ok={result.get('ok')} stage={result.get('stage')}")
    except Exception as e:
        log.error(f"[BG] fetch_qr_code 抛异常 {type(e).__name__}: {e}")
        traceback.print_exc()
        with _STATE_LOCK:
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


@app.before_request
def _before():
    """每个请求都打日志。"""
    ua = (request.headers.get('User-Agent', '') or '')[:50]
    ct = request.headers.get('Content-Type', '') or ''
    access.info(f"[{request.method}] {request.path} from={request.remote_addr} ua={ua} ct={ct}")


@app.route("/api/get_qr", methods=["POST", "OPTIONS"])
def api_get_qr():
    """触发一次扫码。"""
    if request.method == "OPTIONS":
        return Response("", status=204, headers={"Access-Control-Allow-Origin": "*",
                                                 "Access-Control-Allow-Headers": "*",
                                                 "Access-Control-Allow-Methods": "POST, OPTIONS"})
    access.info(f"▶️ /api/get_qr body={request.get_json(silent=True)!r}")
    with _STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "已有任务在跑"}), 409
    try:
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
        log.info(f"📌 启动后台线程 cfg={cfg}")
        threading.Thread(target=_run_in_background, args=(cfg,), daemon=True).start()
        return jsonify({"ok": True, "message": "任务已触发，请轮询 /api/qr_result"})
    except Exception as e:
        log.exception(f"/api/get_qr 异常 {e}")
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@app.route("/api/qr_result", methods=["GET"])
def api_qr_result():
    with _STATE_LOCK:
        return jsonify({k: STATE[k] for k in STATE})


@app.route("/api/reset", methods=["POST", "OPTIONS"])
def api_reset():
    if request.method == "OPTIONS":
        return Response("", status=204, headers={"Access-Control-Allow-Origin": "*",
                                                 "Access-Control-Allow-Headers": "*",
                                                 "Access-Control-Allow-Methods": "POST, OPTIONS"})
    with _STATE_LOCK:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "有任务在跑，无法重置"}), 409
        for k in list(STATE.keys()):
            STATE[k] = None
        STATE["running"] = False
    return jsonify({"ok": True})


INDEX_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RemoteDX - 远程扫码</title>
<style>
body{font-family:-apple-system,Segoe UI,Microsoft YaHei,Arial,sans-serif;
background:#0f1115;color:#e5e7eb;max-width:780px;margin:32px auto;padding:0 16px;}
h1{font-size:22px;margin:0 0 4px;color:#fff;}
.sub{color:#9ca3af;margin-bottom:16px;font-size:13px;}
button{background:#10b981;color:#fff;border:0;padding:12px 28px;
border-radius:10px;font-size:15px;cursor:pointer;min-width:120px;}
button:disabled{background:#475569;cursor:not-allowed;}
button.danger{background:#ef4444;}
button.info{background:#3b82f6;}
.card{background:#181b22;border-radius:12px;padding:16px;margin-top:14px;
border:1px solid #23262f;}
.muted{color:#94a3b8;font-size:12px;word-break:break-all;white-space:pre-wrap;max-height:220px;overflow:auto;}
.ok{color:#34d399;}.bad{color:#f87171;}.warn{color:#fbbf24;}.info{color:#60a5fa;}
canvas{display:block;background:#fff;border-radius:8px;margin-top:10px;max-width:100%;}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;}
input[type=text],input[type=number]{padding:10px 12px;border-radius:8px;
border:1px solid #2d3139;background:#121418;color:#e5e7eb;
font-size:14px;min-width:140px;}
label{font-size:13px;color:#94a3b8;display:flex;align-items:center;gap:6px;}
a{color:#60a5fa;word-break:break-all;}
table{font-size:12px;color:#cbd5e1;border-collapse:collapse;margin-top:10px;}
td,th{border-bottom:1px solid #23262f;padding:4px 8px;}
th{color:#94a3b8;text-align:left;font-weight:normal;}
code{background:#0b0d12;padding:2px 6px;border-radius:4px;color:#cbd5e1;}
#console{font-family:Consolas,Menlo,monospace;font-size:11px;line-height:1.35;
background:#0b0d12;border:1px solid #23262f;border-radius:8px;padding:10px;
max-height:240px;overflow:auto;white-space:pre-wrap;margin-top:10px;}
</style>
</head>
<body>
<h1>RemoteDX - 远程扫码登录</h1>
<div class="sub">微信窗口 → 玩家二维码按钮 → 链接卡片 → 二维码图 → 手机扫码</div>

<div class="card">
  <div class="row">
    <button id="go">触发扫码 (POST /api/get_qr)</button>
    <button id="reset" class="info">清空状态</button>
  </div>
  <div class="row" style="margin-top:10px;">
    <label>区域 <input id="region" type="text" placeholder="留空=全屏"></label>
    <label>外层重试 <input id="retries" type="number" min="1" max="20" value="5" style="width:70px;"></label>
  </div>
</div>

<div class="card" id="status">
  <div id="stage" class="info">等待触发…</div>
  <div id="err" class="muted" style="margin-top:6px;"></div>
  <div id="passes"></div>
  <div class="row" style="margin-top:8px;">
    <span class="muted">后端日志文件：</span>
    <code id="logfile" style="color:#cbd5e1;">(等待任务启动…)</code>
  </div>
</div>

<div class="card" id="qrbox" style="display:none;">
  <div class="ok">✅ 已识别到登录链接：</div>
  <a id="link" target="_blank" rel="noopener"></a>
  <canvas id="qrcanvas"></canvas>
  <div class="muted" style="margin-top:6px;">用手机微信扫这张码 / 点上方链接即可</div>
</div>

<div class="card">
  <div class="info" style="font-size:13px;margin-bottom:4px;">📟 控制台</div>
  <div id="console"></div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const go = $("go"), reset = $("reset");
const stageEl = $("stage"), errEl = $("err"), passesEl = $("passes");
const linkEl = $("link"), qrcanvas = $("qrcanvas"), qrbox = $("qrbox");
const logfileEl = $("logfile"), consoleEl = $("console");

function log(msg){
  const t=new Date().toLocaleTimeString();
  console.log(msg);
  consoleEl.textContent += "["+t+"] "+msg+"\\n";
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

log("页面已加载 ✓ 等待用户点击触发按钮");

const STAGE_MAP={
  'locate_window':'寻找微信窗口...',
  'activate':'激活微信窗口...',
  'open_biz_menu':'打开底部菜单...',
  'find_button_try_1':'寻找玩家二维码按钮...',
  'find_button_try_2':'重试找按钮...',
  'find_button_try_3':'再次重试...',
  'done':'✅ 完成',
  'all_retries_exhausted':'❌ 全部重试耗尽',
};
function stageText(s){
  if(!s)return '';
  if(s in STAGE_MAP) return STAGE_MAP[s];
  if(s.startsWith('outer_pass_')){
    const m=s.match(/outer_pass_(\\d+)_/);
    if(m) return "第 "+m[1]+" 次外层尝试...";
  }
  return s;
}
function renderPasses(passes,total,finished){
  if(!passes||!passes.length){passesEl.innerHTML='';return;}
  let html='<table><tr><th>#</th><th>卡片</th><th>点击</th><th>结果</th></tr>';
  for(const p of passes){
    const col=p.ok?'ok':(p.card_found?'warn':'bad');
    const card=p.card_found?'✓':'✗';
    const click=p.card_clicked?'✓':'—';
    const ok=p.ok?'拿到链接':(p.error?('失败：'+p.error.substring(0,50)):'失败');
    html += `<tr><td>#${p.n}</td><td class="${col}">${card}</td><td>${click}</td><td class="${col}">${ok}</td></tr>`;
  }
  html += '</table>';
  if(finished!=null&&total!=null&&!passes[passes.length-1].ok){
    html += `<div class="warn" style="margin-top:6px;">已跑 ${finished}/${total} 次，均未成功</div>`;
  }
  passesEl.innerHTML=html;
}

async function doGo(){
  log("▶️ 点击触发按钮，构造请求…");
  go.disabled=true; reset.disabled=true;
  stageEl.textContent="👉 请求前端构造中…";
  errEl.textContent=""; passesEl.innerHTML=""; qrbox.style.display="none";

  const body = {};
  const r = $("region").value.trim();
  if(r){ body.qr_region = r; log("  region=" + r); }
  const n = parseInt($("retries").value,10);
  if(n>0){ body.max_outer_retries = n; log("  max_outer_retries=" + n); }

  const url = "/api/get_qr";
  stageEl.textContent="📤 POST " + url + " ...";
  log("📤 fetch " + url + " body=" + JSON.stringify(body));

  let resp, j;
  try{
    resp = await fetch(url,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    log("  响应 HTTP " + resp.status + " type=" + resp.headers.get('content-type'));
    const rawText = await resp.text();
    log("  响应体(前400): " + rawText.substring(0,400));
    try { j = JSON.parse(rawText); } catch(e){ j = {ok:false, error:"后端返回非JSON: "+rawText.substring(0,200)}; }
  } catch(e){
    log("❌ fetch 异常：" + (e && e.message ? e.message : String(e)));
    stageEl.innerHTML = '<span class="bad">前端请求失败</span>';
    errEl.textContent = String(e);
    go.disabled=false; reset.disabled=false; return;
  }

  if(!j.ok){
    log("❌ 后端返回 ok=false: " + (j.error || ''));
    stageEl.innerHTML = '<span class="bad">触发失败</span>';
    errEl.textContent = (j.error || '后端没有返回 ok=true') + "\\n原始: " + JSON.stringify(j);
    go.disabled=false; reset.disabled=false; return;
  }

  log("✅ 后端已接受任务 (后台线程已启动)，开始轮询 /api/qr_result ...");
  stageEl.textContent="✅ 已触发，后台执行中...";
  poll();
}

async function doReset(){
  log("🗑️ 点击清空");
  try { await fetch('/api/reset',{method:'POST'}); } catch(e){}
  stageEl.textContent='等待触发…'; errEl.textContent=''; passesEl.innerHTML='';
  qrbox.style.display='none'; logfileEl.textContent='(等待任务启动…)';
}

async function poll(){
  let last = null, polls = 0;
  while(true){
    polls++;
    let r;
    try{
      const resp = await fetch('/api/qr_result');
      r = await resp.json();
    } catch(e){
      log("poll fetch 异常：" + String(e));
      await new Promise(x=>setTimeout(x,1500));
      continue;
    }
    if(r.log_file){ logfileEl.textContent = r.log_file; }
    if(r.stage && r.stage !== last){
      last = r.stage;
      log("  poll#" + polls + " stage=" + r.stage + " => " + stageText(r.stage));
      stageEl.textContent = stageText(r.stage);
    }
    if(r.pass_history){ renderPasses(r.pass_history, r.outer_attempts, r.outer_finished); }
    if(!r.running){
      log("🏁 running=false 最终 ok="+r.ok+" stage="+r.stage);
      if(r.ok){
        stageEl.innerHTML='<span class="ok">✅ 成功拿到链接</span>';
        linkEl.href=r.link; linkEl.textContent=r.link;
        if(r.qr_png_base64){
          const img = new Image();
          img.onload = ()=>{
            qrcanvas.width = img.width; qrcanvas.height = img.height;
            qrcanvas.getContext('2d').drawImage(img,0,0);
          };
          img.src='data:image/png;base64,'+r.qr_png_base64;
          qrbox.style.display='block';
        }
      } else {
        stageEl.innerHTML='<span class="bad">❌ 失败</span>';
        let msg = "";
        if(r.note){ msg += r.note + "\\n"; }
        msg += "error: " + (r.error || '未知') + "\\n";
        if(r.pass_history){ msg += "pass_history:\\n" + JSON.stringify(r.pass_history,null,2) + "\\n"; }
        if(r.log_file){ msg += "log_file: " + r.log_file + "\\n"; }
        errEl.textContent = msg;
        if(r.raw_png_base64){
          log("  附带失败截图 raw_png_base64 len=" + r.raw_png_base64.length);
          const img = new Image();
          img.onload = () => {
            const c = document.createElement('canvas');
            c.style.cssText="width:100%;border-radius:8px;margin-top:8px;";
            c.width = img.naturalWidth; c.height = img.naturalHeight;
            c.getContext('2d').drawImage(img,0,0);
            errEl.appendChild(c);
          };
          img.src = 'data:image/png;base64,' + r.raw_png_base64;
        }
      }
      go.disabled=false; reset.disabled=false; return;
    }
    await new Promise(x=>setTimeout(x,1500));
  }
}

go.onclick = doGo;
reset.onclick = doReset;
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html")


if __name__ == "__main__":
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    log.info(f"RemoteDX 监听 {host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
