"""
Servidor HTTP genérico para inspecionar eventos recebidos via webhook — POST
(ou GET) com corpo JSON, XML ou multipart/form-data (com imagens), de
qualquer dispositivo ou sistema, em qualquer path.

Tem tratamento especial para eventos no formato ISAPI da Hikvision (câmeras
LPR/ANPR, terminais de reconhecimento facial, controladoras de acesso,
etc.): o XML é achatado em JSON e o dispositivo de origem/resumo são
extraídos automaticamente. Qualquer outro tipo de payload continua sendo
logado normalmente, só sem esse enriquecimento extra.

Serve só para OBSERVAR o formato/conteúdo dos eventos ao cadastrar este
servidor onde for preciso testar uma integração — não decide nada nem aciona
nada de volta.

O ACK ("HTTP/1.1 200 ", exigido pela Hikvision) é enviado assim que o corpo
da requisição termina de chegar, ANTES de logar/gravar/processar qualquer
coisa — dispositivos (ou proxies no caminho) costumam reenviar o mesmo
evento por timeout se a resposta demorar, o que aparece como eventos
"duplicados" que na real são retries do mesmo evento original. Como
segunda camada de proteção, eventos Hikvision com o mesmo <UUID> de um
evento processado há pouco (retry de verdade, não uma nova detecção) são
descartados: nada é salvo em disco nem aparece na página — veja UUID_* mais
abaixo.

Uso:
    python event_listener.py [porta]

Padrão: porta 8000, ou a variável de ambiente PORT quando ela existir
(convenção comum na maioria das plataformas de deploy).

Configure no dispositivo/sistema de origem a URL deste servidor + um path
qualquer, por exemplo:
    IP/Domínio    : <IP/domínio deste servidor>
    Porta         : 8000  (ou a porta que você passar como argumento/PORT)
    URL/Path      : /evento   (qualquer path funciona, EXCETO "/", "/stream",
                                "/events.json", "/recebidos/*" e
                                "/reenviar/*", reservados para a página de
                                visualização ao vivo abaixo)

Todo POST/GET recebido é logado no terminal com headers e corpo. Quando o
corpo é multipart/form-data, cada parte é salva em recebidos/: texto
(XML/JSON) é impresso e salvo como .txt/.xml; imagens (quando "Upload Binary
Image" está ligado, no caso Hikvision) são salvas como .jpg/.png. Partes em
XML também têm seus campos "achatados" (sem namespace) impressos como JSON,
pra facilitar ver a estrutura do evento de cada tipo de dispositivo.

Abrindo a URL raiz ("/") num navegador, você vê os eventos chegando ao vivo
(via Server-Sent Events), sem precisar dar refresh. Os eventos são
agrupados pelo IP interno do equipamento (extraído de dentro do próprio XML
do evento, não da conexão TCP — atrás de proxy/CDN, vários dispositivos da
mesma rede costumam chegar com o mesmo IP de conexão): clique num "chip" de
dispositivo pra filtrar só os eventos dele. Cada dispositivo e tipo de
evento (ANPR, heartBeat, etc.) ganham uma cor pra facilitar identificar.

Os arquivos salvos em recebidos/ (e o corpo bruto em recebidos_raw/, usado
pelo botão "Reenviar…") são apagados automaticamente (por idade e por
tamanho total — veja LIMPEZA_* abaixo), pra não depender de armazenamento
permanente em ambientes com disco efêmero.

A página também tem: Pausar/Retomar (congela a tela sem perder eventos),
Limpar tela, download de cada XML/JSON recebido, e "Reenviar…" pra reenviar
o payload original de um evento pra uma URL escolhida na hora (útil pra
testar o backend real sem precisar acionar o dispositivo de novo — recusa
URLs que apontem pra rede interna).
"""

import sys
import os
import re
import json
import queue
import socket
import ipaddress
import threading
import time
import uuid
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IMAGEM_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
}
TEXTO_CONTENT_TYPES = {
    "xml": "application/xml; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
}

SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recebidos")
os.makedirs(SAVE_DIR, exist_ok=True)

# Corpo bruto de cada requisição (pra função "reenviar evento"), guardado à
# parte de recebidos/ pra não ficar exposto publicamente via /recebidos/.
RAW_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recebidos_raw")
os.makedirs(RAW_DIR, exist_ok=True)

# Limpeza automática de recebidos/ e recebidos_raw/, pra não estourar o disco
# em ambientes com armazenamento limitado: apaga o que passou da idade
# máxima e, se mesmo assim o total ainda passar do tamanho máximo, apaga do
# mais antigo pro mais novo.
LIMPEZA_INTERVALO_SEGUNDOS = 600
LIMPEZA_IDADE_MAXIMA_SEGUNDOS = 6 * 3600
LIMPEZA_TAMANHO_MAXIMO_BYTES = 200 * 1024 * 1024


def limpar_pasta(pasta):
    agora = time.time()
    try:
        arquivos = []
        for nome in os.listdir(pasta):
            caminho = os.path.join(pasta, nome)
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
        print(f"Falha na limpeza de {pasta}: {e}")


def loop_limpeza():
    while True:
        limpar_pasta(SAVE_DIR)
        limpar_pasta(RAW_DIR)
        time.sleep(LIMPEZA_INTERVALO_SEGUNDOS)


def url_e_publica(url: str) -> bool:
    """Recusa reenviar eventos pra localhost/rede interna — evita que a
    função de reenvio vire um proxy pra atacar a infraestrutura interna da
    hospedagem onde este servidor estiver rodando."""
    try:
        partes = urllib.parse.urlparse(url)
        if partes.scheme not in ("http", "https") or not partes.hostname:
            return False
        ip = socket.gethostbyname(partes.hostname)
        endereco = ipaddress.ip_address(ip)
        return not (
            endereco.is_private
            or endereco.is_loopback
            or endereco.is_link_local
            or endereco.is_reserved
            or endereco.is_multicast
        )
    except Exception:
        return False


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


# Dedup de retries: a Hikvision inclui um <UUID> por detecção dentro do XML
# do evento — se a câmera não recebe o ACK a tempo, ela reenvia o MESMO
# evento (mesmo UUID), não gera um novo. Um evento com um UUID já visto
# há pouco tempo é descartado (não salva arquivo nem aparece na página).
UUID_JANELA_SEGUNDOS = 120
UUID_MAX_MEMORIA = 2000
UUIDS_LOCK = threading.Lock()
UUIDS_VISTOS = {}  # uuid -> monotonic() da primeira vez que foi processado


def evento_e_duplicado(uuid_evento):
    if not uuid_evento:
        return False
    agora = time.monotonic()
    with UUIDS_LOCK:
        visto_em = UUIDS_VISTOS.get(uuid_evento)
        if visto_em is not None and (agora - visto_em) < UUID_JANELA_SEGUNDOS:
            return True
        UUIDS_VISTOS[uuid_evento] = agora
        if len(UUIDS_VISTOS) > UUID_MAX_MEMORIA:
            mais_antigo = min(UUIDS_VISTOS, key=UUIDS_VISTOS.get)
            del UUIDS_VISTOS[mais_antigo]
        return False


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


