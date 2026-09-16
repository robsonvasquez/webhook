"""
Servidor HTTP simples para capturar e exibir os eventos que a câmera Hikvision
envia via ISAPI Listening (POST HTTP, multipart quando "Upload Binary Image"
está ativado). Também expõe funções para sincronizar a whitelist da câmera
e acionar a barreira/cancela via ISAPI.

Uso:
    python hik_lpr_listener.py [porta]

Padrão: porta 8000

Na câmera, configure em Configuration > Rede > Ligação de dados > ISAPI Listening:
    ANPR IP/Domínio : <IP/domínio deste servidor>
    ANPR Porta      : 8000  (ou a porta que você passar como argumento)
    URL anfitrião   : /lpr

============================================================================
DOIS FLUXOS DE ACIONAMENTO DA BARREIRA, coexistindo:
============================================================================

1) AUTOMÁTICO, decidido pela própria câmera (requer cartão TF/microSD
   inserido nela — recurso "offline control of barrier gate under allowlist
   mode"): você sincroniza a whitelist com adicionar_placa()/remover_placa(),
   e a câmera abre sozinha quando reconhece uma placa cadastrada, mesmo sem
   rede/backend disponível. Esse script apenas recebe o evento (via
   ISAPIListen) para fins de LOG/auditoria — não precisa acionar nada.

2) MANUAL, sob demanda, via app (botão "abrir cancela" para visitantes,
   por exemplo): o backend chama acionar_barreira(abrir=True) a qualquer
   momento, sem relação com reconhecimento de placa. Para testar esse fluxo
   localmente, este servidor expõe:
       POST /abrir-cancela
       POST /fechar-cancela
   Em produção, é o seu backend (Go/C#/Python) que chama diretamente o
   endpoint ISAPI da câmera (função acionar_barreira() como referência).

Depois de rodar, dispare um evento na câmera (placa cruzando a trigger line)
e observe o terminal. Todo POST recebido é logado com headers e corpo, e as
imagens binárias (se "Upload Binary Image" estiver ligado) são salvas na
pasta "recebidos/" ao lado deste script.
"""

import sys
import os
import re
import json
import ssl
import base64
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recebidos")
os.makedirs(SAVE_DIR, exist_ok=True)

# ============================================================================
# CONFIGURAÇÃO — tudo abaixo vem de variáveis de ambiente (definidas no
# painel do Render, ou num .env local carregado pelo seu shell), para não
# deixar segredos (senha da câmera, IP) presos no código-fonte.
# ============================================================================

# Quando definido (não vazio), o evento decodificado é repassado via POST,
# como JSON, para essa URL depois de cada leitura de placa.
# Ex.: BACKEND_WEBHOOK_URL = "https://api.drikey.com.br/lpr/evento"
BACKEND_WEBHOOK_URL = os.environ.get("BACKEND_WEBHOOK_URL") or None

# Usadas para chamar de volta a API ISAPI da câmera (whitelist e
# acionamento de barreira).
CAMERA_HOST = os.environ.get("CAMERA_HOST", "")        # IP ou domínio da câmera
CAMERA_PORT = int(os.environ.get("CAMERA_PORT", "8420"))  # porta HTTP/HTTPS da interface/ISAPI
CAMERA_USER = os.environ.get("CAMERA_USER", "admin")
CAMERA_PASS = os.environ.get("CAMERA_PASS", "")
CAMERA_USE_HTTPS = os.environ.get("CAMERA_USE_HTTPS", "false").lower() == "true"

# Se "true", quando chegar um evento de placa reconhecida, o script decide
# automaticamente (whitelist local abaixo) e aciona a barreira via ISAPI.
# Deixe "false" enquanto só estiver testando/observando os eventos.
AUTO_ABRIR_BARREIRA = os.environ.get("AUTO_ABRIR_BARREIRA", "false").lower() == "true"

# Whitelist local de exemplo, só para teste standalone deste script.
# Em produção, essa decisão deve vir do backend/banco de dados da Drikey,
# não de uma lista fixa aqui. Formato: placas separadas por vírgula.
PLACAS_AUTORIZADAS = {
    p.strip().upper()
    for p in os.environ.get("PLACAS_AUTORIZADAS", "TEST1234").split(",")
    if p.strip()
}
# ============================================================================


def _camera_url(path: str) -> str:
    scheme = "https" if CAMERA_USE_HTTPS else "http"
    return f"{scheme}://{CAMERA_HOST}:{CAMERA_PORT}{path}"


