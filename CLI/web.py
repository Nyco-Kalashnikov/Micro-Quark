#!/usr/bin/env python3
"""
mini-quark web — interface web local (só biblioteca padrão).

Coloque na mesma pasta de mini_quark_cli.py e mini_quark_web.html:
    python mini_quark_web.py            # abre http://127.0.0.1:8000
    python mini_quark_web.py --yes      # ferramentas rodam sem pedir confirmação (cuidado)

Segurança: escuta só em 127.0.0.1, valida o Host (anti DNS-rebinding) e exige um token
aleatório (gerado a cada execução) em toda chamada /api — outras páginas abertas no seu
navegador não conseguem mandar comandos para cá.
"""
import argparse
import base64
import contextlib
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import mini_quark_cli as mq

try:   # ferramenta de navegador (opcional): precisa de mini_quark_browser.py ao lado
    import mini_quark_browser as qb
except Exception:
    qb = None

try:   # pesquisa na web + cliente MCP (Roblox Studio): precisa de mini_quark_tools.py ao lado
    import mini_quark_tools as qt
except Exception as _e:   # não esconde o motivo: sem isso as ferramentas novas somem sem aviso
    qt = None
    print(f"[aviso] mini_quark_tools.py não carregou ({type(_e).__name__}: {_e}) — pesquisa web e MCP indisponíveis.")

if qt:   # evita que o modelo procure o servidor MCP pelo terminal
    _sp = mq.system_prompt
    mq.system_prompt = lambda: _sp() + (
        "\n\nFerramentas MCP: para conectar ao Roblox Studio (ou qualquer servidor MCP) use SOMENTE a ferramenta "
        "mcp_servidor com acao='conectar' — nunca procure o servidor pelo terminal. Depois de conectar, as ferramentas "
        "roblox_studio__* aparecem na próxima rodada. Para buscar na internet use pesquisar_web e ler_url.")

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.Lock()          # uma resposta por vez
SHOTS = os.path.join(mq.HOME, "shots")   # última captura do navegador de cada conversa
TURN = {"cancel": False}         # cancelamento cooperativo do turno em andamento
PENDING = {}                     # confirmações de ferramentas aguardando o usuário
SAFE = {"lembrar", "consultar_teia", "pesquisar_web", "ler_url"}   # permitidas no modo "Chat" (MCP/terminal só no Work)
ALL_TOOLS = mq.all_tools
mq.USE_COLOR = False


def valid_traces(ch):
    """Raciocínio/passos salvos por turno (chave = índice da mensagem do usuário); ignora os que não batem mais com o histórico."""
    out = {}
    for k, t in (ch.get("traces") or {}).items():
        i = int(k)
        if i < len(ch["history"]) and ch["history"][i]["role"] == "user" and ch["history"][i]["content"][:80] == t.get("u"):
            out[k] = t["items"]
    return out


def state():
    ch = mq.active_chat()
    return {"chats": [{"id": c["id"], "title": c["title"]} for c in reversed(mq.S["chats"])],
            "active": ch["id"], "model": mq.cfg("model"), "hasKey": bool(mq.api_key()), "provider": mq.provider(), "browser": qb is not None,
            "providers": {k: {"nome": v["nome"], "model": mq.saved_model(k) if k != mq.provider() else mq.cfg("model"),
                              "key": mq.key_status(k), "url": v["url"]} for k, v in mq.PROVIDERS.items()},
            "baseUrl": mq.cfg("baseUrl"), "mcp": qt.mcp_status() if qt else [],
            "history": [m for m in ch["history"] if m["role"] in ("user", "assistant")],
            "traces": valid_traces(ch)}


