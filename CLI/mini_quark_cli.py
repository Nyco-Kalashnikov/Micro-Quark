#!/usr/bin/env python3
"""
mini-quark CLI — versão 100% terminal (sem navegador, sem FastAPI, sem servidor).

Só usa a biblioteca padrão do Python 3.8+. Um arquivo, zero dependências.

Uso:
    export OPENROUTER_API_KEY=sk-or-...     # ou use /key dentro do programa
    python mini_quark_cli.py                # modo interativo
    python mini_quark_cli.py -p "oi"        # one-shot (imprime a resposta e sai)
    python mini_quark_cli.py --yes          # executa ferramentas sem pedir confirmação

Dados ficam em ~/.mini_quark/state.json (mude com MQ_HOME=/outro/lugar).
O arquivo tem o mesmo formato do backup do mini_quark.html, então /import e
/export funcionam nos dois sentidos.
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

try:
    import readline  # noqa: F401  (histórico/edição de linha; não existe no Windows puro)
except ImportError:
    pass

# ───────────────────────────── cores ─────────────────────────────
if platform.system() == "Windows":
    os.system("")  # habilita ANSI no console do Windows 10+
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


def green(s): return c("38;5;79", s)
def amber(s): return c("38;5;179", s)
def red(s):   return c("38;5;167", s)
def dim(s):   return c("2", s)
def bold(s):  return c("1", s)
def blue(s):  return c("38;5;111", s)


# ───────────────────────────── estado ─────────────────────────────
HOME = os.environ.get("MQ_HOME") or os.path.join(os.path.expanduser("~"), ".mini_quark")
STATE_FILE = os.path.join(HOME, "state.json")
OUT_DIR = os.path.join(os.getcwd(), "mq_output")
API_URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_TOOL_ROUNDS = 8


def default_root():
    return {"id": "root", "label": "mini-quark",
            "description": "Nó raiz do sistema de teia de memória.", "importance": 5}


def default_state():
    return {
        "v": 1,
        "chats": [{"id": "chat_default", "title": "Conversa Principal", "history": []}],
        "activeChatId": "chat_default",
        "graph": {"nodes": [default_root()], "links": []},
        "tools": [],
        "selected": "root",
        "config": {"apiKey": "", "model": "cohere/north-mini-code:free", "extra": "",
                   "temperature": 0.7, "maxTokens": 4096, "auto": False},
    }


S = default_state()


def load_state():
    global S
    base = default_state()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k in ("chats", "activeChatId", "graph", "tools", "selected"):
            if k in data:
                base[k] = data[k]
        base["config"].update(data.get("config", {}))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(red(f"Aviso: state.json ilegível ({e}); começando do zero."))
    if not base["chats"]:
        base["chats"] = default_state()["chats"]
    if not base["graph"].get("nodes"):
        base["graph"] = {"nodes": [default_root()], "links": []}
    base["graph"].setdefault("links", [])
    S = base


def save_state():
    os.makedirs(HOME, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=HOME, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(S, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)  # escrita atômica


def cfg(k):
    return S["config"][k]


def api_key():
    return os.environ.get("OPENROUTER_API_KEY") or cfg("apiKey")


def active_chat():
    for ch in S["chats"]:
        if ch["id"] == S["activeChatId"]:
            return ch
    S["activeChatId"] = S["chats"][0]["id"]
    return S["chats"][0]


# ───────────────────────────── teia de memória ─────────────────────────────
def find_node(v):
    if not v:
        return None
    for n in S["graph"]["nodes"]:
        if n["id"] == v:
            return n
    v = str(v).lower()
    for n in S["graph"]["nodes"]:
        if n["label"].lower() == v:
            return n
    return None


def add_node(label, description="", importance=2, parent=None):
    try:
        imp = max(1, min(5, int(importance)))
    except (TypeError, ValueError):
        imp = 2
    node = {"id": f"n:{int(time.time() * 1000)}{os.urandom(2).hex()}", "label": label,
            "description": description, "importance": imp}
    S["graph"]["nodes"].append(node)
    p = find_node(parent) or find_node(S["selected"]) or S["graph"]["nodes"][0]
    add_link(p["id"], node["id"])
    S["selected"] = node["id"]
    save_state()
    return node


def add_link(a, b):
    if not a or not b or a == b:
        return
    for l in S["graph"]["links"]:
        if {l["source"], l["target"]} == {a, b}:
            return
    S["graph"]["links"].append({"source": a, "target": b})


def remove_node(node):
    if node["id"] == "root":
        return False
    S["graph"]["nodes"] = [n for n in S["graph"]["nodes"] if n["id"] != node["id"]]
    S["graph"]["links"] = [l for l in S["graph"]["links"]
                           if node["id"] not in (l["source"], l["target"])]
    if S["selected"] == node["id"]:
        S["selected"] = "root"
    save_state()
    return True


def memory_summary():
    nodes, links = S["graph"]["nodes"], S["graph"]["links"]
    if not nodes:
        return "(Teia de memória vazia)"
    by_id = {n["id"]: n for n in nodes}
    children, incoming = {}, set()
    for l in links:
        if l["source"] in by_id and l["target"] in by_id:
            children.setdefault(l["source"], []).append(l["target"])
            incoming.add(l["target"])
    fmt = lambda n: f"{n['label']} (Imp: {n['importance']}" + (f" — {n['description']}" if n.get("description") else "") + ")"
    out, visited = [], set()

    def walk(i, depth, path):
        n = by_id.get(i)
        if not n:
            return
        visited.add(i)
        rel = "Nó raiz" if depth == 0 else "filho direto" if depth == 1 else f"nível {depth} (descendente indireto)"
        out.append(f"{'  ' * depth}- [{rel}] {fmt(n)}")
        for k in children.get(i, []):
            if k in path:
                out.append(f"{'  ' * (depth + 1)}- [referência circular] {by_id[k]['label']} (já listado acima)")
            else:
                walk(k, depth + 1, path | {k})

    roots = [n for n in nodes if n["id"] not in incoming] or nodes
    for r in roots:
        if r["id"] not in visited:
            walk(r["id"], 0, {r["id"]})
    for n in nodes:
        if n["id"] not in visited:
            out.append(f"- [nó desconectado das raízes] {fmt(n)}")
            visited.add(n["id"])
    return "\n".join(out)


def print_graph():
    sel = S["selected"]
    text = memory_summary()
    print(text)
    n = find_node(sel)
    print(dim(f"\n{len(S['graph']['nodes'])} nós · {len(S['graph']['links'])} ligações · "
              f"selecionado: {n['label'] if n else 'root'}"))


# ───────────────────────────── ferramentas ─────────────────────────────
def tool_schema(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required}}}


S_ = {"type": "string"}
BASE_TOOLS = [
    tool_schema("executar_terminal", "Executa um comando de shell no sistema local e devolve exit code, stdout e stderr.",
                {"comando": {"type": "string", "description": "O comando a ser executado"}}, ["comando"]),
    tool_schema("lembrar", "Salva um novo conceito na teia de memória com rótulo, descrição e importância (1-5).",
                {"rotulo": S_, "descricao": S_, "importancia": {"type": "number"}}, ["rotulo", "descricao"]),
    tool_schema("run_code", "Executa código Python, JavaScript, Bash ou PowerShell no sistema local.",
                {"language": S_, "code": S_}, ["language", "code"]),
    tool_schema("create_file", "Cria um arquivo de texto/código no disco (pasta ./mq_output).",
                {"filename": S_, "content": S_}, ["filename", "content"]),
    tool_schema("criar_ferramenta", "Cria uma nova ferramenta dinâmica sob demanda. O código em codigo_acao será "
                "REALMENTE executado quando a ferramenta for chamada depois. A variável `argumento` fica disponível.",
                {"nome": S_, "descricao": S_, "codigo_acao": S_,
                 "linguagem": {"type": "string", "description": "python, javascript ou powershell. Padrão: python."}},
                ["nome", "descricao", "codigo_acao"]),
    tool_schema("consultar_teia", "Consulta DIRETAMENTE a estrutura real da teia de memória, sem depender do contexto "
                "já injetado no prompt. Modos: 'no', 'filhos', 'descendentes', 'tudo'.",
                {"modo": S_, "no_id_ou_nome": S_}, ["modo"]),
]


def all_tools():
    custom = [tool_schema(t["nome"], t["descricao"], {"argumento": S_}, ["argumento"]) for _, t in S["tools"]]
    return BASE_TOOLS + custom


AUTO = False  # --yes ou /auto


def confirm(kind, detail):
    if AUTO or cfg("auto"):
        return True
    print(amber(f"\n⚠ O modelo quer: {kind}"))
    for line in str(detail).splitlines()[:25]:
        print("   " + dim(line))
    if len(str(detail).splitlines()) > 25:
        print("   " + dim("..."))
    try:
        ans = input(amber("   Permitir? [s/N/a=sempre nesta sessão] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if ans in ("a", "always"):
        globals()["AUTO"] = True
        return True
    return ans in ("s", "sim", "y", "yes")


def run_process(cmd, shell, timeout):
    try:
        p = subprocess.run(cmd, shell=shell, text=True, capture_output=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return f"[Exit Code: {p.returncode}]\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
    except subprocess.TimeoutExpired:
        return f"[Erro] Tempo limite de {timeout}s excedido."
    except FileNotFoundError as e:
        return f"[Erro] Interpretador não encontrado: {e}"
    except Exception as e:
        return f"[Erro ao executar] {e}"


def run_code(lang, code):
    lang = (lang or "").lower().strip()
    py = sys.executable or ("python" if platform.system() == "Windows" else "python3")
    if lang in ("python", "py"):
        return run_process([py, "-c", code], False, 45)
    if lang in ("javascript", "js", "node"):
        return run_process(["node", "-e", code], False, 45)
    if lang in ("bash", "sh"):
        return run_process(["bash", "-c", code], False, 45)
    if lang in ("powershell", "ps1"):
        exe = "powershell" if platform.system() == "Windows" else "pwsh"
        return run_process([exe, "-Command", code], False, 45)
    return run_process(code, True, 45)


def show_step(title, detail=""):
    d = str(detail).strip().splitlines()
    first = d[0][:100] if d else ""
    print(dim(f"  ⚙ {title}") + (dim(f": {first}") if first else ""))


def execute_tool(name, args):
    if name == "executar_terminal":
        cmd = args.get("comando", "")
        if not confirm("executar comando no terminal", cmd):
            return "O usuário negou a execução do comando."
        show_step("Terminal Local", cmd)
        return run_process(cmd, True, 120)

    if name == "run_code":
        lang, code = args.get("language", ""), args.get("code", "")
        if not confirm(f"executar código ({lang})", code):
            return "O usuário negou a execução do código."
        show_step("Executar Código Local", f"{lang}: {code}")
        return run_code(lang, code)

    if name == "lembrar":
        n = add_node(args.get("rotulo") or "Conceito", args.get("descricao") or "", args.get("importancia") or 2)
        show_step("Memória Atualizada", f"[{n['label']}]")
        return f'Conceito "{n["label"]}" memorizado na teia com sucesso.'

    if name == "create_file":
        fn = os.path.basename(str(args.get("filename") or "").replace("\\", "/")) or "saida.txt"
        if not confirm("criar arquivo", f"{os.path.join(OUT_DIR, fn)}\n{str(args.get('content',''))[:300]}"):
            return "O usuário negou a criação do arquivo."
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, fn)
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(args.get("content", "")))
        show_step("Arquivo Criado", path)
        return f"Arquivo salvo em {path}."

    if name == "criar_ferramenta":
        nome = args.get("nome")
        if not nome or not args.get("codigo_acao"):
            return "Erro: 'nome' e 'codigo_acao' são obrigatórios."
        lang = (args.get("linguagem") or "python").lower()
        S["tools"] = [t for t in S["tools"] if t[0] != nome]
        S["tools"].append([nome, {"nome": nome, "descricao": args.get("descricao", ""),
                                  "codigo_acao": args["codigo_acao"], "linguagem": lang}])
        save_state()
        show_step("Ferramenta Dinâmica Criada", nome)
        return f'Ferramenta "{nome}" criada e salva (linguagem: {lang}). Será executada de verdade quando chamada.'

    tools = dict((n, t) for n, t in S["tools"])
    if name in tools:
        t = tools[name]
        lang = t.get("linguagem", "python")
        arg = args.get("argumento", args if args else None)
        arg_json = json.dumps(arg, ensure_ascii=False)
        if lang in ("javascript", "js"):
            code = f"const argumento = {arg_json};\n{t['codigo_acao']}"
        elif lang == "powershell":
            code = f"$argumento = '{arg_json.replace(chr(39), chr(39) * 2)}' | ConvertFrom-Json\n{t['codigo_acao']}"
        else:
            code = f"import json\nargumento = json.loads({json.dumps(arg_json)})\n{t['codigo_acao']}"
        if not confirm(f"rodar ferramenta dinâmica '{name}' ({lang})", code):
            return "O usuário negou a execução da ferramenta."
        show_step(f"Ferramenta Dinâmica: {name}", arg_json)
        return f'[Ferramenta dinâmica "{name}"]\n' + run_code(lang, code)

    if name == "consultar_teia":
        modo = str(args.get("modo") or "tudo").lower()
        alvo_q = args.get("no_id_ou_nome")
        show_step("Consulta Direta à Teia", f"{modo} {alvo_q or ''}")
        if modo == "tudo":
            return "[CONSULTA DIRETA]\n" + memory_summary()
        alvo = find_node(alvo_q)
        if not alvo:
            return f'[CONSULTA DIRETA] Nó "{alvo_q}" não encontrado na teia.'
        if modo == "no":
            return (f"[CONSULTA DIRETA] Nó: {alvo['label']} | Importância: {alvo['importance']} | "
                    f"Descrição: {alvo.get('description') or '(sem descrição)'}")
        links = S["graph"]["links"]
        if modo == "filhos":
            ks = [find_node(l["target"]) for l in links if l["source"] == alvo["id"]]
            ks = [k["label"] for k in ks if k]
            return (f'[CONSULTA DIRETA] Filhos diretos de "{alvo["label"]}": ' + ", ".join(ks)) if ks \
                else f'[CONSULTA DIRETA] "{alvo["label"]}" não tem filhos diretos.'
        if modo == "descendentes":
            seen, queue, res = {alvo["id"]}, [alvo["id"]], []
            while queue:
                cur = queue.pop(0)
                for l in links:
                    if l["source"] == cur and l["target"] not in seen:
                        seen.add(l["target"])
                        n = find_node(l["target"])
                        if n:
                            res.append(n["label"])
                        queue.append(l["target"])
            return (f'[CONSULTA DIRETA] Todos os descendentes de "{alvo["label"]}": ' + ", ".join(res)) if res \
                else f'[CONSULTA DIRETA] "{alvo["label"]}" não tem descendentes.'
        return f"[CONSULTA DIRETA] Modo \"{modo}\" não reconhecido. Use 'no', 'filhos', 'descendentes' ou 'tudo'."

    return f"Ferramenta desconhecida: {name}"


# ───────────────────────────── API (OpenRouter, streaming) ─────────────────────────────
def system_prompt():
    ch = active_chat()
    extra = f"\nInstruções Globais do Usuário: {cfg('extra')}" if cfg("extra") else ""
    return (f'Você é o Mini-Quark, rodando 100% em um terminal (CLI). Você está conversando em: "{ch["title"]}".\n'
            "Seja direto e objetivo. Você pode executar comandos com 'executar_terminal' — o resultado "
            "(exit code, stdout, stderr) volta para você. O usuário vê o texto puro, sem renderização de markdown pesada.\n\n"
            "Contexto Completo da Teia de Memória Persistente (estrutura hierárquica real, indentação = profundidade):\n"
            f"{memory_summary()}\n\n"
            "Respeite fielmente os níveis de profundidade e não invente ligações. Use 'consultar_teia' para confirmar "
            "hierarquia, filhos ou descendentes com certeza, e deixe claro ao usuário quando a resposta vem de consulta direta."
            f"{extra}")


# ── Ganchos de saída da resposta do modelo ─────────────────────────────────
# O CLI simples imprime o texto cru. Interfaces (como o mini_quark_tui.py) trocam estes
# três callbacks para renderizar a resposta como quiser (ex.: Markdown):
#   STREAM["start"]()      uma vez, antes do primeiro pedaço
#   STREAM["token"](txt)   a cada pedaço de texto que chega
#   STREAM["end"]()        uma vez, quando a resposta termina
def _stream_start():
    sys.stdout.write(green("◆ ") if USE_COLOR else "")


def _stream_token(txt):
    sys.stdout.write(txt)
    sys.stdout.flush()


def _stream_end():
    print()


STREAM = {"start": _stream_start, "token": _stream_token, "end": _stream_end}


def call_api(messages, stream_out=True):
    body = json.dumps({"model": cfg("model"), "messages": messages, "tools": all_tools(), "tool_choice": "auto",
                       "temperature": cfg("temperature"), "max_tokens": cfg("maxTokens"), "stream": True}).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key()}", "Content-Type": "application/json",
        "X-Title": "mini-quark-cli"})
    try:
        res = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        msg = f"Erro na API OpenRouter ({e.code})"
        try:
            j = json.loads(e.read().decode("utf-8", "replace"))
            msg = j.get("error", {}).get("message", msg)
        except Exception:
            pass
        raise RuntimeError(msg)
    except urllib.error.URLError as e:
        raise RuntimeError(f"Sem conexão com a OpenRouter: {e.reason}")

    content, tool_calls, printed = "", {}, False
    with res:
        for raw in res:
            line = raw.decode("utf-8", "replace").strip()
            if not line or line.startswith(":") or not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                j = json.loads(payload)
            except ValueError:
                continue
            if j.get("error"):
                raise RuntimeError(j["error"].get("message", "erro no stream"))
            ch = (j.get("choices") or [{}])[0]
            delta = ch.get("delta") or {}
            if delta.get("content"):
                if stream_out:
                    if not printed:
                        STREAM["start"]()
                        printed = True
                    STREAM["token"](delta["content"])
                content += delta["content"]
            for tc in delta.get("tool_calls") or []:
                i = tc.get("index", 0)
                slot = tool_calls.setdefault(i, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
    if printed:
        STREAM["end"]()
    msg = {"role": "assistant", "content": content or None}
    if tool_calls:
        msg["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
    return msg


def chat_turn(user_text):
    ch = active_chat()
    ch["history"].append({"role": "user", "content": user_text})
    save_state()
    messages = [{"role": "system", "content": system_prompt()}] + ch["history"]
    try:
        msg = call_api(messages)
        rounds = 0
        while msg.get("tool_calls") and rounds < MAX_TOOL_ROUNDS:
            rounds += 1
            messages.append(msg)
            for tc in msg["tool_calls"]:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                    result = execute_tool(tc["function"]["name"], args)
                except json.JSONDecodeError:
                    result = "Erro: os argumentos da chamada não são um JSON válido."
                except Exception as e:
                    result = f"Erro ao executar a ferramenta: {e}"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(result)})
            messages[0]["content"] = system_prompt()
            msg = call_api(messages)
        reply = msg.get("content") or "Ação concluída."
        if rounds and not msg.get("content"):
            print(green("◆ ") + reply)
        ch["history"].append({"role": "assistant", "content": reply})
        save_state()
        return reply
    except KeyboardInterrupt:
        print(red("\n[interrompido]"))
    except RuntimeError as e:
        print(red(f"\n⚠ Erro: {e}"))
        ch["history"].pop() if ch["history"] and ch["history"][-1]["role"] == "user" else None
        save_state()
    return None


# ───────────────────────────── comandos / ─────────────────────────────
HELP = f"""{bold('Comandos')}
  /help                    esta ajuda
  /exit  (ou Ctrl+D)       sair

