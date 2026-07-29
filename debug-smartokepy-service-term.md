# Debug Session: smartokepy-service-term
- **Status**: [OPEN]
- **Issue**: O `smartokepy.service` sobe corretamente, mas em alguns cenarios recebe `SIGTERM` logo depois e fica inacessivel. Em paralelo, ja houve evidencia anterior de subtensao e de disco nao montado no boot.
- **Debug Server**: Pending
- **Log File**: .dbg/trae-debug-log-smartokepy-service-term.ndjson

## Reproduction Steps
1. Reiniciar o Raspberry Pi.
2. Confirmar se `/media/ssd` foi montado.
3. Iniciar `smartokepy.service`.
4. Observar se o servico permanece ativo ou recebe `SIGTERM`.

## Hypotheses & Verification
| ID | Hypothesis | Likelihood | Effort | Evidence |
|----|------------|------------|--------|----------|
| A | Algum servico auxiliar (`smartokepy-kiosk`, `monitor-leds`, ou outro unit) esta parando o `smartokepy.service` explicitamente | High | Low | Pending |
| B | O `smartokepy.service` sobe antes de alguma dependencia real de runtime ficar pronta e depois outro processo corrige isso parando/reiniciando | Medium | Low | Pending |
| C | O `monitor-leds.service` em falha/auto-restart esta disparando efeito colateral sobre o `smartokepy.service` | Medium | Low | Pending |
| D | O disco/SSD ainda oscila na subida, e algum fluxo de supervisao encerra o servico ao detectar biblioteca inconsistente | Medium | Medium | Pending |
| E | A alimentacao ainda causa instabilidade intermitente e o encerramento do servico e sintoma secundario | Low | Medium | Pending |

## Log Evidence
- `smartokepy.service` recebeu `signal=TERM` sem stack interna de falha.
- Com `smartokepy-kiosk`, `karaoke-internet` e `monitor-leds` parados, o `smartokepy.service` permaneceu ativo.
- Apos `mount -a`, o SSD passou a montar em `/media/ssd` e o scan encontrou `301` musicas com `circuit_tripped=False`.
- `monitor-leds.service` entra em loop de falha com `lgpio.error: 'GPIO busy'` e `restart counter` crescente.
- `smartokepy-kiosk.service` esta configurado com `--app=http://127.0.0.1:5555/splash`, mas o SmartokePy anuncia a splash correta em `/karaoke/splash`.

## Verification Conclusion
Status parcial:
- **A**: Parcialmente confirmada. Ha evidencia de que servicos auxiliares alteram o comportamento geral do boot; com eles parados, o `smartokepy.service` fica estavel.
- **B**: Confirmada anteriormente para o SSD. O servico precisava aguardar o mount de `/media/ssd`.
- **C**: Confirmada. `monitor-leds.service` esta quebrado por conflito de GPIO e deve ser tratado como suspeito forte de interferencia no boot.
- **D**: Rejeitada no estado atual. Com o SSD montado, a biblioteca fica consistente (`301` musicas, `circuit_tripped=False`).
- **E**: Ainda inconclusiva como causa secundaria/historica; houve subtensao no passado, mas a evidencia atual mais forte e de ordem de boot + servicos auxiliares.

Atualizacao adicional:
- A URL do kiosk foi corrigida para `/karaoke/splash`.
- `monitor-leds.service` foi desabilitado com sucesso.
- As entradas `Stopping smartokepy.service` observadas as `22:58:49` e `23:00:57` coincidem com reinicios manuais executados durante o teste, nao com encerramento espontaneo confirmado.

Proximo passo recomendado:
1. Rodar um teste limpo sem executar `restart` manual durante a reproducao.
2. Verificar se o `smartokepy.service` permanece ativo por alguns minutos enquanto uma musica toca.
3. Se permanecer estavel, avancar para validacao funcional da musica que cortava no meio.
