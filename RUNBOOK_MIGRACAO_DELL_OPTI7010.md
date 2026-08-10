# RUNBOOK — Migração SmartokePy + Karaoke Guard para Dell OptiPlex 7010
> Contexto: Raspberry → Dell OptiPlex 7010 (Ubuntu 26.04 LTS, 192.168.15.9, user `karaoke`)
> Data de escrita: 2026-08-10 (Lições aprendidas colhidas depois de ~12h de depuração)

---

## 1. Topologia final — duas portas, dois serviços, dois túneis Cloudflare

| Serviço              | Porta | Caminho instalado                              | systemd           | Túnel Cloudflare               |
|----------------------|-------|------------------------------------------------|-------------------|--------------------------------|
| Pikaraoke / SmartokePy (Flask + Websocket + HLS) | 5555  | `/opt/Karaoke/SmartokePy` (app.py, `python -m gevent.pywsgi.WSGIServer`) | `smartokepy.service` (antigo `pikaraoke.service`) | `karaoke.thermowatch.com.br` → 127.0.0.1:5555 |
| Karaoke Guard (React SPA + Node API SQLite)      | 3001  | `/opt/Karaoke/acess-karaoke/build-server/api/server.js` (servindo `dist/`) | `karaoke-guard.service` | `portal.thermowatch.com.br` → 127.0.0.1:3001 |

- **IP local fixo do Dell:** `192.168.15.9` (WiFi integrado do OptiPlex 7010)
- **SSH remoto (se estiver na mesma rede):** `ssh karaoke@192.168.15.9`
- **Acesso local ao Pikaraoke:** `http://192.168.15.9:5555/`
- **Acesso local ao portal:** `http://192.168.15.9:3001/`

---

## 2. Patches aplicados no SmartokePy (porta 5555)

