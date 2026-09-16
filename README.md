# event_listener

Servidor HTTP (stdlib, sem dependências) pra inspecionar os eventos que
dispositivos Hikvision (câmeras LPR/ANPR, terminais de reconhecimento
facial, controladoras de acesso, etc.) enviam via ISAPI Listening. Cadastre
a URL deste servidor no dispositivo e veja no terminal (e em `recebidos/`)
como cada tipo de evento chega — headers, corpo, XML achatado em JSON e
imagens, quando o dispositivo envia.

Não decide nada nem aciona nada de volta no dispositivo: é só um listener
de observação/depuração.

## Rodando localmente

```bash
python event_listener.py            # porta 8000
python event_listener.py 9000       # porta customizada
```

## Deploy no Render

1. **Suba este repositório para o GitHub** (já feito se você seguiu o fluxo
   assistido; senão: `git init && git add . && git commit -m "..." && gh repo create`).
2. No [dashboard do Render](https://dashboard.render.com), clique em
   **New + → Blueprint** e aponte para este repositório — ele lê o
   `render.yaml` e cria o Web Service `event-listener` automaticamente.
   - Alternativa manual (sem blueprint): **New + → Web Service**, conecte o
     repo, Runtime = **Python 3**, Build Command = `pip install -r requirements.txt`,
     Start Command = `python event_listener.py`.
3. Depois do primeiro deploy, o Render te dá uma URL pública, algo como
   `https://event-listener.onrender.com`. Configure essa URL em cada
   dispositivo (Configuration → Rede → Ligação de dados → ISAPI Listening,
   o caminho exato varia por modelo):
   - **IP/Domínio**: `event-listener.onrender.com`
   - **Porta**: `443` (HTTPS) — o Render já termina TLS por você
   - **URL anfitrião**: `/evento` (ou qualquer caminho — o servidor aceita
     POST/GET em qualquer path)

### Observação importante

**Disco efêmero**: o Render reinicia o filesystem a cada deploy/restart. Os
arquivos salvos em `recebidos/` (imagens/XML) são perdidos nesse momento —
serve só pra depuração pontual, olhar os logs no dashboard do Render
enquanto testa cada dispositivo, não como armazenamento definitivo.
