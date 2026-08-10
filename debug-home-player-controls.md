# Debug Session: home-player-controls
- **Status**: [OPEN]
- **Issue**: O card "Player Control" na Home muda visualmente, mas os comandos locais nao afetam de verdade o player/splash.
- **Debug Server**: http://192.168.15.8:7777/event
- **Log File**: .dbg/trae-debug-log-home-player-controls.ndjson

## Reproduction Steps
1. Abrir o SmartokePy localmente.
2. Abrir a pagina `/splash`.
3. Colocar uma musica para tocar.
4. Na Home, usar os controles `pause`, `skip`, `volume` e `Change Key`.
5. Confirmar se o card muda visualmente, mas o player continua sem obedecer.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | O clique da Home chama a rota, mas a rota falha antes de emitir o evento | High | Low | Pending |
| B | A rota emite, mas o evento nao e entregue ao socket conectado da splash | High | Low | Pending |
| C | A splash recebe o evento, mas ignora por estado interno do player ou do video | Medium | Medium | Pending |
| D | O comando atualiza apenas o estado HTTP/`now_playing`, sem alterar o elemento de midia real | Medium | Medium | Pending |
| E | Home e splash estao ligadas a sockets/instancias diferentes | Medium | Medium | Pending |

## Log Evidence
Pending.

## Verification Conclusion
Pending.

## Instrumentation Points
- `templates/home.html`: loga clique e estado visual do card da Home.
- `routes/controller.py`: loga entrada e saida das rotas locais de controle.
- `static/js/splash.js`: loga conexao socket, papel master/slave e recebimento dos eventos.
