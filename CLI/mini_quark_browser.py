#!/usr/bin/env python3
"""
Ferramenta `navegador` do Mini-Quark (Playwright + Chromium).

Instalação:   pip install playwright && playwright install chromium
Uso:          importado pelo mini_quark_web.py (a ferramenta só aparece no modo Work).

Ciclo: o modelo chama `navegador` com uma ação; a resposta é sempre uma OBSERVAÇÃO nova
(URL, elementos clicáveis com número e coordenadas, texto visível). A interface web recebe
em tempo real a captura (com cursor/cliques), o outline do DOM e o histórico de ações.

Controles: site novo pede confirmação · rede local/privada bloqueada · navegação por link
só dentro de domínios já autorizados · sem downloads, popups ou senhas · limite de ações
por turno · Parar na interface interrompe o agente.
O conteúdo do site NUNCA vai para a interface como HTML: só imagem JPEG e texto puro.
"""
import atexit
import base64
import ipaddress
import json
import queue
import socket
import threading
from collections import deque
from functools import lru_cache
from urllib.parse import urlparse

import mini_quark_cli as mq

VW, VH = 1024, 640
MAX_STEPS = 30
ACTS = ("abrir", "ver", "clicar", "mover", "digitar", "tecla", "rolar", "voltar", "console", "js", "fechar")
SCHEMA = mq.tool_schema(
    "navegador",
    "Controla um navegador (Chromium) para acessar sites, ver páginas e interagir. Toda ação devolve a observação "
    "atual da página (URL, elementos interativos numerados [n] com coordenadas, texto visível). Ciclo: 'abrir' uma URL, "
    "leia a observação, escolha UMA ação (clicar/digitar/tecla/rolar...), observe o resultado e repita. "
    "'clicar' aceita 'elemento' (número [n]) ou x,y; 'mover' move o cursor; 'rolar' usa dy (positivo = desce); "
    "'digitar' escreve no campo focado (clique nele antes); 'tecla' ex.: Enter, Tab; 'ver' só observa; 'fechar' encerra. "
    "'console' lê o console do navegador (console.log/warn/error, erros de JS, requisições que falharam e respostas HTTP 4xx/5xx); "
    "filtre com 'nivel' (error|warning|log|all) e use limpar=true para esvaziar. "
    "'js' avalia uma expressão JavaScript ('expressao') na página e devolve o resultado (o usuário precisa autorizar cada uso).",
    {"acao": {"type": "string", "enum": list(ACTS)}, "url": {"type": "string"},
     "elemento": {"type": "integer", "description": "número [n] da última observação"},
     "x": {"type": "number"}, "y": {"type": "number"}, "dy": {"type": "number"},
     "texto": {"type": "string"}, "tecla": {"type": "string"},
     "nivel": {"type": "string", "enum": ["all", "error", "warning", "log"]}, "limpar": {"type": "boolean"},
     "expressao": {"type": "string", "description": "expressão JavaScript para a ação 'js'"}}, ["acao"])

JS = """() => {
 const els = [], out = [], SKIP = ['SCRIPT','STYLE','NOSCRIPT','SVG','PATH','LINK','META'];
 const inter = 'a[href],button,input,textarea,select,[role=button],[role=link],[onclick],[contenteditable=true]';
 const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
   return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' &&
          r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth; };
 let n = 0;
 const walk = (e, d) => {
   if (!(e instanceof Element) || out.length > 350 || SKIP.includes(e.tagName.toUpperCase())) return;
   if (getComputedStyle(e).display === 'none') return;
   let tag = '<' + e.tagName.toLowerCase();
   if (e.matches(inter) && vis(e)) {
     n++; const r = e.getBoundingClientRect();
     const t = (e.innerText || e.value || e.placeholder || e.getAttribute('aria-label') || e.title || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
     els.push({i: n, tag: e.tagName.toLowerCase(), t, type: e.type || '', x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)});
     tag = '[' + n + '] ' + tag;
   }
   for (const a of ['id','class','href','name','type','placeholder','alt','role']) { const v = e.getAttribute(a); if (v) tag += ' ' + a + '="' + String(v).slice(0, 40) + '"'; }
   const own = [...e.childNodes].filter(c => c.nodeType === 3).map(c => c.textContent.trim()).join(' ').replace(/\\s+/g, ' ').slice(0, 80);
   out.push('  '.repeat(d) + tag + '>' + (own ? ' ' + own : ''));
   if (d < 12) for (const c of e.children) walk(c, d + 1);
 };
 walk(document.documentElement, 0);
 return {els: els.slice(0, 40), outline: out.join('\\n').slice(0, 9000), scrollY: Math.round(scrollY),
         scrollH: document.documentElement.scrollHeight, text: ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').slice(0, 1500)};
}"""

