# event_listener

Servidor HTTP genérico (stdlib, sem dependências) pra inspecionar eventos
que qualquer dispositivo/sistema manda via webhook — JSON, XML ou
multipart/form-data (com imagens), em qualquer path. Cadastre a URL deste
servidor onde for preciso testar uma integração e veja no terminal (e em
`recebidos/`) como cada evento chega — headers, corpo e imagens, quando
enviadas.

Tem tratamento especial pra eventos no formato ISAPI da Hikvision (câmeras
LPR/ANPR, terminais de reconhecimento facial, controladoras de acesso,
etc.): o XML é achatado em JSON, o dispositivo de origem e um resumo (placa,
pessoa reconhecida...) são extraídos automaticamente. Pra qualquer outro
tipo de payload, o corpo cru continua sendo logado normalmente.

Não decide nada nem aciona nada de volta no dispositivo: é só um listener
de observação/depuração.

**Eventos "duplicados" na câmera?** O ACK (`HTTP/1.1 200 `, no formato exato
que a Hikvision exige) é enviado assim que o corpo termina de chegar, antes
de gravar qualquer arquivo em disco — se a resposta demora, a própria câmera
(ou um proxy no meio do caminho) tende a reenviar a mesma captura por
timeout, o que aparece como dois eventos idênticos (mesmo `UUID`/
`activePostCount` no XML) em vez de dois eventos novos.

Abrindo a URL raiz (`/`) do serviço num navegador, você vê os eventos
chegando **ao vivo**, sem precisar dar refresh. Quando o dispositivo manda
uma imagem (ex.: foto da placa/rosto capturados), a miniatura aparece direto
no card do evento. Com vários dispositivos cadastrados no mesmo webhook, os
eventos são agrupados pelo IP interno de cada equipamento (lido de dentro do
XML do evento — não do IP da conexão, que atrás de proxy/CDN costuma ser o
mesmo pra todos os dispositivos da mesma rede). Cada dispositivo e tipo de
evento
ganham uma cor; clique no "chip" de um dispositivo pra ver só os eventos
dele. Só o evento mais recente de cada dispositivo fica expandido — os
anteriores ficam recolhidos automaticamente, mostrando um resumo (placa
detectada, pessoa reconhecida, etc.) no cabeçalho mesmo fechado — clique
pra expandir/recolher qualquer um.

Outros recursos da página:
- **Pausar** — congela a tela pra você ler com calma; os eventos que
  chegarem nesse meio tempo ficam na fila e aparecem ao retomar.
- **Limpar tela** — some com os cards (não apaga nada do servidor).
- **Baixar** — cada XML/JSON recebido vira um link de download no card.
- **Reenviar…** — manda de novo o payload exatamente como chegou (mesmo
  Content-Type/boundary) pra uma URL que você escolhe na hora, útil pra
  testar como o backend real reagiria sem precisar acionar o dispositivo
  de novo. Só funciona enquanto o corpo bruto ainda não foi limpo pela
  limpeza automática (mesma janela de tempo de `recebidos/`).

Configure o dispositivo pra mandar os eventos pra qualquer outro path
(`/evento`, por exemplo) — `/`, `/stream`, `/events.json`, `/recebidos/*`
e `/reenviar/*` são reservados para essa visualização.

## Rodando localmente

```bash
python event_listener.py            # porta 8000
python event_listener.py 9000       # porta customizada
```

## Deploy

É Python 3 puro (só stdlib, sem dependências) — roda em qualquer lugar que
execute Python: VPS próprio, container/Docker, ou qualquer PaaS (Render,
Railway, Fly.io, Heroku, etc.). Passos gerais:

1. Suba o código pro host escolhido (git push, docker build, upload direto —
   o que a plataforma pedir).
2. Comando de start: `python event_listener.py`. Não precisa de build step
   (o `requirements.txt` está vazio, só existe porque algumas plataformas
   exigem o arquivo presente).
3. O servidor já lê a porta da variável de ambiente `PORT` quando ela existe
   (convenção comum na maioria das PaaS) — sem ela, cai no padrão 8000. Se
   sua plataforma usa outra convenção, ajuste `main()` em
   `event_listener.py` ou passe a porta como argumento
   (`python event_listener.py <porta>`).
4. Exponha a porta publicamente com HTTPS na frente, se o dispositivo/
   integração exigir TLS (a maioria das PaaS já termina TLS automaticamente;
   num VPS próprio, use um proxy reverso como Caddy ou nginx).
5. Configure no dispositivo/sistema de origem a URL pública + qualquer path
   (`/evento`, por exemplo) — evite `/`, `/stream`, `/events.json`,
   `/recebidos/*` e `/reenviar/*`, reservados para a visualização ao vivo.
6. Abra a URL raiz (`https://seu-host/`) no navegador pra acompanhar os
   eventos chegando ao vivo.

### Observações importantes

- **Disco efêmero + limpeza automática**: em containers/PaaS o filesystem
  costuma ser efêmero (reseta a cada deploy/restart); além disso, os
  arquivos em `recebidos/` e `recebidos_raw/` são apagados automaticamente
  em segundo plano (por padrão: mais de 6h de idade, ou o quanto for
  preciso pra manter o total abaixo de 200 MB por pasta) — ajuste as
  constantes `LIMPEZA_*` no topo do `event_listener.py` se quiser outros
  limites. Serve só pra depuração pontual, não como armazenamento
  definitivo.
- **Planos gratuitos costumam "dormir"**: várias PaaS suspendem a instância
  após alguns minutos sem receber requisição; a próxima requisição demora
  alguns segundos (cold start) pra acordar o serviço. Pra observar eventos
  pontualmente não costuma ser problema, mas se um dispositivo enviar o
  evento exatamente durante esse "acordar", ele pode não ser logado a
  tempo — nesse caso, considere um plano/instância sempre ativa.
- **Imagens/XML sem autenticação**: `/recebidos/<arquivo>` serve qualquer
  imagem ou XML/JSON salvo pra quem souber (ou adivinhar) o nome do arquivo
  — não tem login. Se o serviço for público, evite usar isso com dados que
  não possam ficar temporariamente expostos por URL.
- **Reenviar bloqueia rede interna**: a função "Reenviar…" recusa URLs que
  resolvam pra IP privado/loopback/link-local, pra não virar um jeito de
  usar o servidor como proxy contra a infraestrutura interna da hospedagem.
  Só aceita `http://`/`https://` pra hosts públicos.
