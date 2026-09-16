# hik_lpr_listener

Servidor HTTP (stdlib, sem dependências) que recebe os eventos ANPR/LPR
enviados por câmeras Hikvision via ISAPI Listening, loga tudo e opcionalmente
repassa cada evento para um backend e/ou aciona a barreira/cancela.

## Rodando localmente

```bash
python hik_lpr_listener.py            # porta 8000
python hik_lpr_listener.py 9000       # porta customizada
```

Configuração via variáveis de ambiente — veja `.env.example`. Nenhum dado
sensível (senha da câmera, IP) fica no código.

## Deploy no Render

1. **Suba este repositório para o GitHub** (já feito se você seguiu o fluxo
   assistido; senão: `git init && git add . && git commit -m "..." && gh repo create`).
2. No [dashboard do Render](https://dashboard.render.com), clique em
   **New + → Blueprint** e aponte para este repositório — ele vai ler o
   `render.yaml` e criar o Web Service `hik-lpr-listener` automaticamente
   (build/start command já configurados).
   - Alternativa manual (sem blueprint): **New + → Web Service**, conecte o
     repo, Runtime = **Python 3**, Build Command = `pip install -r requirements.txt`,
     Start Command = `python hik_lpr_listener.py`.
3. Quando o Render pedir, preencha as variáveis marcadas como secretas
   (`CAMERA_HOST`, `CAMERA_PASS`, `BACKEND_WEBHOOK_URL`) — as demais já vêm
   com valor padrão no `render.yaml` e podem ser ajustadas depois em
   **Environment**.
4. Depois do primeiro deploy, o Render te dá uma URL pública, algo como
   `https://hik-lpr-listener.onrender.com`. Configure na câmera
   (Configuration → Rede → Ligação de dados → ISAPI Listening):
   - **ANPR IP/Domínio**: `hik-lpr-listener.onrender.com`
   - **ANPR Porta**: `443` (HTTPS) — o Render já termina TLS por você
   - **URL anfitrião**: `/lpr`

### Variáveis de ambiente

| Variável              | Descrição                                                         | Padrão      |
|-----------------------|---------------------------------------------------------------------|-------------|
| `CAMERA_HOST`         | IP/domínio da câmera (para chamadas ISAPI de volta)                 | *(vazio)*   |
| `CAMERA_PORT`         | Porta HTTP/HTTPS da câmera                                          | `8420`      |
| `CAMERA_USER`         | Usuário ISAPI da câmera                                             | `admin`     |
| `CAMERA_PASS`         | Senha ISAPI da câmera                                                | *(vazio)*   |
| `CAMERA_USE_HTTPS`    | `true`/`false` — usar HTTPS ao chamar a câmera                      | `false`     |
| `AUTO_ABRIR_BARREIRA` | `true`/`false` — abrir a barreira automaticamente por whitelist local| `false`     |
| `PLACAS_AUTORIZADAS`  | Placas de teste autorizadas, separadas por vírgula                  | `TEST1234`  |
| `BACKEND_WEBHOOK_URL` | URL do backend real que recebe cada evento decodificado (POST JSON) | *(vazio)*   |
| `PORT`                | Definida automaticamente pelo Render — não precisa configurar       | `8000`      |

### Observações importantes

- **Disco efêmero**: o Render reinicia o filesystem a cada deploy/restart.
  As imagens e XMLs salvos em `recebidos/` são perdidos nesse momento — isso
  serve para depuração pontual, não como armazenamento definitivo. Se
  precisar reter esses arquivos, grave-os num serviço externo (S3, backend
  próprio, etc.) em vez de depender do disco local.
- **`AUTO_ABRIR_BARREIRA`**: mantenha `false` em produção a menos que a
  decisão de autorização realmente deva vir da whitelist fixa deste
  script — o normal é essa lógica morar no backend real.
