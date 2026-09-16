"""
Servidor HTTP simples para inspecionar os eventos que dispositivos Hikvision
(câmeras LPR/ANPR, terminais de reconhecimento facial, controladoras de
acesso, etc.) enviam via ISAPI Listening (POST HTTP, multipart quando
"Upload Binary Image" está ativado no dispositivo).

Serve só para OBSERVAR o formato/conteúdo dos eventos ao cadastrar este
servidor em cada dispositivo — não decide nada nem aciona nada de volta.

Uso:
    python hik_lpr_listener.py [porta]

Padrão: porta 8000, ou a variável de ambiente PORT (usada em produção/Render).

No dispositivo, configure em Configuration > Rede > Ligação de dados > ISAPI
Listening (o caminho exato varia por modelo/firmware):
    IP/Domínio    : <IP/domínio deste servidor>
    Porta         : 8000  (ou a porta que você passar como argumento/PORT)
    URL anfitrião : /evento   (este servidor aceita POST/GET em qualquer path)

Todo POST/GET recebido é logado no terminal com headers e corpo. Quando o
corpo é multipart/form-data, cada parte é salva em recebidos/: texto
(XML/JSON) é impresso e salvo como .txt/.xml; imagens (quando "Upload Binary
Image" está ligado) são salvas como .jpg/.png. Partes em XML também têm seus
campos "achatados" (sem namespace) impressos como JSON, pra facilitar ver a
estrutura do evento de cada tipo de dispositivo.
"""

import sys
import os
import re
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

SAVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recebidos")
os.makedirs(SAVE_DIR, exist_ok=True)


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


class EventHandler(BaseHTTPRequestHandler):
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
            # está ativado no dispositivo.
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

                        # Se parecer XML, achata os campos e mostra como JSON
                        # pra facilitar ver a estrutura do evento.
                        if (part["filename"] or "").lower().endswith(".xml") or text.lstrip().startswith("<"):
                            flat = flatten_xml(text)
                            if flat:
                                print("Campos do evento (achatado, sem namespace):")
                                print(json.dumps(flat, indent=2, ensure_ascii=False))
                                print()
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
    server = HTTPServer(("0.0.0.0", port), EventHandler)
    print(f"Escutando em http://0.0.0.0:{port}  (Ctrl+C para parar)")
    print(f"Arquivos recebidos serão salvos em: {SAVE_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando...")
        server.server_close()


if __name__ == "__main__":
    main()
