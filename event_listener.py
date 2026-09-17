"""
Servidor HTTP simples para inspecionar os eventos que dispositivos Hikvision
(câmeras LPR/ANPR, terminais de reconhecimento facial, controladoras de
acesso, etc.) enviam via ISAPI Listening (POST HTTP, multipart quando
"Upload Binary Image" está ativado no dispositivo).

Serve só para OBSERVAR o formato/conteúdo dos eventos ao cadastrar este
servidor em cada dispositivo — não decide nada nem aciona nada de volta.

Uso:
    python event_listener.py [porta]

Padrão: porta 8000, ou a variável de ambiente PORT (usada em produção/Render).

No dispositivo, configure em Configuration > Rede > Ligação de dados > ISAPI
Listening (o caminho exato varia por modelo/firmware):
    IP/Domínio    : <IP/domínio deste servidor>
    Porta         : 8000  (ou a porta que você passar como argumento/PORT)
    URL anfitrião : /evento   (qualquer path funciona, EXCETO "/", "/stream",
                                "/events.json" e "/recebidos/*", reservados
                                para a página de visualização ao vivo abaixo)

Todo POST/GET recebido é logado no terminal com headers e corpo. Quando o
corpo é multipart/form-data, cada parte é salva em recebidos/: texto
(XML/JSON) é impresso e salvo como .txt/.xml; imagens (quando "Upload Binary
Image" está ligado) são salvas como .jpg/.png. Partes em XML também têm seus
campos "achatados" (sem namespace) impressos como JSON, pra facilitar ver a
estrutura do evento de cada tipo de dispositivo.

Abrindo a URL raiz ("/") num navegador, você vê os eventos chegando ao vivo
(via Server-Sent Events), sem precisar dar refresh — útil pra acompanhar o
cadastro de cada dispositivo sem depender dos logs do Render. Os eventos são
agrupados pelo IP interno do equipamento (extraído de dentro do próprio XML
do evento, não da conexão TCP — na Render, vários dispositivos atrás do
mesmo roteador chegam com o mesmo IP de conexão): clique num "chip" de
dispositivo pra filtrar só os eventos dele. Cada dispositivo e tipo de
evento (ANPR, heartBeat, etc.) ganham uma cor pra facilitar identificar.

Os arquivos salvos em recebidos/ são apagados automaticamente (por idade e
por tamanho total — veja LIMPEZA_* abaixo), pra não estourar o disco do
plano Free do Render.
"""

import sys
import os
import re
import json
import queue
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMAGE_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}

SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recebidos")
os.makedirs(SAVE_DIR, exist_ok=True)

# Limpeza automática de recebidos/, pra não estourar o disco do plano Free
# do Render: apaga o que passou da idade máxima e, se mesmo assim o total
# ainda passar do tamanho máximo, apaga do arquivo mais antigo pro mais novo.
LIMPEZA_INTERVALO_SEGUNDOS = 600
LIMPEZA_IDADE_MAXIMA_SEGUNDOS = 6 * 3600
LIMPEZA_TAMANHO_MAXIMO_BYTES = 200 * 1024 * 1024


def limpar_recebidos():
    agora = time.time()
    try:
        arquivos = []
        for nome in os.listdir(SAVE_DIR):
            caminho = os.path.join(SAVE_DIR, nome)
            try:
                stat = os.stat(caminho)
            except OSError:
                continue
            if os.path.isfile(caminho):
                arquivos.append((caminho, stat.st_mtime, stat.st_size))

        restantes = []
        for caminho, mtime, tamanho in arquivos:
            if agora - mtime > LIMPEZA_IDADE_MAXIMA_SEGUNDOS:
                os.remove(caminho)
            else:
                restantes.append((caminho, mtime, tamanho))

        restantes.sort(key=lambda item: item[1])  # mais antigo primeiro
        total = sum(tamanho for _, _, tamanho in restantes)
        i = 0
        while total > LIMPEZA_TAMANHO_MAXIMO_BYTES and i < len(restantes):
            caminho, _, tamanho = restantes[i]
            os.remove(caminho)
            total -= tamanho
            i += 1
    except Exception as e:
        print(f"Falha na limpeza de {SAVE_DIR}: {e}")


def loop_limpeza():
    while True:
        limpar_recebidos()
        time.sleep(LIMPEZA_INTERVALO_SEGUNDOS)