### 2.1. Splash: tolerância a início lento de vídeo (CDN / YouTube / HLS lento)
Arquivo: [static/js/splash.js](file:///c:/Users/Usuario/Desktop/Lazaro%2018-09-25/Karaoke/SmartokePy/static/js/splash.js)

```js
// ANTES
const playbackStartTimeout = 20000;
const prematureEndThresholdSeconds = 3;

// DEPOIS
const playbackStartTimeout = 90000;          // espera até 90s para o vídeo começar (YouTube demora)
const prematureEndThresholdSeconds = 60;     // ignora "falso fim" por até 60s
```

Também invertida a prioridade de duração — sempre usar `nowPlaying.now_playing_duration` (duração real reportada pelo servidor Pikaraoke) primeiro, e só fallback para `video.duration` do browser HLS (que é 0 enquanto o HLS bufferiza).

**Sintoma que corrigiu:** splash fechava cedo demais e aparecia "fim da música" aleatoriamente no meio.

### 2.2. Sync Dell via SSH Raspberry legado — DESATIVADO por padrão
Arquivo: [lib/dell_wifi_sync.py](file:///c:/Users/Usuario/Desktop/Lazaro%2018-09-25/Karaoke/SmartokePy/lib/dell_wifi_sync.py)

```python
# ANTES: _SYNC_DELL_ENABLED = os.environ.get(..., "1")
# DEPOIS:
_SYNC_DELL_ENABLED = os.environ.get("NETWORK_MAESTRO_SYNC_DELL", "0")
```

**Sintoma que corrigiu:** `/maestro/api/contract` demorava 5s bloqueando a UI porque tentava SSH numa Raspberry que não existe mais no stack. Agora default = 0. Para ativar de novo, exporta `NETWORK_MAESTRO_SYNC_DELL=1` no environment do smartokepy.service.

### 2.3. Timeouts do network_maestro — 1.5s/2.0s → 0.35s
Arquivo: [lib/network_maestro.py](file:///c:/Users/Usuario/Desktop/Lazaro%2018-09-25/Karaoke/SmartokePy/lib/network_maestro.py)

- `_check_internet_reachability(timeout_seconds=0.35)`  (antes 1.5)
- `_get_ngrok_status(timeout_seconds=0.35)`  (antes 2.0)

**Resultado medido na prática:**
| Endpoint | Antes | Depois |
|---|---|---|
| `/maestro/api/contract` | **~5.2 s** | **232 ms** |
| `/` | 80 ms | 15 ms |
| `/maestro/health` | 4 ms | 1 ms |

### 2.4. app.py — WebSocketHandler no gevent WSGIServer — JÁ ESTAVA OK
Arquivo: [app.py](file:///c:/Users/Usuario/Desktop/Lazaro%2018-09-25/Karaoke/SmartokePy/app.py#L296-L302) — SEM ALTERAÇÃO. Mantém `handler_class=WebSocketHandler`.

### 2.5. Como aplicar patches no Dell (SSH) sem precisar de SCP
Sempre usar `python3 -c` ou `python3 <<PYEOF` inline (trocar `os.sep` + `pathlib`) em vez de comandos bash com backtick que quebram no PowerShell/SSH Windows.

Exemplo padrão:
```bash
cd /opt/Karaoke/SmartokePy && python3 <<PYEOF
import pathlib
p = pathlib.Path('static/js/splash.js')
s = p.read_text().replace("const playbackStartTimeout = 20000","const playbackStartTimeout = 90000")
p.write_text(s); print("OK")
PYEOF
sudo systemctl restart smartokepy
```

---

## 3. Banco de dados e Login de Mesa (Karaoke Guard — porta 3001)

### 3.1. Localização do SQLite
```
/opt/Karaoke/acess-karaoke/data/karaoke-auth.sqlite
```

**Banco é aberto pela API Node com `node:sqlite`**: dá `ExperimentalWarning: The node:sqlite module is an experimental feature`. Isso é normal, não é erro. O banco é lido por `build-server/api/lib/database.js`.

### 3.2. Estrutura da tabela `mesas` (O QUE DAVA ERRO "MESA NÃO ACHADA")
```sql
CREATE TABLE mesas (
  id TEXT PRIMARY KEY,
  numero INTEGER UNIQUE NOT NULL,
  codigo TEXT UNIQUE NOT NULL,     -- slug tipo 'mesa-1' (NÃO É 'Mesa1', NÃO É 'mesa1')
  descricao TEXT,
  ativo INTEGER DEFAULT 1,
  senha TEXT,                      -- COLUNA QUE FALTAVA (criada com ALTER TABLE)
  criado_em TEXT,
  atualizado_em TEXT
);
```

**ERRO RAIZ 1 — coluna `senha` não existia**
O código legado presumia `senha` na projeção. Ao rodar `SELECT * FROM mesas WHERE codigo = ?` ou joins que esperavam a coluna, SQLite dava `sqlite3.OperationalError: no such column: senha`.

**Correção (uma vez só):**
```sql
ALTER TABLE mesas ADD COLUMN senha TEXT;
```

**ERRO RAIZ 2 — campo `codigo` foi inicializado como `Mesa1` em vez de `mesa-1`**
A API usa duas funções:
```js
// build-server/api/lib/tables.js
export function normalizeTableCode(input) {
    return input.normalize('NFD').replace(/[\u0300-\u036f]/g, '')
      .toLowerCase().trim().replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '').replace(/-{2,}/g, '-');
}
export function buildDefaultTableCode(numero) {
    const normalized = normalizeTableCode(numero);
    return normalized ? `mesa-${normalized}` : '';   // '1' → 'mesa-1'
}
```
E depois `getTableByCode(code)` = `SELECT * FROM mesas WHERE codigo = ?`.
Se tu tiver `codigo='Mesa1'` e o usuário digitar `1` ou passar `mesa-1` via `buildDefaultTableCode('1')`, **nunca bate**. A query retorna null, página mostra `"Mesa não achada"`.

**Correção para 30 mesas (uma vez só, via python inline):**
```python
import sqlite3
conn = sqlite3.connect('/opt/Karaoke/acess-karaoke/data/karaoke-auth.sqlite')
c = conn.cursor()
for n in range(1,31):
    slug = f'mesa-{n}'
    senha = f'Mesa{n}'
    c.execute("UPDATE mesas SET codigo=?, senha=? WHERE numero=?", (slug, senha, n))
conn.commit(); conn.close()
```

### 3.3. Senha vs. Código de mesa — IMPORTANTE (não confunde mais)
- `mesas.codigo` = slug identificador (ex: `mesa-1`). Usado para `mesaCodigo` no payload e na URL `/mesa/mesa-1`.
- `mesas.senha` = NÃO É a senha do login do cantor. A senha no login é do `users` (o cantor cria automaticamente na primeira vez). O `mesas.senha` só existe por legado e opcionalmente pode ser mostrada como "PIN da mesa" no QR.
- Login do portal: **o endpoint é `/api/auth/login`** (NÃO É `/api/login`, deu 404 antes).

Exemplo de login de cliente (Node):
```js
await fetch('http://127.0.0.1:3001/api/auth/login', {
  method:'POST',
  headers:{'Content-Type':'application/json'},
  body: JSON.stringify({
    login:'CantorTeste',
    senha:'senha123',      // primeira vez: cria usuário automaticamente com essa senha
    mesaCodigo:'mesa-1'
  })
});
// resposta: 200 + Set-Cookie: karaoke_session=s%3A...
```

### 3.4. Token `tok` (base64url) na URL de QR de mesa
Estrutura:
```
Buffer.from(JSON.stringify({
  mid:'mesa-1',                  // id da mesa slug
  n:'Mesa 1',                    // nome display
  ts:Date.now(),                 // timestamp
  v:1                            // versão
})).toString('base64url');
```

O token é decodado só no cliente (React) para pré-preencher a tela de boas-vindas — não é validador de segurança. A autenticação real é o cookie `karaoke_session`.

### 3.5. Exemplos de links de mesa (Mesa 1)
- **LOCAL (WiFi da casa):** `http://192.168.15.9:3001/mesa/mesa-1?tok=...`
- **PÚBLICO (Cloudflare Tunnel):** `https://portal.thermowatch.com.br/mesa/mesa-1?tok=...`

---

## 4. Build do Portal acess-karaoke (React 19 + Vite 6.4.3)

### 4.1. Onde fica
```
/opt/Karaoke/acess-karaoke
├── src/                  # fonte React
├── vite.config.ts        # build do SPA (dist/)
├── tsup.config.*         # build do Node API (build-server/)
├── dist/                 # SPA estático servido pela API
└── build-server/api/     # API Node (porta 3001)
```

### 4.2. Rebuild DEFINITIVO (sempre usar esses 2 passos, ou o `npm run build` completo)
```bash
cd /opt/Karaoke/acess-karaoke
# Build completo (cliente + API)
npm run build
# OU só cliente (mais rápido para ajustes de ícones/CSS):
npm run build:client
# OU só API:
npm run build:server
```

Depois SEMPRE restarta (pois a API Node roda do `build-server/` compilado):
```bash
sudo systemctl restart karaoke-guard
sudo systemctl status karaoke-guard --no-pager -n 3
```

### 4.3. Trapaças do build que quebraram o app e levaram HORAS para descobrir

#### 4.3.1. Ícones lucide-react apareciam como "bolinhas pretas vazias" (SVG paths sumindo)
`lucide-react@0.511.0` usa componentes React que retornam `<svg><path d="..." ... /></svg>`, tudo baseado em stroke.
Sintoma: botões com ícone apareciam como um "ponto preto minúsculo" no canto (stroke width 2px em coordenadas 0,0 ou path era vazio).

**Hipóteses testadas e DESCARTADAS:**
- ❌ Classes Tailwind `.h-4 .w-4 .text-slate-200` não estavam sendo aplicadas → CSS com `!important` e `svg.lucide { width/height: 1rem }` appendado no `index.css` — não resolveu.
- ❌ Tree-shaking do Vite matando exports não usados → `strings dist/assets/*.js | grep -c User`, `QrCode` etc deu 0. Engano: Rollup minifica os nomes das variáveis/componentes, não apaga o código SVG em si.

**Hipótese que parecia certa mas quebrou TUDO:**
- ⚠️ Trocar `jsxRuntime: "classic"` no `@vitejs/plugin-react` (pensando que SWC do React 19 + Vite "zero runtime transforms" estavam matando `<path>`). Ao fazer isso, o build assume que `React` está disponível em **todos os arquivos .tsx** como `import React from 'react'`. Como nenhum arquivo tinha esse import (React 19 / jsx-runtime automático não precisa), o bundle final tentava acessar `React.createElement` e dava `ReferenceError: React is not defined`. O React não monta NENHUM componente — a página fica só com fundo preto/azul degradê (só o CSS raiz aplicou, mas JS abortou antes de renderizar).

**Como diagnosticar React is not defined sem F12:**
1. Abre a página, espera 2s
2. `curl -s http://192.168.15.9:3001/assets/index-*.js | head -c 1024` — hash deve ter mudado
3. Ou: no DevTools Console → vai aparecer `ReferenceError: React is not defined at index-XXX.js`

**Solução final correta para ícones (ATENÇÃO):**
Não usar `jsxRuntime: "classic"` a menos que vá adicionar `import React from 'react'` em TODOS os `.tsx` do `src/` (injetar via plugin Babel, ou global inject). A abordagem segura:
1. **Deixar jsxRuntime no default automático** (não declarar no vite.config.ts)
2. **Garantir que ícones não usem `currentColor` dependente de `color` CSS**: adicionar `stroke="#e2e8f0"` explícito, ou fazer um wrapper `IconPadrao` no `AppShell` que dá `.h-4 .w-4 .text-slate-200` garantido.
3. **Fazer purge do Tailwind não apagar classes utilitárias dos ícones**: garantir `content` no `tailwind.config.ts` cobre `src/**/*.{ts,tsx}`.

**Se tu precisar mesmo forçar classic runtime SEM CDN (não recomendado, fallback apenas):**
Adicionar um plugin Vite no `vite.config.ts` com `transformIndexHtml` para injetar `<script>` de React UMD **antes** do `<script type="module" src="...bundle"></script>`, OU adicionar `import React from 'react'` + `import * as ReactDOM from 'react-dom/client'` no topo de `src/main.tsx` e usar Babel plugin `babel-plugin-react-require` para auto-injetar nos demais.

#### 4.3.2. Não confundir: pasta `dist/` do client vs `build-server/` da API
Se rodar só `npm run build:client`, atualiza só o `dist/` (HTML/CSS/JS). A API Node continua rodando do `build-server/` antigo. Se mexer em rotas, middlewares ou database, tem que rodar `npm run build` (ou `build:server` separado) e restarta.

### 4.4. Como ver os logs do karaoke-guard
```bash
# ultimos 10 minutos
sudo journalctl -u karaoke-guard --since "10 min ago" --no-pager -l -n 200
# "seguir" ao vivo
sudo journalctl -u karaoke-guard -f
```
Logs HTTP: a API já loga `HTTP 200 POST /api/auth/login (10ms)` etc. Se der 404 em `/api/login` é pq a rota é `/api/auth/login` (rotas ficam em `build-server/api/app.js`: `app.use('/api/auth', authRoutes)`, `app.use('/api/operations', operationsRoutes)` etc).

### 4.5. Erros comuns de SSH/powershell
- PowerShell aliases `curl` = `Invoke-WebRequest` (não compatível com curl Linux). Se tu rodar comando SSH do PowerShell local (`RunCommand ssh ...`) e usar `curl`, melhor usar Node `http.request` em vez de curl — também evita problema de escape de aspas/backtick.
- Bash heredoc `<<'EOF'` (com aspas simples) = sem interpolação (usar para scripts Node inline). `<<EOF` sem aspas = bash interpola `$var` e quebra JavaScript com `${}`. Sempre usar `<<'NODEOF'` nos scripts Node.
- PowerShell não suporta `&&`. Usar `;` e depois checar `if ($LASTEXITCODE -eq 0)`. Nunca colar scripts do DevTools Console no SSH bash (HTML + backtick vai dar 3 erros de sintaxe seguidos).

---

## 5. Rotas importantes do Karaoke Guard (porta 3001)

| Rota | Propósito |
|---|---|
| `POST /api/auth/login` body `{login, senha, mesaCodigo}` | Login do cantor (cria user auto na 1a vez). Retorna cookie `karaoke_session`. |
| `GET  /api/auth/me` | Perfil do usuário logado (ou 401). |
| `POST /api/auth/logout` | Limpa sessão. |
| `GET  /api/operations/overview` | Dados do painel de operação (Admin/Operador apenas). Usuário comum = 403. |
| `POST /api/operations/tables /mesa/:id/qr /mesa/:id/token` | CRUD e geração de QR/Token. |
| `GET  /` | SPA de login. |
| `GET  /operacao` | Painel administrativo (precisa de papel staff). |
| `GET  /mesa/:codigo` | Tela do cliente (mesa). |
| `GET  /mesa/:codigo?tok=...` | Tela do cliente com pré-preenchimento. |
| `GET  /print/mesa/:numero` | Folha de impressão da mesa (QR + link + senha). |

---

## 6. Comandos de checagem rápida (sempre rodar antes de dizer "acabou")

```bash
# Services vivos
sudo systemctl is-active smartokepy karaoke-guard

# Portas escutando
ss -ltnp | grep -E ':(3001|5555)'

# Contrato do Pikaraoke (mede latência — deve ser < 500 ms)
time curl -sS http://127.0.0.1:5555/maestro/api/contract -o /dev/null -w '%{http_code}\n'

# Login de mesa via API (deve dar 200)
node <<'NODEOF'
const http=require('http');
const b=JSON.stringify({login:'CantorTeste',senha:'senha123',mesaCodigo:'mesa-1'});
const req=http.request({host:'127.0.0.1',port:3001,path:'/api/auth/login',method:'POST',
  headers:{'Content-Type':'application/json','Content-Length':b.length}},r=>{
    let d='';r.on('data',c=>d+=c);r.on('end',()=>console.log('STATUS',r.statusCode,d.slice(0,120)));
  });
req.write(b);req.end();
NODEOF

# Build hash do bundle (verifica se realmente mudou)
ls -la /opt/Karaoke/acess-karaoke/dist/assets/index-*.js
```

---

## 7. Checklist de deploy (antes de entregar)

- [ ] `sudo systemctl restart smartokepy karaoke-guard` — ambos `active (running)`
- [ ] `ss -ltnp | grep -E ':(3001|5555)'` — portas OK
- [ ] `time curl -sS 127.0.0.1:5555/maestro/api/contract` — < 500 ms
- [ ] `POST /api/auth/login mesa-1` — 200
- [ ] `http://192.168.15.9:3001/operacao` carrega **sem** tela preta (sem `React is not defined` no Console)
- [ ] Ícones dos 4 botões de Ação (QR, Link, Power, Printer) e `+ Cadastrar` aparecem **sem** bolinhas pretas
- [ ] Link local mesa-1 abre tela de login do cliente
- [ ] Link público portal.thermowatch.com.br/mesa/mesa-1?tok=... abre (túnel Cloudflare ativo)
- [ ] Push do git com todos os arquivos alterados (Splash, Dell sync, network maestro, RUNBOOK novo)