def resumir_campos(flat: dict) -> str:
    """Extrai uma linha curta e legível dos campos achatados, pra mostrar no
    cabeçalho do evento sem precisar expandir. Cobre os casos mais comuns
    (ANPR, facial, heartbeat) e cai pro eventType/eventDescription genérico
    pros outros tipos de dispositivo."""
    placa = flat.get("licensePlate")
    if placa and placa.lower() != "unknown":
        conf = flat.get("confidenceLevel")
        return f"placa {placa}" + (f" ({conf}%)" if conf else "")
    nome = flat.get("name") or flat.get("employeeNoString") or flat.get("employeeNo")
    if nome:
        return f"pessoa {nome}"
    return flat.get("eventDescription") or flat.get("eventType") or ""


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
  .header-top { display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
  header h1 { font-size:14px; font-weight:600; margin:0; }
  #status { font-size:12px; padding:2px 8px; border-radius:10px; }
  #status.ok { background:#123d24; color:#5fd58a; }
  #status.down { background:#3d1212; color:#e37a7a; }
  .header-acoes { display:flex; gap:6px; margin-left:auto; }
  .btn-mini { font: inherit; font-size:12px; color:#a9bbcc; background:#182430; border:1px solid #24303c;
              border-radius:6px; padding:4px 10px; cursor:pointer; }
  .btn-mini:hover { border-color:#3a4c60; color:#cfe3fb; }
  .btn-mini.ativo { background:#4a2a12; border-color:#c97a3f; color:#f0c9a3; }
  .btn-mini:disabled { opacity:0.6; cursor:default; }
  #dispositivos { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .chip { font: inherit; font-size:12px; color:#a9bbcc; background:#182430; border:1px solid #24303c;
          border-radius:999px; padding:4px 10px 4px 8px; cursor:pointer; display:inline-flex; align-items:center; gap:6px; }
  .chip:hover { border-color:#3a4c60; }
  .chip.ativo { background:#1b3550; border-color:#3f7dc9; color:#cfe3fb; }
  .bolinha { width:8px; height:8px; border-radius:50%; flex:none; }
  main { padding:12px 16px 40px; max-width:900px; margin:0 auto; }
  .evento { border:1px solid #24303c; border-left-width:4px; border-radius:8px; padding:10px 12px;
            margin-bottom:10px; background:#0f151c; }
  .evento .meta { color:#7ea0c2; font-size:12px; margin-bottom:6px; display:flex; align-items:center; gap:8px;
                   flex-wrap:wrap; cursor:pointer; user-select:none; }
  .evento .meta:hover { color:#cfe3fb; }
  .seta { display:inline-block; transition: transform 0.15s; flex:none; }
  .evento.recolhido .seta { transform: rotate(-90deg); }
  .evento.recolhido pre, .evento.recolhido .imagens { display:none; }
  .evento.recolhido { padding-bottom:10px; }
  .badge-tipo { font-size:11px; font-weight:600; padding:1px 8px; border-radius:999px; color:#fff; }
  .evento pre { white-space: pre-wrap; word-break: break-word; margin:0; font-size:12.5px; line-height:1.4; }
  .evento .imagens { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
  .evento .imagens img { max-width:220px; max-height:220px; border-radius:6px; border:1px solid #24303c; }
  .evento.recolhido .arquivos { display:none; }
  .arquivos { display:flex; flex-direction:column; gap:4px; margin-top:8px; }
  .link-arquivo { font-size:12px; color:#7ea0c2; text-decoration:none; }
  .link-arquivo:hover { text-decoration:underline; color:#cfe3fb; }
  .resumo { color:#e3c17a; font-weight:600; }
  #vazio { color:#5c6b7a; padding:20px 0; }
</style>
</head>
<body>
<header>
  <div class="header-top">
    <h1>Event Listener &mdash; ao vivo</h1>
    <span id="status" class="down">conectando&hellip;</span>
    <div class="header-acoes">
      <button id="pausarBtn" class="btn-mini">Pausar</button>
      <button id="limparBtn" class="btn-mini">Limpar tela</button>
    </div>
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
  const ultimoPorDispositivo = {}; // dispositivo -> <div class="evento"> mais recente

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
    div.className = 'evento'; // o mais recente de cada dispositivo começa aberto
    div.dataset.dispositivo = ev.dispositivo;
    div.style.borderLeftColor = corDoDispositivo(ev.dispositivo);
    if (filtro && ev.dispositivo !== filtro) div.style.display = 'none';

    // Só fica aberto o último evento de cada dispositivo; ao chegar um novo,
    // o anterior desse mesmo dispositivo recolhe sozinho.
    const anterior = ultimoPorDispositivo[ev.dispositivo];
    if (anterior) anterior.classList.add('recolhido');
    ultimoPorDispositivo[ev.dispositivo] = div;

    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.onclick = () => div.classList.toggle('recolhido');
    const seta = document.createElement('span');
    seta.className = 'seta';
    seta.textContent = '▾';
    meta.appendChild(seta);
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
    if (ev.resumo) {
      const resumo = document.createElement('span');
      resumo.className = 'resumo';
      resumo.textContent = ev.resumo;
      meta.appendChild(resumo);
    }
    if (ev.tem_raw && ev.id) {
      const reenviarBtn = document.createElement('button');
      reenviarBtn.className = 'btn-mini';
      reenviarBtn.textContent = 'Reenviar…';
      reenviarBtn.onclick = (clique) => {
        clique.stopPropagation();
        reenviarEvento(ev.id, reenviarBtn);
      };
      meta.appendChild(reenviarBtn);
    }

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
    if (ev.textos && ev.textos.length) {
      const arquivos = document.createElement('div');
      arquivos.className = 'arquivos';
      for (const fname of ev.textos) {
        const a = document.createElement('a');
        a.className = 'link-arquivo';
        a.href = '/recebidos/' + encodeURIComponent(fname);
        a.textContent = '📄 ' + fname;
        arquivos.appendChild(a);
      }
      div.appendChild(arquivos);
    }
    log.appendChild(div);
    if (deveRolar) window.scrollTo(0, document.body.scrollHeight);
  }

  function reenviarEvento(id, btn) {
    let destino = '';
    try { destino = localStorage.getItem('destino_reenvio') || ''; } catch (err) {}
    destino = window.prompt('Reenviar este evento (payload original) para qual URL?', destino);
    if (!destino) return;
    try { localStorage.setItem('destino_reenvio', destino); } catch (err) {}

    const textoOriginal = btn.textContent;
    btn.textContent = 'Enviando…';
    btn.disabled = true;
    fetch('/reenviar/' + id, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: destino }),
    })
      .then(r => r.json())
      .then(res => {
        btn.textContent = res.ok ? `OK (${res.status})` : `Falhou: ${res.erro || res.status || '?'}`;
      })
      .catch(() => { btn.textContent = 'Erro de rede'; })
      .finally(() => {
        setTimeout(() => { btn.textContent = textoOriginal; btn.disabled = false; }, 3000);
      });
  }

  let pausado = false;
  let filaPendente = [];
  const pausarBtn = document.getElementById('pausarBtn');
  const limparBtn = document.getElementById('limparBtn');

  pausarBtn.onclick = () => {
    pausado = !pausado;
    pausarBtn.classList.toggle('ativo', pausado);
    if (!pausado) {
      const pendentes = filaPendente;
      filaPendente = [];
      pausarBtn.textContent = 'Pausar';
      pendentes.forEach(addEvento);
    } else {
      pausarBtn.textContent = 'Retomar';
    }
  };

  limparBtn.onclick = () => {
    log.innerHTML = '';
    Object.keys(ultimoPorDispositivo).forEach(k => delete ultimoPorDispositivo[k]);
    vazio.style.display = '';
  };

  function processarEvento(ev) {
    if (pausado) {
      filaPendente.push(ev);
      pausarBtn.textContent = `Retomar (${filaPendente.length})`;
      return;
    }
    addEvento(ev);
  }

  function connect() {
    const es = new EventSource('/stream');
    es.onopen = () => { status.textContent = 'ao vivo'; status.className = 'ok'; };
    es.onerror = () => { status.textContent = 'reconectando…'; status.className = 'down'; };
    es.onmessage = (e) => {
      try { processarEvento(JSON.parse(e.data)); } catch (err) { console.error(err); }
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
        content_type = IMAGEM_CONTENT_TYPES.get(ext) or TEXTO_CONTENT_TYPES.get(ext)
        fpath = os.path.join(SAVE_DIR, fname)
        if not fname or not content_type or not os.path.isfile(fpath):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with open(fpath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if ext in TEXTO_CONTENT_TYPES:
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.end_headers()
        self.wfile.write(data)

    def _serve_reenviar(self):
        evento_id = os.path.basename(self.path[len("/reenviar/"):].split("?", 1)[0])
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            payload = {}
        destino = (payload.get("url") or "").strip()

        resultado = {"ok": False, "erro": None}
        raw_path = os.path.join(RAW_DIR, f"{evento_id}.raw")
        if not url_e_publica(destino):
            resultado["erro"] = "URL inválida ou aponta pra rede interna"
        elif not os.path.isfile(raw_path):
            resultado["erro"] = "corpo do evento expirado ou não encontrado"
        else:
            with STATE_LOCK:
                original = next((e for e in EVENTS if e.get("id") == evento_id), None)
            content_type = (original or {}).get("content_type") or "application/octet-stream"
            with open(raw_path, "rb") as f:
                dados = f.read()
            req = urllib.request.Request(
                destino, data=dados, headers={"Content-Type": content_type}, method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resultado = {"ok": True, "status": resp.status}
            except urllib.error.HTTPError as e:
                resultado = {"ok": False, "status": e.code, "erro": str(e)}
            except Exception as e:
                resultado = {"ok": False, "erro": str(e)}

        body = json.dumps(resultado).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        # (sem Content-Length) de um jeito compatível com proxies reversos
        # comuns em plataformas de deploy.
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

    def _log_common(self, ts):
        self._out("\n" + "=" * 70)
        self._out(f"[{ts}] {self.command} {self.path}  de {self.client_address[0]}")
        self._out("-" * 70)
        self._out("Headers:")
        for k, v in self.headers.items():
            self._out(f"  {k}: {v}")
        self._out("-" * 70)

    def _handle(self):
        self._lines = []
        self._imagens = []
        self._textos = []
        self._dispositivo = None
        self._tipo = None
        self._resumo = None
        self._descartado = False
        evento_id = uuid.uuid4().hex[:10]
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        length = int(self.headers.get("Content-Length", 0))
        content_type = self.headers.get("Content-Type", "")
        body = self.rfile.read(length) if length > 0 else b""

        # Responde o ACK aqui, ANTES de logar/gravar/processar o que veio no
        # corpo. A Hikvision espera a resposta EXATA "HTTP/1.1 200 " (com
        # espaço após o 200, sem "OK") rapidamente — se o processamento
        # (gravar imagens grandes em disco, por exemplo) demorar antes de
        # responder, a câmera ou um proxy no caminho pode reenviar a mesma
        # requisição por timeout, o que aparece como eventos "duplicados"
        # que na real são apenas retries do mesmo evento original.
        self.send_response_only(200, "")
        self.send_header("Content-Length", "0")
        self.end_headers()

        inicio_processamento = time.monotonic()
        self._log_common(ts)

        if length == 0:
            self._out("(sem corpo)")
        elif content_type.startswith("multipart/form-data"):
            # Payload multipart: normalmente contém o XML/JSON do evento
            # e a(s) imagem(ns) capturada(s), quando "Upload Binary Image"
            # está ativado no dispositivo.
            boundary_match = re.search(r"boundary=(.+)$", content_type)
            if not boundary_match:
                self._out("(multipart sem boundary identificável, corpo bruto abaixo)")
                self._out(str(body))
            else:
                boundary = boundary_match.group(1).strip().strip('"').encode("utf-8")
                partes = parse_multipart(body, boundary)

                # Pré-checagem: acha o UUID do evento (campo <UUID> do XML da
                # Hikvision) ANTES de salvar qualquer coisa, pra poder
                # descartar retries sem gravar arquivo nem gerar card na
                # página — a câmera reenvia o MESMO UUID quando não recebe o
                # ACK a tempo, não gera um novo por detecção.
                uuid_evento = None
                for part in partes:
                    if part["data"][:1] == b"<":
                        try:
                            flat_preview = flatten_xml(part["data"].decode("utf-8", errors="replace"))
                        except Exception:
                            flat_preview = {}
                        if flat_preview.get("UUID"):
                            uuid_evento = flat_preview["UUID"]
                            break

                if evento_e_duplicado(uuid_evento):
                    self._out(
                        f"Evento duplicado descartado — mesmo UUID '{uuid_evento}' de um "
                        f"retry recente da câmera (não é uma nova detecção). Nada foi "
                        f"salvo nem exibido na página."
                    )
                    self._descartado = True
                else:
                    for i, part in enumerate(partes):
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
                            fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{evento_id}_{fname}"
                            fpath = os.path.join(SAVE_DIR, fname)
                            with open(fpath, "w", encoding="utf-8") as f:
                                f.write(text)
                            self._textos.append(fname)
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
                                    # próprio XML — atrás de proxy/CDN, o IP da conexão
                                    # TCP costuma ser o do proxy (ou o IP público
                                    # compartilhado pelo roteador de todos os
                                    # dispositivos da mesma rede local).
                                    if self._dispositivo is None:
                                        self._dispositivo = flat.get("ipAddress") or flat.get("macAddress")
                                    if self._tipo is None:
                                        self._tipo = flat.get("eventType")
                                    if self._resumo is None:
                                        self._resumo = resumir_campos(flat)
                        else:
                            ext = "jpg" if is_jpeg else ("png" if is_png else "bin")
                            base = part["filename"] or f"{name}.{ext}"
                            fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{evento_id}_{base}"
                            fpath = os.path.join(SAVE_DIR, fname)
                            with open(fpath, "wb") as f:
                                f.write(data)
                            if is_jpeg or is_png:
                                self._imagens.append(fname)
                            self._out(f"Campo '{name}': imagem salva em {fpath} ({len(data)} bytes)")
        else:
            # Corpo simples (JSON, XML ou texto puro)
            try:
                text = body.decode("utf-8", errors="replace")
            except Exception:
                text = repr(body)
            self._out("Corpo:")
            self._out(text)

            ext = "xml" if text.lstrip().startswith("<") else ("json" if text.lstrip().startswith("{") else "txt")
            flat = flatten_xml(text) if ext == "xml" else {}

            if evento_e_duplicado(flat.get("UUID")):
                self._out(
                    f"Evento duplicado descartado — mesmo UUID '{flat.get('UUID')}' de "
                    f"um retry recente (não é uma nova detecção). Nada foi salvo nem "
                    f"exibido na página."
                )
                self._descartado = True
            else:
                fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{evento_id}_corpo.{ext}"
                with open(os.path.join(SAVE_DIR, fname), "w", encoding="utf-8") as f:
                    f.write(text)
                self._textos.append(fname)

                if ext == "xml":
                    self._dispositivo = flat.get("ipAddress") or flat.get("macAddress")
                    self._tipo = flat.get("eventType")
                    self._resumo = resumir_campos(flat)

        duracao = time.monotonic() - inicio_processamento
        self._out(f"(processamento pós-ACK levou {duracao:.3f}s)")
        self._out("=" * 70 + "\n")

        if not self._descartado:
            if body:
                with open(os.path.join(RAW_DIR, f"{evento_id}.raw"), "wb") as f:
                    f.write(body)

            broadcast({
                "id": evento_id,
                "ts": ts,
                "metodo": self.command,
                "path": self.path,
                "ip": self.client_address[0],
                "dispositivo": self._dispositivo or self.client_address[0],
                "tipo": self._tipo,
                "resumo": self._resumo,
                "content_type": content_type,
                "tem_raw": bool(body),
                "texto": "\n".join(self._lines),
                "imagens": self._imagens,
                "textos": self._textos,
            })

    def do_POST(self):
        if self.path.startswith("/reenviar/"):
            self._serve_reenviar()
            return
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
        # Muitas plataformas de deploy definem a porta via variável de
        # ambiente PORT; localmente cai no padrão 8000.
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