_sink = None
_steps = 0
ALLOWED = set()
_blocked = []
_els = []
_cx, _cy = VW // 2, VH // 2
_pw = _br = _ctx = _pg = None
_logs = deque(maxlen=300)   # console da página: logs, erros de JS, falhas de rede
_logn = 0


def _log(nivel, text, loc=""):
    global _logn
    _logn += 1
    e = {"n": _logn, "nivel": nivel, "text": str(text)[:400], "loc": loc}
    _logs.append(e)
    _emit({"k": "con", "nivel": nivel, "text": e["text"]})


def _console(a):
    nivel = str(a.get("nivel") or "all").lower()
    groups = {"error": ("error", "pageerror", "http", "requestfailed", "assert"), "warning": ("warning",),
              "log": ("log", "info", "debug", "dir", "table", "trace")}
    items = [e for e in _logs if nivel == "all" or e["nivel"] in groups.get(nivel, (nivel,))]
    out = "\n".join(f'[{e["n"]}] {e["nivel"]}: {e["text"]}' + (f' ({e["loc"]})' if e["loc"] else "") for e in items[-50:])
    if a.get("limpar"):
        _logs.clear()
    return out or "(console vazio para este filtro)"


def set_sink(fn):
    global _sink
    _sink = fn


def reset_budget():
    global _steps
    _steps = 0


def _emit(d):
    if _sink:
        _sink(d)


# ── Thread dedicada: a API síncrona do Playwright só pode ser usada pela thread que a criou ──
class _Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.q = queue.Queue()
        self.start()

    def run(self):
        while True:
            fn, arg, box = self.q.get()
            try:
                box["r"] = fn(arg)
            except BaseException as e:
                box["e"] = e
            box["ev"].set()

    def call(self, fn, arg, timeout=90):
        box = {"ev": threading.Event()}
        self.q.put((fn, arg, box))
        if not box["ev"].wait(timeout):
            raise TimeoutError("o navegador demorou demais para responder")
        if "e" in box:
            raise box["e"]
        return box["r"]


W = _Worker()