def _chamar_isapi(method: str, path: str, xml_body: str) -> dict:
    """Faz uma chamada ISAPI (digest auth) para a própria câmera e devolve
    {"ok": bool, "status": int, "body": str}. Usa só urllib (stdlib) mais um
    handler de Digest Authentication, sem depender do pacote 'requests'.
    """
    import urllib.request

    url = _camera_url(path)
    password_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    password_mgr.add_password(None, url, CAMERA_USER, CAMERA_PASS)
    digest_handler = urllib.request.HTTPDigestAuthHandler(password_mgr)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    https_handler = urllib.request.HTTPSHandler(context=ctx)

    opener = urllib.request.build_opener(digest_handler, https_handler)

    data = xml_body.encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/xml")

    try:
        with opener.open(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return {"ok": True, "status": resp.status, "body": body}
    except Exception as e:
        return {"ok": False, "status": None, "body": str(e)}


def adicionar_placa(plate_num: str, list_type: int = 0):
    """Adiciona (ou atualiza) uma placa na whitelist/blacklist da câmera.
    list_type: 0=Whitelist, 1=Blacklist, 2=Graylist, 3=Yellowlist, 4=Otherlist.
    """
    xml_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<SetVCLData><VCLDataList><singleVCLData>"
        "<id>0</id><runNum>0</runNum>"
        f"<listType>{list_type}</listType>"
        f"<plateNum>{plate_num}</plateNum>"
        "<cardNo></cardNo>"
        "<startTime>2000-01-01T00:00:00Z</startTime>"
        "<endTime>2037-12-31T23:59:59Z</endTime>"
        "</singleVCLData></VCLDataList></SetVCLData>"
    )
    return _chamar_isapi("PUT", "/ISAPI/ITC/Entrance/VCL", xml_body)


def remover_placa(plate_num: str):
    """Remove uma placa da whitelist/blacklist da câmera."""
    xml_body = (
        "<VCLDelCond><delVCLCond>1</delVCLCond>"
        f"<plateNum>{plate_num}</plateNum>"
        "<plateColor>0</plateColor><plateType>0</plateType>"
        "<cardNo>123</cardNo></VCLDelCond>"
    )
    return _chamar_isapi("DELETE", "/ISAPI/ITC/Entrance/VCL", xml_body)


def acionar_barreira(abrir: bool = True):
    """Abre ou fecha a barreira/cancela via ISAPI (endpoint dedicado desta
    linha de câmera — /ISAPI/Parking/channels/<ID>/barrierGate).
    """
    modo = "open" if abrir else "close"
    xml_body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f"<BarrierGate><ctrlMode>{modo}</ctrlMode></BarrierGate>"
    )
    return _chamar_isapi("PUT", "/ISAPI/Parking/channels/1/barrierGate", xml_body)