{bold('Conversas')}
  /chats                   lista as conversas
  /new [título]            nova conversa
  /switch <n|id>           troca de conversa
  /rename <título>         renomeia a atual
  /delete [n|id]           apaga (a atual por padrão)
  /history [n]             mostra as últimas n mensagens (padrão 10)
  /clear                   limpa o histórico da conversa atual

{bold('Teia de memória')}
  /graph                   mostra a árvore
  /remember rótulo | descrição | imp   adiciona nó sob o selecionado
  /select <id|rótulo>      seleciona o nó-pai das próximas memórias
  /forget <id|rótulo>      remove um nó (e suas ligações)

{bold('Ferramentas')}
  /tools                   lista ferramentas dinâmicas
  /rmtool <nome>           remove uma
  /auto                    liga/desliga execução sem confirmação (cuidado!)

{bold('Configuração')}
  /key [chave]             define a API key (ou usa $OPENROUTER_API_KEY)
  /model [id]              mostra/troca o modelo
  /temp <0-2>   /tokens <n>   /extra <instruções globais>
  /config                  mostra a configuração

{bold('Arquivos')}
  /attach <caminho>        anexa um arquivo de texto à próxima mensagem
  /export [arquivo]        backup JSON (compatível com o mini_quark.html)
  /import <arquivo>        restaura backup (do CLI ou do navegador)