class Sink:
    """Captura o que o mq imprime (erros, avisos) e manda para o navegador."""
    def __init__(self, emit): self.emit = emit
    def write(self, s):
        if s.strip():
            self.emit("log", s.strip())
        return len(s)
    def flush(self): pass
    def isatty(self): return False


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _allowed(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost") and (self.path == "/" or self.headers.get("X-Token") == TOKEN)

    def do_GET(self):
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        if self.path == "/":
            with open(os.path.join(HERE, "mini_quark_web.html"), encoding="utf-8") as f:
                b = f.read().replace("__TOKEN__", TOKEN).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif self.path == "/api/state":
            self._json(state())
        elif self.path == "/api/mcp":
            self._json({"mcp": qt.mcp_status() if qt else []})
        elif self.path.startswith("/api/browser"):
            cid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            ch = next((c for c in mq.S["chats"] if c["id"] == cid), None)
            img = None
            if ch:
                try:
                    with open(os.path.join(SHOTS, ch["id"] + ".jpg"), "rb") as f:
                        img = base64.b64encode(f.read()).decode()
                except OSError:
                    pass
            self._json({"b": (ch or {}).get("browser") or {}, "img": img})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._allowed():
            return self._json({"error": "forbidden"}, 403)
        d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        route, S = self.path[5:], mq.S
        if route == "send":
            return self.send(d)
        if route == "chat/new":
            ch = {"id": f"chat_{int(time.time() * 1000)}", "title": f"Conversa {len(S['chats']) + 1}", "history": []}
            S["chats"].append(ch)
            S["activeChatId"] = ch["id"]
        elif route == "chat/switch":
            if any(c["id"] == d.get("id") for c in S["chats"]):
                S["activeChatId"] = d["id"]
        elif route == "chat/delete":
            S["chats"] = [c for c in S["chats"] if c["id"] != d.get("id")] or mq.default_state()["chats"]
            if not any(c["id"] == S["activeChatId"] for c in S["chats"]):
                S["activeChatId"] = S["chats"][-1]["id"]
        elif route == "config":
            if d.get("provider") in mq.PROVIDERS and d["provider"] != mq.provider():
                mq.set_provider(d["provider"])
            if d.get("baseUrl") is not None and mq.provider() == "custom":
                S["config"]["baseUrl"] = d["baseUrl"].strip()
            if d.get("model"):
                S["config"]["model"] = d["model"].strip()
            if d.get("key"):
                mq.set_key(d["key"].strip())
        elif route == "cancel":
            TURN["cancel"] = True
            for p in list(PENDING.values()):          # destrava confirmações pendentes (negadas)
                p["ok"] = False
                p["ev"].set()
            try:                                      # destrava a leitura bloqueada da resposta do modelo
                r = mq.ACTIVE.get("res")
                r and r.close()
            except Exception:
                pass
            return self._json({"ok": True})
        elif route == "browser/close":
            if qb:
                qb.close()
            return self._json({"ok": True})
        elif route in ("mcp/connect", "mcp/disconnect"):
            if not qt:
                return self._json({"error": "mini_quark_tools.py não encontrado"}, 500)
            nome, err = d.get("nome") or "Roblox_Studio", None
            if LOCK.locked():          # evita mexer nas ferramentas no meio de uma resposta
                return self._json({"error": "Aguarde a resposta atual terminar."}, 409)
            try:
                if route == "mcp/connect":
                    qt.mcp_connect(nome, ask=False)   # o clique do usuário já é a confirmação
                else:
                    qt.mcp_disconnect(nome)
            except Exception as e:
                err = str(e)
            return self._json({"mcp": qt.mcp_status(), "error": err})
        elif route == "confirm":
            p = PENDING.get(d.get("id"))
            if p:
                if d.get("always"):
                    mq.AUTO = True
                p["ok"] = bool(d.get("ok"))
                p["ev"].set()
            return self._json({"ok": True})
        else:
            return self._json({"error": "not found"}, 404)
        mq.save_state()
        self._json(state())

    def send(self, d):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def emit(t, v=None, **kw):
            try:
                self.wfile.write(b"data: " + json.dumps({"t": t, "d": v, **kw}, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
            except OSError:          # navegador fechou/abortou: interrompe o turno
                raise KeyboardInterrupt
            if TURN["cancel"]:       # botão Parar: idem
                raise KeyboardInterrupt

        # Depois de um Parar, o turno antigo pode levar alguns segundos para encerrar: espera por ele.
        got = LOCK.acquire(timeout=8) if TURN["cancel"] else LOCK.acquire(blocking=False)
        if not got:
            return emit("error", "A resposta anterior ainda está sendo encerrada. Tente de novo em instantes."
                        if TURN["cancel"] else "Já existe uma resposta em andamento.")
        TURN["cancel"] = False
        mq.should_stop = lambda: TURN["cancel"]
        try:
            ch = next((c for c in mq.S["chats"] if c["id"] == d.get("chat")), None)
            text = (d.get("text") or "").strip()
            if not ch or not text:
                return emit("error", "Conversa ou mensagem inválida.")
            mq.S["activeChatId"] = ch["id"]
            if not ch["history"] and ch["title"].startswith("Conversa"):
                ch["title"] = text.split("\n")[0][:40]
            mq.S["config"]["effort"] = "medium" if d.get("think") else "auto"
            work = d.get("mode") == "work"
            mq.all_tools = ALL_TOOLS if work else (lambda: [t for t in ALL_TOOLS() if t["function"]["name"] in SAFE])
            # Grava o que acontece no turno (raciocínio, trechos, passos) para reconstruir após recarregar a página.
            base, trace, cur = len(ch["history"]), [], {"seg": None, "think": None}

            def rec(kind, **kw):
                trace.append({"k": kind, **kw})
                return trace[-1]

            def h_start():
                cur["seg"] = rec("seg", text="")
                emit("seg")

            def h_token(t):
                cur["seg"] = cur["seg"] or rec("seg", text="")
                cur["seg"]["text"] += t
                emit("text", t)

            def h_tstart():
                cur["think"] = rec("think", text="", secs=None)
                emit("think_start")

            def h_ttoken(t):
                cur["think"] = cur["think"] or rec("think", text="", secs=None)
                cur["think"]["text"] += t
                emit("think", t)

            def h_tend(s):
                if cur["think"]:
                    cur["think"]["secs"] = s
                cur["think"] = None
                emit("think_end", s)

            def step(title, detail=""):
                d = str(detail).strip()
                msg = title + (": " + d.splitlines()[0][:120] if d else "")
                cur["seg"] = None
                rec("step", text=msg)
                emit("step", msg)

            def brec(p):   # estado do painel do navegador, por conversa
                b, k = ch.setdefault("browser", {}), p.get("k")
                if k == "shot":
                    b.update({x: p[x] for x in ("url", "title", "x", "y", "w", "h")})
                    try:
                        os.makedirs(SHOTS, exist_ok=True)
                        with open(os.path.join(SHOTS, ch["id"] + ".jpg"), "wb") as f:
                            f.write(base64.b64decode(p["img"]))
                    except OSError:
                        pass
                elif k == "dom":
                    b["dom"] = p["text"]
                elif k == "act":
                    if p["name"] == "abrir":
                        b["con"] = []
                    b.setdefault("hist", []).append({"name": p["name"], "args": p["args"], "result": None})
                    del b["hist"][:-100]
                elif k == "res" and b.get("hist"):
                    b["hist"][-1]["result"] = p["result"]
                elif k == "con":
                    b.setdefault("con", []).append({"nivel": p["nivel"], "text": p["text"]})
                    del b["con"][:-200]

            mq.STREAM = {"start": h_start, "token": h_token, "end": lambda: None,
                         "think_start": h_tstart, "think_token": h_ttoken, "think_end": h_tend}
            mq.show_step = step

            def ask(kind, detail):
                if mq.AUTO or mq.cfg("auto"):
                    return True
                cid = uuid.uuid4().hex
                p = PENDING[cid] = {"ev": threading.Event(), "ok": False}
                emit("confirm", None, id=cid, kind=kind, detail=str(detail)[:2000])
                p["ev"].wait(300)
                PENDING.pop(cid, None)
                return p["ok"]

            mq.confirm = ask
            if qt:   # construir algo no Studio exige várias rodadas de ferramenta
                mq.MAX_TOOL_ROUNDS = max(mq.MAX_TOOL_ROUNDS, qt.MAX_ROUNDS)
            if qb:   # navegador: eventos ao vivo para o painel + mais rodadas de ferramenta para o ciclo observar→agir
                qb.set_sink(lambda p: (brec(p), emit("browser", None, **p)))
                qb.reset_budget()
                mq.MAX_TOOL_ROUNDS = max(mq.MAX_TOOL_ROUNDS, qb.MAX_STEPS)
            try:
                with contextlib.redirect_stdout(Sink(lambda t, v: (rec("log", text=v), emit(t, v)))):
                    mq.chat_turn(text)
                emit("done")
            except KeyboardInterrupt:
                pass
            except Exception as e:   # ex.: leitura fechada pelo Parar, queda de rede
                if not TURN["cancel"]:
                    try:
                        emit("error", f"Falha: {e}")
                    except KeyboardInterrupt:
                        pass
            finally:
                if trace:
                    ch.setdefault("traces", {})[str(base)] = {"u": text[:80], "items": trace}
                mq.save_state()
                if qb:
                    qb.set_sink(None)
        finally:
            LOCK.release()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("-y", "--yes", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    mq.load_state()
    mq.AUTO = a.yes
    url = f"http://127.0.0.1:{a.port}"
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    print(f"mini-quark web em {url}  (Ctrl+C para sair)")
    print(f"  ferramentas web/MCP V0.2: {'ativas' if qt else 'INATIVAS'}")
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()