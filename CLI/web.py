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
import contextlib
import json
import os
import secrets
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mini_quark_cli as mq

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = secrets.token_urlsafe(24)
LOCK = threading.Lock()          # uma resposta por vez
PENDING = {}                     # confirmações de ferramentas aguardando o usuário
SAFE = {"lembrar", "consultar_teia"}   # ferramentas permitidas no modo "Chat"
ALL_TOOLS = mq.all_tools
mq.USE_COLOR = False


def state():
    ch = mq.active_chat()
    return {"chats": [{"id": c["id"], "title": c["title"]} for c in reversed(mq.S["chats"])],
            "active": ch["id"], "model": mq.cfg("model"), "hasKey": bool(mq.api_key()),
            "history": [m for m in ch["history"] if m["role"] in ("user", "assistant")]}


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
            if d.get("model"):
                S["config"]["model"] = d["model"].strip()
            if d.get("key"):
                S["config"]["apiKey"] = d["key"].strip()
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

        if not LOCK.acquire(blocking=False):
            return emit("error", "Já existe uma resposta em andamento.")
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
            mq.STREAM = {"start": lambda: emit("seg"), "token": lambda t: emit("text", t), "end": lambda: None,
                         "think_start": lambda: emit("think_start"), "think_token": lambda t: emit("think", t),
                         "think_end": lambda s: emit("think_end", s)}
            mq.show_step = lambda title, detail="": emit("step", title + (": " + str(detail).strip().splitlines()[0][:120]
                                                                             if str(detail).strip() else ""))

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
            try:
                with contextlib.redirect_stdout(Sink(emit)):
                    mq.chat_turn(text)
                emit("done")
            except KeyboardInterrupt:
                pass
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
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()