# Buffer dos últimos eventos (pra quem abrir a página depois de eventos já
# terem chegado) + assinantes conectados agora via SSE, pra ver ao vivo.
MAX_EVENTS = 200
STATE_LOCK = threading.Lock()
EVENTS = []
SUBSCRIBERS = set()


def broadcast(evento: dict):
    """Guarda o evento no buffer e empurra pra quem estiver com a página
    de visualização ao vivo aberta agora."""
    with STATE_LOCK:
        EVENTS.append(evento)
        if len(EVENTS) > MAX_EVENTS:
            del EVENTS[: len(EVENTS) - MAX_EVENTS]
        subscribers = list(SUBSCRIBERS)
    for q in subscribers:
        q.put(evento)


def flatten_xml(xml_text: str) -> dict:
    """Achata um XML de evento (EventNotificationAlert ou similar) num dict
    simples {tag: valor}, ignorando namespaces. Genérico o suficiente pra
    qualquer tipo de evento Hikvision (ANPR, facial, alarme, etc.) — não
    assume nenhum campo específico.
    """
    def strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    flat = {}
    for elem in root.iter():
        tag = strip_ns(elem.tag)
        if tag in flat:
            continue  # primeira ocorrência vence (evita que entradas
            # repetidas dentro de listas sobrescrevam o valor principal)
        if elem.text and elem.text.strip() and len(list(elem)) == 0:
            flat[tag] = elem.text.strip()
    return flat


def parse_multipart(body: bytes, boundary: bytes):
    """Parser manual e simples de multipart/form-data.
    Retorna uma lista de dicts: {"name": str, "filename": str|None,
    "content_type": str|None, "data": bytes}
    """
    parts = []
    delimiter = b"--" + boundary
    # Divide o corpo pelos delimitadores, ignorando o preâmbulo e o epílogo final
    chunks = body.split(delimiter)
    for chunk in chunks:
        chunk = chunk.strip(b"\r\n")
        if not chunk or chunk == b"--":
            continue
        if b"\r\n\r\n" not in chunk:
            continue
        header_blob, data = chunk.split(b"\r\n\r\n", 1)
        data = data.rstrip(b"\r\n")
        headers_text = header_blob.decode("utf-8", errors="replace")

        name_match = re.search(r'name="([^"]*)"', headers_text)
        filename_match = re.search(r'filename="([^"]*)"', headers_text)
        ctype_match = re.search(r"Content-Type:\s*([^\r\n]+)", headers_text, re.IGNORECASE)

        parts.append({
            "name": name_match.group(1) if name_match else None,
            "filename": filename_match.group(1) if filename_match else None,
            "content_type": ctype_match.group(1).strip() if ctype_match else None,
            "data": data,
        })
    return parts