# ── Segurança de rede ──
@lru_cache(maxsize=512)
def _private(host):
    if not host or host.lower() == "localhost" or host.endswith((".local", ".internal")):
        return True
    try:
        ips = {i[4][0].split("%")[0] for i in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    for ip in ips:
        a = ipaddress.ip_address(ip)
        if a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast:
            return True
    return False


def _allowed(host):
    host = (host or "").lower()
    return any(host == a or host.endswith("." + a) for a in ALLOWED)


def _check_url(url):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("só URLs http/https são permitidas")
    if _private(u.hostname):
        raise ValueError("endereços locais/privados não são permitidos")
    return u.hostname.lower()


def _route(route):
    req = route.request
    try:
        u = urlparse(req.url)
        if u.scheme in ("data", "blob", "about"):
            return route.continue_()
        if u.scheme not in ("http", "https") or _private(u.hostname):
            return route.abort()
        if req.is_navigation_request() and req.frame == _pg.main_frame and not _allowed(u.hostname):
            _blocked.append(u.hostname)
            return route.abort()
    except Exception:
        return route.abort()
    route.continue_()


def _page():
    global _pw, _br, _ctx, _pg
    if _pg and not _pg.is_closed():
        return _pg
    from playwright.sync_api import sync_playwright   # ImportError → mensagem de instalação em run()
    _pw = _pw or sync_playwright().start()
    _br = _br or _pw.chromium.launch(headless=True)
    _ctx = _br.new_context(viewport={"width": VW, "height": VH}, accept_downloads=False, service_workers="block")
    _ctx.route("**/*", _route)
    _pg = _ctx.new_page()
    _pg.on("dialog", lambda d: d.dismiss())
    _pg.on("popup", lambda p: p.close())
    _pg.on("console", lambda m: _log(m.type, m.text, (f'{m.location.get("url", "")[:80]}:{m.location.get("lineNumber", "")}'
                                                      if m.location and m.location.get("url") else "")))
    _pg.on("pageerror", lambda e: _log("pageerror", e))
    _pg.on("requestfailed", lambda r: _log("requestfailed", f"{r.method} {r.url[:150]} — {r.failure}"))
    _pg.on("response", lambda r: r.status >= 400 and _log("http", f"{r.status} {r.url[:150]}"))
    return _pg


def _close(_=None):
    global _pw, _br, _ctx, _pg
    for obj, m in ((_ctx, "close"), (_br, "close"), (_pw, "stop")):
        try:
            obj and getattr(obj, m)()
        except Exception:
            pass
    _pw = _br = _ctx = _pg = None
    ALLOWED.clear()


def close():
    try:
        W.call(_close, None, 10)
    except Exception:
        pass


atexit.register(close)


# ── Ações (executam na thread do navegador) ──
def _target(a):
    if a.get("elemento") is not None:
        for e in _els:
            if e["i"] == int(a["elemento"]):
                return e["x"], e["y"]
        raise ValueError("elemento não encontrado: use 'ver' para atualizar a lista")
    if a.get("x") is None or a.get("y") is None:
        raise ValueError("informe 'elemento' ou 'x' e 'y'")
    return max(0.0, min(VW - 1.0, float(a["x"]))), max(0.0, min(VH - 1.0, float(a["y"])))


def _observe(pg, click):
    global _els
    d = pg.evaluate(JS)
    _els = d["els"]
    shot = base64.b64encode(pg.screenshot(type="jpeg", quality=55)).decode()
    title = pg.title()
    _emit({"k": "shot", "img": shot, "w": VW, "h": VH, "x": _cx, "y": _cy, "click": click, "url": pg.url, "title": title})
    _emit({"k": "dom", "text": d["outline"]})
    lines = [f'[{e["i"]}] {e["tag"]}' + (f'({e["type"]})' if e["type"] else "") + f' "{e["t"]}" @{e["x"]},{e["y"]}' for e in _els]
    blk = ("\nNavegação bloqueada (domínio não autorizado; use 'abrir'): " + ", ".join(sorted(set(_blocked)))) if _blocked else ""
    return (f"URL: {pg.url}\nTítulo: {title}\nViewport {VW}x{VH} · rolagem {d['scrollY']}/{d['scrollH']}{blk}\n"
            'Elementos interativos visíveis ([n] tag "texto" @x,y):\n' + ("\n".join(lines) or "(nenhum)") +
            "\nTexto visível: " + d["text"])


def _do(a):
    global _cx, _cy
    act = a["acao"]
    if act == "fechar":
        _close()
        return "Navegador fechado."
    pg, click = _page(), False
    if act == "console":
        return _console(a)
    if act == "js":
        return "Resultado: " + json.dumps(pg.evaluate(str(a["expressao"])), ensure_ascii=False, default=str)[:2000]
    if act == "abrir":
        _blocked.clear()
        _logs.clear()
        pg.goto(a["url"], wait_until="domcontentloaded", timeout=20000)
    elif act == "voltar":
        pg.go_back(wait_until="domcontentloaded", timeout=15000)
    elif act in ("clicar", "mover"):
        x, y = _target(a)
        pg.mouse.move(x, y, steps=12)
        _cx, _cy = x, y
        if act == "clicar":
            pg.mouse.click(x, y)
            click = True
    elif act == "digitar":
        if pg.evaluate("!!(document.activeElement && document.activeElement.type === 'password')"):
            return "[Bloqueado] não digito em campos de senha."
        pg.keyboard.type(str(a.get("texto", "")), delay=25)
    elif act == "tecla":
        pg.keyboard.press(str(a.get("tecla") or "Enter"))
    elif act == "rolar":
        pg.mouse.move(_cx, _cy)
        pg.mouse.wheel(0, float(a.get("dy", 500)))
    try:
        pg.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass
    pg.wait_for_timeout(400)
    return _observe(pg, click)


def run(a):
    """Chamada pelo mq.execute_tool (thread do turno): valida, pede confirmação e delega à thread do navegador."""
    global _steps
    act = a.get("acao")
    if act not in ACTS:
        return "Ação inválida. Use: " + ", ".join(ACTS)
    if act != "fechar":
        _steps += 1
        if _steps > MAX_STEPS:
            return "Limite de ações do navegador neste turno atingido. Resuma ao usuário e pergunte se deve continuar."
    if act == "abrir":
        url = str(a.get("url") or "")
        url = url if "://" in url else "https://" + url
        try:
            host = _check_url(url)
        except ValueError as e:
            return f"[Bloqueado] {e}"
        if not _allowed(host):
            if not mq.confirm("abrir site no navegador", url):
                return "O usuário negou o acesso a este site."
            ALLOWED.add(host)
        a = dict(a, url=url)
    if act == "js":
        expr = str(a.get("expressao") or "").strip()
        if not expr:
            return "Erro: informe 'expressao'."
        if not mq.confirm("executar JavaScript na página", expr):
            return "O usuário negou a execução do JavaScript."
    brief = " ".join(f"{k}={str(v)[:60]}" for k, v in a.items() if k != "acao")
    mq.show_step("Navegador", f"{act} {brief}".strip())
    _emit({"k": "act", "name": act, "args": brief})
    try:
        res = W.call(_do, a)
    except KeyboardInterrupt:
        raise
    except ImportError:
        res = "[Erro] Playwright não instalado. Rode: pip install playwright && playwright install chromium"
    except Exception as e:
        res = f"[Erro no navegador] {str(e)[:300]}"
    _emit({"k": "res", "result": res[:400]})
    return res


mq.register_tool(SCHEMA, run)