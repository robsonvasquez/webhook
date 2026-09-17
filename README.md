# event_listener

Servidor HTTP (stdlib, sem dependências) pra inspecionar os eventos que
dispositivos Hikvision (câmeras LPR/ANPR, terminais de reconhecimento
facial, controladoras de acesso, etc.) enviam via ISAPI Listening. Cadastre
a URL deste servidor no dispositivo e veja no terminal (e em `recebidos/`)
como cada tipo de evento chega — headers, corpo, XML achatado em JSON e
imagens, quando o dispositivo envia.

Não decide nada nem aciona nada de volta no dispositivo: é só um listener
de observação/depuração.

Abrindo a URL raiz (`/`) do serviço num navegador, você vê os eventos
chegando **ao vivo**, sem precisar dar refresh — não precisa entrar no
dashboard do Render pra acompanhar. Quando o dispositivo manda uma imagem
(ex.: foto da placa/rosto capturados), a miniatura aparece direto no card do
evento. Com vários dispositivos cadastrados no mesmo webhook, os eventos são
agrupados pelo IP interno de cada equipamento (lido de dentro do XML do
evento — não do IP da conexão, que na Render é o mesmo pra todos os
dispositivos atrás do mesmo roteador). Cada dispositivo e tipo de evento
ganham uma cor; clique no "chip" de um dispositivo pra ver só os eventos
dele. Só o evento mais recente de cada dispositivo fica expandido — os
anteriores ficam recolhidos automaticamente (clique no cabeçalho de
qualquer evento pra expandir/recolher). Configure o dispositivo pra mandar
os eventos pra qualquer
outro path (`/evento`, por exemplo) — `/`, `/stream`, `/events.json` e
`/recebidos/*` são reservados para essa visualização.

## Rodando localmente

```bash
python event_listener.py            # porta 8000
python event_listener.py 9000       # porta customizada
```

## Deploy no Render

1. **Suba este repositório para o GitHub** (já feito se você seguiu o fluxo
   assistido; senão: `git init && git add . && git commit -m "..." && gh repo create`).
2. No [dashboard do Render](https://dashboard.render.com), clique em
   **New + → Web Service**, conecte este repositório e configure:
   - Runtime = **Python 3**
   - Instance Type = **Free**
   - Build Command = `pip install -r requirements.txt`
   - Start Command = `python event_listener.py`
   - (Alternativa: **New + → Blueprint** lê o `render.yaml` deste repo e cria
     o serviço com essas mesmas configurações automaticamente — também no
     plano Free.)
3. Depois do primeiro deploy, o Render te dá uma URL pública, algo como
   `https://event-listener.onrender.com`. Configure essa URL em cada
   dispositivo (Configuration → Rede → Ligação de dados → ISAPI Listening,
   o caminho exato varia por modelo):
   - **IP/Domínio**: `event-listener.onrender.com`
   - **Porta**: `443` (HTTPS) — o Render já termina TLS por você
   - **URL anfitrião**: `/evento` (ou qualquer caminho, exceto `/`, `/stream`
     e `/events.json`, reservados para a visualização ao vivo)
4. Pra acompanhar em tempo real, abra `https://event-listener.onrender.com/`
   no navegador — cada evento aparece formatado assim que chega, sem
   precisar dar refresh nem entrar no dashboard do Render.

### Observações importantes

- **Disco efêmero + limpeza automática**: o Render reinicia o filesystem a
  cada deploy/restart, e os arquivos em `recebidos/` também são apagados
  automaticamente em segundo plano (por padrão: mais de 6h de idade, ou o
  quanto for preciso pra manter o total abaixo de 200 MB) — ajuste as
  constantes `LIMPEZA_*` no topo do `event_listener.py` se quiser outros
  limites. Serve só pra depuração pontual, não como armazenamento
  definitivo.
- **Plano Free "dorme"**: depois de ~15 min sem receber requisição, o Render
  suspende a instância; a próxima requisição demora alguns segundos (cold
  start) pra acordar o serviço. Pra observar eventos pontualmente (testando
  um dispositivo por vez) não costuma ser problema, mas se o dispositivo
  enviar o evento exatamente durante esse "acordar", ele pode não ser
  logado a tempo. Se isso incomodar, dá pra migrar depois pro plano Starter
  (pago, sem sleep) trocando `plan: free` por `plan: starter` no
  `render.yaml` ou no dashboard.
- **Imagens sem autenticação**: `/recebidos/<arquivo>` serve qualquer imagem
  salva pra quem souber (ou adivinhar) o nome do arquivo — não tem login.
  Como o serviço e o repositório são públicos, evite usar isso com fotos que
  não possam ficar temporariamente expostas por URL.