VIEWER_HTML = """<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Event Listener - ao vivo</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font-family: ui-monospace, Menlo, Consolas, monospace; background:#0b0f14; color:#d7e0ea; }
  header { position:sticky; top:0; background:#111820; padding:10px 16px; border-bottom:1px solid #24303c;
           z-index:1; }
  .header-top { display:flex; align-items:center; gap:10px; }
  header h1 { font-size:14px; font-weight:600; margin:0; }
  #status { font-size:12px; padding:2px 8px; border-radius:10px; }
  #status.ok { background:#123d24; color:#5fd58a; }
  #status.down { background:#3d1212; color:#e37a7a; }
  #dispositivos { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .chip { font: inherit; font-size:12px; color:#a9bbcc; background:#182430; border:1px solid #24303c;
          border-radius:999px; padding:4px 10px 4px 8px; cursor:pointer; display:inline-flex; align-items:center; gap:6px; }
  .chip:hover { border-color:#3a4c60; }
  .chip.ativo { background:#1b3550; border-color:#3f7dc9; color:#cfe3fb; }
  .bolinha { width:8px; height:8px; border-radius:50%; flex:none; }
  main { padding:12px 16px 40px; max-width:900px; margin:0 auto; }
  .evento { border:1px solid #24303c; border-left-width:4px; border-radius:8px; padding:10px 12px;
            margin-bottom:10px; background:#0f151c; }
  .evento .meta { color:#7ea0c2; font-size:12px; margin-bottom:6px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .badge-tipo { font-size:11px; font-weight:600; padding:1px 8px; border-radius:999px; color:#fff; }
  .evento pre { white-space: pre-wrap; word-break: break-word; margin:0; font-size:12.5px; line-height:1.4; }
  .evento .imagens { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
  .evento .imagens img { max-width:220px; max-height:220px; border-radius:6px; border:1px solid #24303c; }
  #vazio { color:#5c6b7a; padding:20px 0; }
</style>
</head>
<body>
<header>
  <div class="header-top">
    <h1>Event Listener &mdash; ao vivo</h1>
    <span id="status" class="down">conectando&hellip;</span>
  </div>
  <div id="dispositivos"></div>
</header>
<main>
  <div id="vazio">Aguardando eventos&hellip;</div>
  <div id="log"></div>
</main>
<script>
  const log = document.getElementById('log');
  const vazio = document.getElementById('vazio');
  const status = document.getElementById('status');
  const dispositivos = document.getElementById('dispositivos');

  let filtro = null; // null = mostra todos; string = só esse dispositivo
  const contagem = {}; // dispositivo -> quantidade de eventos vistos

  const CORES_TIPO = {
    ANPR: '#3f7dc9',
    heartBeat: '#4b5a68',
    facedetection: '#a855f7',
    facerecognition: '#a855f7',
    alarm: '#e5484d',
    videoloss: '#e5484d',
  };

  function corDoDispositivo(id) {
    let hash = 0;
    for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) >>> 0;
    return `hsl(${hash % 360}, 65%, 55%)`;
  }

  function corDoTipo(tipo) {
    if (!tipo) return '#3a4c60';
    return CORES_TIPO[tipo] || CORES_TIPO[tipo.toLowerCase()] || '#3f9f7a';
  }

  function chipDoDispositivo(id) {
    return dispositivos.querySelector(`[data-dispositivo="${CSS.escape(id)}"]`);
  }

  function selecionarFiltro(id) {
    filtro = (filtro === id) ? null : id;
    dispositivos.querySelectorAll('.chip[data-dispositivo]').forEach(btn => {
      btn.classList.toggle('ativo', btn.dataset.dispositivo === filtro);
    });
    todosBtn.classList.toggle('ativo', !filtro);
    document.querySelectorAll('.evento').forEach(div => {
      div.style.display = (!filtro || div.dataset.dispositivo === filtro) ? '' : 'none';
    });
  }

  const todosBtn = document.createElement('button');
  todosBtn.className = 'chip ativo';
  todosBtn.textContent = 'Todos';
  todosBtn.onclick = () => selecionarFiltro(null);
  dispositivos.appendChild(todosBtn);

  function registrarDispositivo(id) {
    contagem[id] = (contagem[id] || 0) + 1;
    let chip = chipDoDispositivo(id);
    if (!chip) {
      chip = document.createElement('button');
      chip.className = 'chip';
      chip.dataset.dispositivo = id;
      chip.onclick = () => selecionarFiltro(id);
      const bolinha = document.createElement('span');
      bolinha.className = 'bolinha';
      bolinha.style.background = corDoDispositivo(id);
      chip.appendChild(bolinha);
      chip.appendChild(document.createTextNode(''));
      dispositivos.appendChild(chip);
    }
    chip.lastChild.textContent = `${id} (${contagem[id]})`;
  }

  function pertoDoFim() {
    return (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 80);
  }

  function addEvento(ev) {
    vazio.style.display = 'none';
    registrarDispositivo(ev.dispositivo);
    const deveRolar = pertoDoFim();

    const div = document.createElement('div');
    div.className = 'evento';
    div.dataset.dispositivo = ev.dispositivo;
    div.style.borderLeftColor = corDoDispositivo(ev.dispositivo);
    if (filtro && ev.dispositivo !== filtro) div.style.display = 'none';

    const meta = document.createElement('div');
    meta.className = 'meta';
    if (ev.tipo) {
      const badge = document.createElement('span');
      badge.className = 'badge-tipo';
      badge.style.background = corDoTipo(ev.tipo);
      badge.textContent = ev.tipo;
      meta.appendChild(badge);
    }
    meta.appendChild(document.createTextNode(
      `[${ev.ts}] ${ev.metodo} ${ev.path}  — ${ev.dispositivo}`
    ));

    const pre = document.createElement('pre');
    pre.textContent = ev.texto;
    div.appendChild(meta);
    div.appendChild(pre);
    if (ev.imagens && ev.imagens.length) {
      const imgs = document.createElement('div');
      imgs.className = 'imagens';
      for (const fname of ev.imagens) {
        const a = document.createElement('a');
        a.href = '/recebidos/' + encodeURIComponent(fname);
        a.target = '_blank';
        const img = document.createElement('img');
        img.src = '/recebidos/' + encodeURIComponent(fname);
        img.loading = 'lazy';
        img.alt = fname;
        a.appendChild(img);
        imgs.appendChild(a);
      }
      div.appendChild(imgs);
    }
    log.appendChild(div);
    if (deveRolar) window.scrollTo(0, document.body.scrollHeight);
  }

  function connect() {
    const es = new EventSource('/stream');
    es.onopen = () => { status.textContent = 'ao vivo'; status.className = 'ok'; };
    es.onerror = () => { status.textContent = 'reconectando…'; status.className = 'down'; };
    es.onmessage = (e) => {
      try { addEvento(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
  }
  connect();
</script>
</body>
</html>
""".encode("utf-8")


class EventHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # Página de visualização ao vivo (rotas reservadas)
    # ------------------------------------------------------------------

    def _serve_viewer(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(VIEWER_HTML)))
        self.end_headers()
        self.wfile.write(VIEWER_HTML)

    def _serve_recebido(self):
        fname = os.path.basename(urllib.parse.unquote(self.path[len("/recebidos/"):].split("?", 1)[0]))
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
        fpath = os.path.join(SAVE_DIR, fname)
        if not fname or ext not in IMAGE_CONTENT_TYPES or not os.path.isfile(fpath):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(fpath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", IMAGE_CONTENT_TYPES[ext])
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_events_json(self):
        with STATE_LOCK:
            backlog = list(EVENTS)
        body = json.dumps(backlog, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_chunk(self, data: bytes):
        # Transfer-Encoding: chunked manual, pra manter a conexão aberta
        # (sem Content-Length) de um jeito compatível com proxies (Render).
        size = f"{len(data):x}".encode()
        self.wfile.write(size + b"\r\n" + data + b"\r\n")
        self.wfile.flush()

    def _serve_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        q = queue.Queue()
        with STATE_LOCK:
            backlog = list(EVENTS)
            SUBSCRIBERS.add(q)
        try:
            for evento in backlog:
                self._sse_chunk(b"data: " + json.dumps(evento, ensure_ascii=False).encode("utf-8") + b"\n\n")
            while True:
                try:
                    evento = q.get(timeout=15)
                    self._sse_chunk(b"data: " + json.dumps(evento, ensure_ascii=False).encode("utf-8") + b"\n\n")
                except queue.Empty:
                    self._sse_chunk(b": ping\n\n")  # mantém a conexão viva
        except Exception:
            pass  # cliente desconectou
        finally:
            with STATE_LOCK:
                SUBSCRIBERS.discard(q)

    # ------------------------------------------------------------------
    # Recepção dos eventos dos dispositivos
    # ------------------------------------------------------------------

    def _out(self, text=""):
        print(text)
        self._lines.append(text)

    def _log_common(self):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._out("\n" + "=" * 70)
        self._out(f"[{ts}] {self.command} {self.path}  de {self.client_address[0]}")
        self._out("-" * 70)
        self._out("Headers:")
        for k, v in self.headers.items():
            self._out(f"  {k}: {v}")
        self._out("-" * 70)
        return ts

    def _handle(self):
        self._lines = []
        self._imagens = []
        self._dispositivo = None
        self._tipo = None
        ts = self._log_common()
        length = int(self.headers.get("Content-Length", 0))
        content_type = self.headers.get("Content-Type", "")

        if length == 0:
            self._out("(sem corpo)")
        elif content_type.startswith("multipart/form-data"):
            # Payload multipart: normalmente contém o XML/JSON do evento
            # e a(s) imagem(ns) capturada(s), quando "Upload Binary Image"
            # está ativado no dispositivo.
            boundary_match = re.search(r"boundary=(.+)$", content_type)
            if not boundary_match:
                self._out("(multipart sem boundary identificável, corpo bruto abaixo)")
                self._out(str(self.rfile.read(length)))
            else:
                boundary = boundary_match.group(1).strip().strip('"').encode("utf-8")
                body = self.rfile.read(length)
                for i, part in enumerate(parse_multipart(body, boundary)):
                    name = part["name"] or f"campo_{i}"
                    data = part["data"]
                    is_jpeg = data[:2] == b"\xff\xd8"
                    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
                    looks_like_text = (part["filename"] or "").lower().endswith((".xml", ".json", ".txt")) or (
                        not is_jpeg and not is_png and data[:1] in (b"<", b"{")
                    )

                    if looks_like_text and not is_jpeg and not is_png:
                        # XML/JSON/texto: imprime formatado no terminal E salva um .txt/.xml pra referência
                        text = data.decode("utf-8", errors="replace")
                        fname = part["filename"] or f"{name}.xml"
                        fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{fname}"
                        fpath = os.path.join(SAVE_DIR, fname)
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write(text)
                        self._out(f"Campo '{name}' (texto, salvo em {fpath}):\n{text}\n")

                        # Se parecer XML, achata os campos e mostra como JSON
                        # pra facilitar ver a estrutura do evento.
                        if (part["filename"] or "").lower().endswith(".xml") or text.lstrip().startswith("<"):
                            flat = flatten_xml(text)
                            if flat:
                                self._out("Campos do evento (achatado, sem namespace):")
                                self._out(json.dumps(flat, indent=2, ensure_ascii=False))
                                self._out("")
                                # Identifica o dispositivo pelo IP/MAC de dentro do
                                # próprio XML — na Render, o IP da conexão TCP é o do
                                # proxy (ou o IP público compartilhado pelo roteador
                                # de todos os dispositivos da mesma rede local).
                                if self._dispositivo is None:
                                    self._dispositivo = flat.get("ipAddress") or flat.get("macAddress")
                                if self._tipo is None:
                                    self._tipo = flat.get("eventType")
                    else:
                        ext = "jpg" if is_jpeg else ("png" if is_png else "bin")
                        base = part["filename"] or f"{name}.{ext}"
                        fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{base}"
                        fpath = os.path.join(SAVE_DIR, fname)
                        with open(fpath, "wb") as f:
                            f.write(data)
                        if is_jpeg or is_png:
                            self._imagens.append(fname)
                        self._out(f"Campo '{name}': imagem salva em {fpath} ({len(data)} bytes)")
        else:
            # Corpo simples (JSON, XML ou texto puro)
            body = self.rfile.read(length)
            try:
                text = body.decode("utf-8", errors="replace")
            except Exception:
                text = repr(body)
            self._out("Corpo:")
            self._out(text)

            if text.lstrip().startswith("<"):
                flat = flatten_xml(text)
                self._dispositivo = flat.get("ipAddress") or flat.get("macAddress")
                self._tipo = flat.get("eventType")

        self._out("=" * 70 + "\n")

        broadcast({
            "ts": ts,
            "metodo": self.command,
            "path": self.path,
            "ip": self.client_address[0],
            "dispositivo": self._dispositivo or self.client_address[0],
            "tipo": self._tipo,
            "texto": "\n".join(self._lines),
            "imagens": self._imagens,
        })

        # A Hikvision espera a resposta EXATA "HTTP/1.1 200 " (com espaço após o
        # 200, sem "OK"). Sem isso, alguns firmwares reenviam o mesmo evento
        # repetidamente por não reconhecerem a confirmação de recebimento.
        self.send_response_only(200, "")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self._handle()

    def do_GET(self):
        if self.path == "/":
            self._serve_viewer()
        elif self.path.startswith("/stream"):
            self._serve_stream()
        elif self.path.startswith("/events.json"):
            self._serve_events_json()
        elif self.path.startswith("/recebidos/"):
            self._serve_recebido()
        elif self.path in ("/favicon.ico", "/robots.txt"):
            # O navegador busca isso sozinho ao abrir "/" — responde sem
            # logar/mostrar como se fosse um evento de dispositivo.
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._handle()

    def log_message(self, format, *args):
        # Silencia o log padrão (já fazemos log customizado acima)
        pass


def main():
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        # O Render (e outras plataformas PaaS) definem a porta via variável
        # de ambiente PORT; localmente cai no padrão 8000.
        port = int(os.environ.get("PORT", "8000"))
    # ThreadingHTTPServer: a página de visualização ao vivo mantém uma
    # conexão aberta (SSE), então precisa de mais de uma thread pra não
    # travar o recebimento de eventos dos dispositivos enquanto isso.
    server = ThreadingHTTPServer(("0.0.0.0", port), EventHandler)
    threading.Thread(target=loop_limpeza, daemon=True).start()
    print(f"Escutando em http://0.0.0.0:{port}  (Ctrl+C para parar)")
    print(f"Visualização ao vivo em: http://0.0.0.0:{port}/")
    print(f"Arquivos recebidos serão salvos em: {SAVE_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando...")
        server.server_close()


if __name__ == "__main__":
    main()