def parse_anpr_xml(xml_text: str) -> dict:
    """Extrai os campos relevantes do EventNotificationAlert/ANPR e devolve
    um dict pronto para virar JSON. Namespace-agnostic (ignora o xmlns).
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
            continue  # primeira ocorrência vence (evita que duplicatas dentro
            # de PlateInfoList, com valores 'unknown'/0 para não-matches,
            # sobrescrevam os valores corretos do bloco ANPR principal)
        if elem.text and elem.text.strip() and len(list(elem)) == 0:
            flat[tag] = elem.text.strip()

    raw_list_type = flat.get("listType", "")
    LIST_TYPE_MAP = {
        "0": "whitelist",
        "1": "blacklist",
        "2": "graylist",
        "3": "yellowlist",
        "4": "otherlist",
    }

    # Campos mais usados no dia a dia, com nomes já traduzidos/normalizados
    return {
        "placa": flat.get("licensePlate"),
        "confianca": flat.get("confidenceLevel"),
        "faixa": flat.get("line"),
        "direcao": flat.get("direction"),  # forward/reverse
        "cor_placa": flat.get("plateColor"),
        "tipo_placa": flat.get("plateType"),
        "data_hora": flat.get("dateTime"),
        "canal": flat.get("channelName"),
        "tipo_evento": flat.get("eventType"),
        "lista": LIST_TYPE_MAP.get(raw_list_type),  # None = não cadastrada em nenhuma lista
        "raw": flat,  # todos os campos originais, sem perder nada
    }


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


def encaminhar_para_backend(evento: dict):
    """Envia o evento decodificado (JSON) para o backend, se BACKEND_WEBHOOK_URL
    estiver configurado. Usa só a biblioteca padrão (urllib), sem precisar
    instalar 'requests'.
    """
    if not BACKEND_WEBHOOK_URL:
        return
    import urllib.request

    payload = json.dumps(evento).encode("utf-8")
    req = urllib.request.Request(
        BACKEND_WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"Encaminhado para o backend: HTTP {resp.status}")
    except Exception as e:
        print(f"Falha ao encaminhar para o backend: {e}")


def decidir_e_acionar_barreira(evento: dict):
    """Exemplo de lógica de decisão: em produção, troque PLACAS_AUTORIZADAS
    por uma consulta real ao backend/banco da Drikey. Só age de verdade se
    AUTO_ABRIR_BARREIRA estiver True (trava de segurança para testes).
    """
    placa = evento.get("placa")
    if not placa:
        return

    autorizada = placa.upper() in PLACAS_AUTORIZADAS
    print(f"Placa '{placa}' {'AUTORIZADA' if autorizada else 'NÃO autorizada'} (whitelist local de teste)")

    if not AUTO_ABRIR_BARREIRA:
        print("(AUTO_ABRIR_BARREIRA=False — nenhuma ação física tomada; só decisão simulada)\n")
        return

    if autorizada:
        resultado = acionar_barreira(abrir=True)
        print(f"Comando de abertura de barreira: {resultado}\n")
    else:
        print("Placa não autorizada — barreira não acionada.\n")


class LPRHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _log_common(self):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print("\n" + "=" * 70)
        print(f"[{ts}] {self.command} {self.path}  de {self.client_address[0]}")
        print("-" * 70)
        print("Headers:")
        for k, v in self.headers.items():
            print(f"  {k}: {v}")
        print("-" * 70)

    def _handle(self):
        self._log_common()
        length = int(self.headers.get("Content-Length", 0))
        content_type = self.headers.get("Content-Type", "")

        if length == 0:
            print("(sem corpo)")
        elif content_type.startswith("multipart/form-data"):
            # Payload multipart: normalmente contém o XML/JSON do evento
            # e a(s) imagem(ns) capturada(s), quando "Upload Binary Image"
            # está ativado na câmera.
            boundary_match = re.search(r"boundary=(.+)$", content_type)
            if not boundary_match:
                print("(multipart sem boundary identificável, corpo bruto abaixo)")
                print(self.rfile.read(length))
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
                        print(f"Campo '{name}' (texto, salvo em {fpath}):\n{text}\n")

                        # Se for o XML do evento ANPR, extrai os campos e monta o JSON
                        if "anpr" in name.lower() or (part["filename"] or "").lower().endswith(".xml"):
                            evento = parse_anpr_xml(text)
                            if evento.get("placa"):
                                print("Evento decodificado (JSON):")
                                print(json.dumps(evento, indent=2, ensure_ascii=False))
                                print()
                                encaminhar_para_backend(evento)
                                decidir_e_acionar_barreira(evento)
                    else:
                        ext = "jpg" if is_jpeg else ("png" if is_png else "bin")
                        base = part["filename"] or f"{name}.{ext}"
                        fname = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{base}"
                        fpath = os.path.join(SAVE_DIR, fname)
                        with open(fpath, "wb") as f:
                            f.write(data)
                        print(f"Campo '{name}': imagem salva em {fpath} ({len(data)} bytes)")
        else:
            # Corpo simples (JSON, XML ou texto puro)
            body = self.rfile.read(length)
            try:
                text = body.decode("utf-8", errors="replace")
            except Exception:
                text = repr(body)
            print("Corpo:")
            print(text)

        print("=" * 70 + "\n")

        # A Hikvision espera a resposta EXATA "HTTP/1.1 200 " (com espaço após o
        # 200, sem "OK"). Sem isso, alguns firmwares reenviam o mesmo evento
        # repetidamente por não reconhecerem a confirmação de recebimento.
        self.send_response_only(200, "")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        # Rotas de acionamento manual — simulam o botão "abrir cancela" do
        # app, chamado pelo backend sob demanda (sem relação com ANPR).
        # Em produção, é o seu backend Go/C# que chama diretamente o
        # endpoint ISAPI da câmera; essas rotas aqui existem só para você
        # testar esse fluxo localmente antes de portar a lógica.
        if self.path in ("/abrir-cancela", "/fechar-cancela"):
            abrir = self.path == "/abrir-cancela"
            resultado = acionar_barreira(abrir=abrir)
            print(f"[Acionamento manual via 'app'] {'Abrir' if abrir else 'Fechar'} → {resultado}\n")
            self.send_response_only(200, "")
            body = json.dumps(resultado).encode("utf-8")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._handle()

    def do_GET(self):
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
    server = HTTPServer(("0.0.0.0", port), LPRHandler)
    print(f"Escutando em http://0.0.0.0:{port}  (Ctrl+C para parar)")
    print(f"Imagens recebidas serão salvas em: {SAVE_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando...")
        server.server_close()


if __name__ == "__main__":
    main()
