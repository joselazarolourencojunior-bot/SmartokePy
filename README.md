# SmartokePy

Fork customizado do [PiKaraoke](https://github.com/vicwomg/pikaraoke) (base v1.20.0), adaptado para uso em producao no **Smart Solutions Karaoke** — sistema de karaokê com busca/download do YouTube, fila de cantores, transcodificacao HLS e tela de splash.

Roda em producao num **Dell OptiPlex 7010** (Ubuntu, 4GB RAM) servindo na porta **5555**, exposto externamente via Cloudflare Tunnel em `karaoke.thermowatch.com.br`.

## Principais customizacoes sobre o PiKaraoke original

- **Busca YouTube "nunca vazia"**: filtros hard/soft de palavras-chave (keywords positivas de karaoke/sertanejo, negativas hard como igreja/shorts/noticias), filtro de duracao 90s–600s, retry automatico em stdout vazio (rate limit), fallback em 3 niveis com banner no frontend.
- **Player de preview/stream robusto**: selecao de formatos com `acodec!=none` + `vcodec!=none` garantidos (nunca audio-only nem video-only), User-Agent Chrome fixo para evitar HTTP 403 do YouTube, fallback WebM.
- **Preload de transcode HLS**: FFmpeg transcodifica as proximas musicas da fila em background (`window_size=2`, `max_parallel=2` — apos upgrade para 18GB RAM; era 1 na era 4GB), com LEDs de status na fila (cinza/amarelo/verde/vermelho) e reparo automatico de transcode morto via evento `playback_stream_url_changed`.
- **Fila de cantores** ("lei da vez"): rotacao de cantores com regras anti-pulo.
- **Network Maestro**: app auxiliar de gerenciamento de WiFi/rede (`network_maestro_app.py`), ver `NETWORK_MAESTRO.md`.
- **WiFi Setup** e sincronizacao de rede Dell/Raspberry (`lib/dell_wifi_sync.py`, `lib/raspi_wifi_config.py`).

## Stack

- **Backend**: Flask + Flask-SocketIO + flask-smorest (Swagger em `/apidocs`) + gevent/gevent-websocket
- **Media**: FFmpeg + ffprobe (binarios de sistema), transcodificacao HLS, `ffmpeg-python`
- **Download/busca**: `yt-dlp` via subprocess (`python -m yt_dlp`)
- **Banco**: SQLite (biblioteca de musicas)
- **i18n**: Flask-Babel + `translations/`

## Instalacao

```bash
pip install -r requirements.txt
# sistema: ffmpeg + ffprobe no PATH
```

Este diretorio e o pacote `pikaraoke`. Para rodar, o **diretorio pai** precisa estar no `PYTHONPATH`:

```bash
cd ..
PYTHONPATH=. python -m pikaraoke.app --download-path /caminho/das/musicas
```

Acesso local: `http://<ip>:5555/`

## Producao (Dell OptiPlex 7010)

| Item | Valor |
|---|---|
| Servico systemd | `smartokepy.service` |
| Porta | `5555` |
| IP local | `192.168.15.9` |
| Runtime real | `/opt/Karaoke/pikaraoke` (**nao** `/opt/Karaoke/SmartokePy` — ver REGRA DE OURO no runbook) |
| Tunel Cloudflare | `karaoke.thermowatch.com.br` -> `127.0.0.1:5555` |

Documentacao operacional:

- `RUNBOOK_MIGRACAO_DELL_OPTI7010.md` — topologia completa, servicos, tuneis e patches aplicados
- `INSTALL_RECOVERY.md` — reinstalacao/recuperacao
- `NETWORK_MAESTRO.md` — app de rede
- `CHANGELOG.md` — historico upstream do PiKaraoke

## Estrutura

```
app.py                  # entrypoint (Flask + SocketIO + gevent WSGIServer)
karaoke.py              # engine central (fila, playback, preferencias)
version.py              # versao (base pikaraoke)
lib/                    # playback_controller, queue_manager, download_manager,
                        # youtube_dl (busca/filtros), ffmpeg, singer_manager, ...
routes/                 # blueprints Flask (search, queue, splash, stream, admin, ...)
templates/ static/      # frontend Jinja + JS (splash.js com player HLS)
translations/           # i18n
scripts/                # instaladores auxiliares (rpi/dell)
```

## Observacoes

- Scripts de desenvolvimento/deploy (`_deploy_*.py`, `_diag_*.py`, pastas `_RECOVERY_*`, logs e cookies do YouTube) ficam **fora do git** via `.gitignore` — sao artefatos locais de manutencao da maquina de producao.
- O portal de acompanhamento (**Karaoke Guard**) vive em outro projeto (`acess-karaoke`), servindo na porta 3001 com tunnel proprio.