Dica: termine a linha com \\ para continuar em várias linhas."""

ATTACH = []


def pick_chat(arg):
    if not arg:
        return None
    if arg.isdigit() and 1 <= int(arg) <= len(S["chats"]):
        return S["chats"][int(arg) - 1]
    return next((c_ for c_ in S["chats"] if c_["id"] == arg or c_["title"].lower() == arg.lower()), None)


def handle_command(line):
    global AUTO
    cmd, _, arg = line[1:].partition(" ")
    cmd, arg = cmd.lower(), arg.strip()

    if cmd in ("exit", "quit", "q"):
        raise EOFError
    if cmd in ("help", "h", "?"):
        print(HELP)
    elif cmd == "chats":
        for i, ch in enumerate(S["chats"], 1):
            mark = green("●") if ch["id"] == S["activeChatId"] else " "
            n_msgs = len(ch["history"])
            print(f" {mark} {i}. {ch['title']} " + dim(f"({n_msgs} msgs)"))
    elif cmd == "new":
        ch = {"id": f"chat_{int(time.time() * 1000)}", "title": arg or f"Conversa {len(S['chats']) + 1}", "history": []}
        S["chats"].append(ch)
        S["activeChatId"] = ch["id"]
        save_state()
        print(green(f"Nova conversa: {ch['title']}"))
    elif cmd == "switch":
        ch = pick_chat(arg)
        if not ch:
            print(red("Conversa não encontrada. Use /chats."))
        else:
            S["activeChatId"] = ch["id"]
            save_state()
            print(green(f"Conversa: {ch['title']}"))
    elif cmd == "rename":
        if arg:
            active_chat()["title"] = arg
            save_state()
            print(green("Renomeada."))
    elif cmd == "delete":
        ch = pick_chat(arg) if arg else active_chat()
        if not ch:
            print(red("Conversa não encontrada."))
        else:
            S["chats"].remove(ch)
            if not S["chats"]:
                S["chats"] = default_state()["chats"]
            if S["activeChatId"] == ch["id"]:
                S["activeChatId"] = S["chats"][0]["id"]
            save_state()
            print(green(f"Apagada. Agora em: {active_chat()['title']}"))
    elif cmd == "history":
        n = int(arg) if arg.isdigit() else 10
        for m in active_chat()["history"][-n:]:
            who = blue("você") if m["role"] == "user" else green("mini-quark")
            print(f"{who}: {m['content']}\n")
    elif cmd == "clear":
        active_chat()["history"] = []
        save_state()
        print(green("Histórico limpo."))
    elif cmd == "graph":
        print_graph()
    elif cmd == "remember":
        parts = [p.strip() for p in arg.split("|")]
        if not parts[0]:
            print(red("Uso: /remember rótulo | descrição | importância(1-5)"))
        else:
            n = add_node(parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else 2)
            print(green(f"Nó criado: {n['label']}"))
    elif cmd == "select":
        n = find_node(arg)
        if n:
            S["selected"] = n["id"]
            save_state()
            print(green(f"Selecionado: {n['label']}"))
        else:
            print(red("Nó não encontrado."))
    elif cmd == "forget":
        n = find_node(arg)
        if not n:
            print(red("Nó não encontrado."))
        elif not remove_node(n):
            print(red("O nó raiz não pode ser removido."))
        else:
            print(green(f"Removido: {n['label']}"))
    elif cmd == "tools":
        if not S["tools"]:
            print(dim("Nenhuma ferramenta dinâmica ainda. A IA pode criar sob demanda."))
        for name, t in S["tools"]:
            print(f"  {bold(name)} {dim('[' + t.get('linguagem', 'python') + ']')} — {t.get('descricao', '')}")
    elif cmd == "rmtool":
        before = len(S["tools"])
        S["tools"] = [t for t in S["tools"] if t[0] != arg]
        save_state()
        print(green("Removida.") if len(S["tools"]) < before else red("Não encontrada."))
    elif cmd == "auto":
        S["config"]["auto"] = not cfg("auto")
        AUTO = False
        save_state()
        print(amber("Execução automática LIGADA — o modelo roda comandos sem perguntar.") if cfg("auto")
              else green("Confirmação manual ativada."))
    elif cmd == "key":
        if not arg:
            import getpass
            arg = getpass.getpass("API key (oculta): ").strip()
        S["config"]["apiKey"] = arg
        save_state()
        print(green("Chave salva em " + STATE_FILE))
    elif cmd == "model":
        if arg:
            S["config"]["model"] = arg
            save_state()
        print(f"Modelo: {bold(cfg('model'))}")
    elif cmd == "temp":
        try:
            S["config"]["temperature"] = max(0.0, min(2.0, float(arg)))
            save_state()
        except ValueError:
            print(red("Uso: /temp 0.7"))
    elif cmd == "tokens":
        if arg.isdigit():
            S["config"]["maxTokens"] = int(arg)
            save_state()
        else:
            print(red("Uso: /tokens 4096"))
    elif cmd == "extra":
        S["config"]["extra"] = arg
        save_state()
        print(green("Instruções globais atualizadas." if arg else "Instruções globais removidas."))
    elif cmd == "config":
        c_ = dict(S["config"])
        c_["apiKey"] = ("…" + c_["apiKey"][-4:]) if c_["apiKey"] else ("(via env)" if os.environ.get("OPENROUTER_API_KEY") else "(não definida)")
        for k, v in c_.items():
            print(f"  {k:12} {v}")
        print(dim(f"  estado em    {STATE_FILE}"))
    elif cmd == "attach":
        path = os.path.expanduser(arg.strip('"\''))
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                ATTACH.append({"name": os.path.basename(path), "content": f.read()})
            print(green(f"Anexado: {os.path.basename(path)} ({len(ATTACH)} pendente(s))"))
        except OSError as e:
            print(red(f"Não consegui ler: {e}"))
    elif cmd == "export":
        path = arg or "mini-quark-backup.json"
        data = {k: S[k] for k in ("v", "chats", "activeChatId", "graph")}
        data["tools"] = S["tools"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(green(f"Backup salvo em {path} (sem a API key)."))
    elif cmd == "import":
        try:
            with open(os.path.expanduser(arg), encoding="utf-8") as f:
                j = json.load(f)
            if j.get("chats"):
                S["chats"] = j["chats"]
            if j.get("activeChatId"):
                S["activeChatId"] = j["activeChatId"]
            if j.get("graph", {}).get("nodes"):
                S["graph"] = j["graph"]
                S["graph"].setdefault("links", [])
                S["selected"] = "root"
            if isinstance(j.get("tools"), list):
                S["tools"] = [t for t in j["tools"] if isinstance(t, list) and len(t) == 2]
            save_state()
            print(green("Backup importado."))
        except (OSError, ValueError) as e:
            print(red(f"Arquivo inválido: {e}"))
    else:
        print(red(f"Comando desconhecido: /{cmd}. Digite /help."))


# ───────────────────────────── main ─────────────────────────────
def read_input(prompt):
    lines = []
    while True:
        line = input(prompt if not lines else dim("… "))
        if line.endswith("\\"):
            lines.append(line[:-1])
            continue
        lines.append(line)
        return "\n".join(lines)


def build_user_text(text):
    if not ATTACH:
        return text
    blocks = "\n\n".join(f"Ficheiro: {a['name']}\nConteúdo:\n{a['content']}" for a in ATTACH)
    ATTACH.clear()
    return f"{text}\n\n--- [Ficheiros Anexados] ---\n{blocks}"


def main():
    global AUTO
    ap = argparse.ArgumentParser(description="mini-quark CLI")
    ap.add_argument("-p", "--prompt", help="envia uma mensagem, imprime a resposta e sai")
    ap.add_argument("-c", "--chat", help="conversa (número, id ou título) a usar")
    ap.add_argument("-m", "--model", help="modelo para esta sessão")
    ap.add_argument("-y", "--yes", action="store_true", help="não pedir confirmação para executar ferramentas")
    args = ap.parse_args()

    load_state()
    if args.model:
        S["config"]["model"] = args.model
    if args.yes:
        AUTO = True
    if args.chat:
        ch = pick_chat(args.chat)
        if ch:
            S["activeChatId"] = ch["id"]

    if args.prompt is not None:
        if not api_key():
            sys.exit("Defina OPENROUTER_API_KEY ou use /key no modo interativo.")
        if not sys.stdin.isatty() and not AUTO:
            print(amber("(stdin não interativo: ferramentas que executam código serão negadas sem --yes)"), file=sys.stderr)
        sys.exit(0 if chat_turn(args.prompt) is not None else 1)

    print(f"{green('◆')} {bold('mini-quark')} {dim('CLI · /help para comandos')}")
    print(dim(f"  modelo: {cfg('model')} · conversa: {active_chat()['title']}"))
    if not api_key():
        print(amber("  Sem API key. Use /key ou exporte OPENROUTER_API_KEY."))
    if cfg("auto"):
        print(amber("  ⚠ Execução automática de ferramentas está LIGADA (/auto para desligar)."))

    while True:
        try:
            tag = f" +{len(ATTACH)}📎" if ATTACH else ""
            text = read_input(blue(f"\n{active_chat()['title']}{tag} ❯ ")).strip()
        except EOFError:
            print(dim("\nAté mais."))
            break
        except KeyboardInterrupt:
            print(dim("\n(Ctrl+C — use /exit ou Ctrl+D para sair)"))
            continue
        if not text:
            continue
        if text.startswith("/"):
            try:
                handle_command(text)
            except EOFError:
                print(dim("Até mais."))
                break
            except KeyboardInterrupt:
                print()
            continue
        if not api_key():
            print(red("Configure a API key: /key"))
            continue
        chat_turn(build_user_text(text))


if __name__ == "__main__":
    main()