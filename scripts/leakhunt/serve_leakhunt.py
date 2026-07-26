
import os, sys, re, io
os.environ["HART_OS_MODE"] = "1"
sys.path.insert(0, r"C:\Users\sathi\PycharmProjects\HARTOS")
import integrations.agent_engine.liquid_ui_service as m
# reproduce the NODE's state: webkit-cairo rung -> body.gpu-hardware (heavy effects LIVE)
m.read_shell_render_mode = lambda: "webkit-cairo"
m.read_gpu_render_mode = lambda: "hardware"
from integrations.agent_engine.theme_service import ThemeService
ThemeService.apply_theme("aura")
svc = m.LiquidUIService()
app = svc._create_flask_app()
LEAK = io.open(r"C:\Users\LENOVO\AppData\Local\Temp\claude\C--Users-sathi-PycharmProjects-HARTOS\99c39a1e-f517-4573-b62b-d8d225b93f1e\scratchpad\leakhunt.js", encoding="utf-8").read()
_BOOT = re.compile(rb'<div id="hart-boot".*?</div>\s*', re.S)
_orig = app.wsgi_app
def _mw(environ, start_response):
    p = environ.get("PATH_INFO","")
    if p == "/api/onboarding/status":
        b=b'{"onboarded": true, "lit": true, "success": true}'
        start_response("200 OK",[("Content-Type","application/json"),("Content-Length",str(len(b)))]); return [b]
    if "/stream" in p or p.endswith("/events"):
        b=b"retry: 3600000\n\n"
        start_response("200 OK",[("Content-Type","text/event-stream"),("Content-Length",str(len(b)))]); return [b]
    if p == "/leakhunt":
        try:
            n = int(environ.get("CONTENT_LENGTH") or 0)
            data = environ["wsgi.input"].read(n)
            io.open('C:/Users/LENOVO/AppData/Local/Temp/claude/C--Users-sathi-PycharmProjects-HARTOS/99c39a1e-f517-4573-b62b-d8d225b93f1e/scratchpad/leak_result.json', "wb").write(data)
            print("LEAKHUNT RESULT RECEIVED", len(data), flush=True)
        except Exception as e:
            print("leakhunt capture failed:", e, flush=True)
        start_response("200 OK",[("Content-Type","application/json"),("Content-Length","2")]); return [b"{}"]
    if p == "/":
        chunks=[]
        def _sr(s,h,e=None): _mw.s=s; _mw.h=h; return chunks.append
        it=_orig(environ,_sr)
        body=b"".join(list(it)) if it else b"".join(chunks)
        if hasattr(it,"close"): it.close()
        body=_BOOT.sub(b"",body)
        body=body.replace(b"</body>", b"<script>"+LEAK.encode()+b"</script></body>")
        hdrs=[(k,v) for k,v in _mw.h if k.lower()!="content-length"]; hdrs.append(("Content-Length",str(len(body))))
        start_response(_mw.s,hdrs); return [body]
    return _orig(environ,start_response)
app.wsgi_app=_mw
print("leakhunt server on 6810", flush=True)
app.run(host="127.0.0.1",port=6810,debug=False,use_reloader=False,threaded=True)
