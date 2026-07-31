# SmartokePy - Instalacao, Recuperacao e Replica do Ambiente

Este arquivo deixa o ambiente pronto para ser recriado no Raspberry Pi e no Dell sem depender do historico do chat.

## 1. Arquitetura Final

- Raspberry Pi:
  - SmartokePy
  - splash/kiosk
  - ngrok do portal
  - IP Wi-Fi: `192.168.15.5`
  - IP cabo ponto a ponto: `10.10.10.2`
- Dell OptiPlex:
  - armazenamento das musicas
  - servidor NFS
  - desligamento remoto do Raspberry
  - IP Wi-Fi: `192.168.15.6`
  - IP cabo ponto a ponto: `10.10.10.1`
- Biblioteca de musicas:
  - Dell: `/mnt/hd500/pikaraoke-songs`
  - Raspberry: `/home/pi/pikaraoke-songs` via NFS

## 2. Arquivos do Projeto Alterados

Mudancas de codigo feitas no repositorio:

- `lib/download_manager.py`
  - bloqueio de download duplicado
  - filtro adicional para nao-karaoke
  - analise de link direto via metadata
  - timeout de download travado
  - mensagens claras de erro para o usuario
  - remocao de arquivo corrompido/invalido
- `lib/ffmpeg.py`
  - `validate_media_file()` para checagem de integridade
- `lib/queue_manager.py`
  - fila justa por rodadas
  - 1 pessoa por rodada quando houver multiplos cantores
- `lib/youtube_dl.py`
  - busca mais forte para karaoke/playback
  - heuristica para priorizar karaoke e rejeitar resultados ruins
  - leitura de metadata para link direto
- `routes/search.py`
  - busca passa a usar filtro de karaoke do backend
- `routes/socket_events.py`
  - tratamento melhor para encerramento precoce do player
- `static/js/splash.js`
  - resiliencia maior para HLS/player e reconexao
- `debug-smartokepy-service-term.md`
  - registro de debug da sessao

Commit principal:

- `ebffd1d` - `Improve karaoke download filtering and fair queue rotation`

## 3. Raspberry Pi - Configuracao de Sistema

### 3.1 Pacotes base

```bash
sudo apt update
sudo apt install -y nfs-common openssh-server
```

### 3.2 Rede ponto a ponto

```bash
CON_NAME=$(nmcli -t -f NAME,DEVICE connection show | awk -F: '$2=="eth0"{print $1; exit}')
sudo nmcli connection modify "$CON_NAME" connection.autoconnect yes ipv4.method manual ipv4.addresses 10.10.10.2/24 ipv4.gateway "" ipv4.dns "" ipv6.method ignore
sudo nmcli connection up "$CON_NAME"
```

### 3.3 Montagem NFS no Raspberry

Linha correta em `/etc/fstab`:

```fstab
10.10.10.1:/mnt/hd500/pikaraoke-songs /home/pi/pikaraoke-songs nfs vers=3,_netdev,nofail,x-systemd.after=network-online.target,timeo=14,retrans=3 0 0
```

Aplicacao:

```bash
sudo mkdir -p /home/pi/pikaraoke-songs
sudo mount -a
findmnt /home/pi/pikaraoke-songs
```

### 3.4 Servico SmartokePy

Servico principal:

`/etc/systemd/system/smartokepy.service`

Drop-in importante:

`/etc/systemd/system/smartokepy.service.d/wait-for-ssd.conf`

Conteudo esperado:

```ini
[Unit]
After=network-online.target remote-fs.target
Wants=network-online.target remote-fs.target
RequiresMountsFor=/home/pi/pikaraoke-songs
```

Recarregar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now smartokepy
sudo systemctl restart smartokepy
systemctl status smartokepy --no-pager | head -n 20
```

### 3.5 Kiosk/Splash

Servico:

`/etc/systemd/system/smartokepy-kiosk.service`

URL correta:

```text
http://127.0.0.1:5555/karaoke/splash
```

### 3.6 ngrok do portal

Servico:

`/etc/systemd/system/karaoke-internet.service`

Conteudo final:

```ini
[Unit]
Description=Link de Internet do Portal (Ngrok)
After=network.target

[Service]
Type=simple
User=pi
Environment=HOME=/home/pi
ExecStart=/snap/bin/ngrok http 8088
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Aplicar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now karaoke-internet
curl -s http://127.0.0.1:4040/api/tunnels
```

### 3.7 Maestro de Rede separado

Servico sugerido:

`/etc/systemd/system/network-maestro.service`

Arquivo base no projeto:

`network-maestro.service.example`

Healthcheck local:

```text
http://127.0.0.1:5560/health
```

Aplicar:

```bash
sudo cp /home/pi/SmartokePy/network-maestro.service.example /etc/systemd/system/network-maestro.service
sudo systemctl daemon-reload
sudo systemctl enable --now network-maestro.service
systemctl status network-maestro.service --no-pager -l | head -n 20
curl http://127.0.0.1:5560/health
```

Instalador automatizado:

```bash
chmod +x /home/pi/SmartokePy/scripts/install_network_maestro_rpi.sh
/home/pi/SmartokePy/scripts/install_network_maestro_rpi.sh
```

Atalho do Dell:

```ini
Exec=sh -c 'xdg-open "$0://$1:5560"' http 10.10.10.2
```

## 4. Dell - Configuracao de Sistema

### 4.1 Pacotes base

```bash
sudo apt update
sudo apt install -y openssh-server nfs-kernel-server
sudo systemctl enable --now ssh
sudo systemctl enable --now nfs-kernel-server
```

### 4.2 Disco de musicas

Ponto de montagem:

```text
/mnt/hd500
```

Linha correta em `/etc/fstab`:

```fstab
UUID=cca57246-25dc-4073-8552-70024884f2fa /mnt/hd500 ext4 defaults,nofail,noatime 0 2
```

Aplicar:

```bash
sudo mkdir -p /mnt/hd500
sudo mount -a
findmnt /mnt/hd500
```

### 4.3 Rede ponto a ponto

```bash
CON_NAME=$(nmcli -t -f NAME,DEVICE connection show | awk -F: '$2=="enp2s0"{print $1; exit}')
if [ -z "$CON_NAME" ]; then
  sudo nmcli connection add type ethernet ifname enp2s0 con-name karaoke-cabo
  CON_NAME=karaoke-cabo
fi
sudo nmcli connection modify "$CON_NAME" connection.autoconnect yes ipv4.method manual ipv4.addresses 10.10.10.1/24 ipv4.gateway "" ipv4.dns "" ipv6.method ignore
sudo nmcli connection up "$CON_NAME"
```

### 4.4 Export NFS

Arquivo:

`/etc/exports`

Conteudo final:

```exports
/mnt/hd500/pikaraoke-songs 10.10.10.2(rw,sync,no_subtree_check,no_root_squash)
```

Aplicar:

```bash
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server
sudo exportfs -v
```

## 5. Desligamento Automatico Dell -> Raspberry

### 5.1 Chave SSH do Dell para o Raspberry

No Dell:

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id -i ~/.ssh/id_ed25519.pub pi@10.10.10.2
ssh -o BatchMode=yes -o ConnectTimeout=5 pi@10.10.10.2 'echo SSH_OK'
```

### 5.2 Liberar poweroff sem senha no Raspberry

No Raspberry:

```bash
sudo tee /etc/sudoers.d/90-rpi-poweroff-from-dell >/dev/null <<'EOF'
pi ALL=(root) NOPASSWD: /usr/sbin/poweroff, /usr/bin/systemctl poweroff
EOF
sudo chmod 440 /etc/sudoers.d/90-rpi-poweroff-from-dell
sudo visudo -cf /etc/sudoers.d/90-rpi-poweroff-from-dell
```

### 5.3 Script no Dell

Arquivo:

`/usr/local/sbin/shutdown-rpi-safe`

Conteudo final:

```sh
#!/bin/sh
logger -t shutdown-rpi-safe "Requesting Raspberry Pi shutdown"
sudo -u pi -H timeout 12 ssh -o BatchMode=yes -o ConnectTimeout=5 pi@10.10.10.2 'sudo /usr/sbin/poweroff' || true
exit 0
```

Aplicar:

```bash
sudo chmod 755 /usr/local/sbin/shutdown-rpi-safe
```

### 5.4 Servico systemd no Dell

Arquivo:

`/etc/systemd/system/poweroff-rpi-on-dell-poweroff.service`

Conteudo final:

```ini
[Unit]
Description=Power off Raspberry Pi when Dell powers off
DefaultDependencies=no
Before=poweroff.target halt.target shutdown.target
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/shutdown-rpi-safe
TimeoutStartSec=15

[Install]
WantedBy=poweroff.target
WantedBy=halt.target
WantedBy=shutdown.target
```

Aplicar:

```bash
sudo systemctl daemon-reload
sudo systemctl enable poweroff-rpi-on-dell-poweroff.service
```

Teste controlado:

```bash
sudo systemctl start poweroff-rpi-on-dell-poweroff.service
```

## 6. Comandos de Deploy do Codigo para o Raspberry

### 6.1 Descobrir caminhos reais dos modulos instalados

```bash
python3 -c "import pikaraoke.lib.queue_manager as m; print(m.__file__)"
python3 -c "import pikaraoke.lib.download_manager as m; print(m.__file__)"
python3 -c "import pikaraoke.lib.youtube_dl as m; print(m.__file__)"
python3 -c "import pikaraoke.lib.ffmpeg as m; print(m.__file__)"
```

### 6.2 Copiar do Windows para o Raspberry

```powershell
scp "C:\Users\Usuario\Desktop\Lazaro 18-09-25\Karaoke\SmartokePy\lib\queue_manager.py" pi@192.168.15.5:/tmp/queue_manager.py
scp "C:\Users\Usuario\Desktop\Lazaro 18-09-25\Karaoke\SmartokePy\lib\download_manager.py" pi@192.168.15.5:/tmp/download_manager.py
scp "C:\Users\Usuario\Desktop\Lazaro 18-09-25\Karaoke\SmartokePy\lib\youtube_dl.py" pi@192.168.15.5:/tmp/youtube_dl.py
scp "C:\Users\Usuario\Desktop\Lazaro 18-09-25\Karaoke\SmartokePy\lib\ffmpeg.py" pi@192.168.15.5:/tmp/ffmpeg.py
scp "C:\Users\Usuario\Desktop\Lazaro 18-09-25\Karaoke\SmartokePy\routes\search.py" pi@192.168.15.5:/tmp/search.py
```

### 6.3 Aplicar no Raspberry

```bash
QM="/home/pi/.local/lib/python3.13/site-packages/pikaraoke/lib/queue_manager.py"
DM="/home/pi/.local/lib/python3.13/site-packages/pikaraoke/lib/download_manager.py"
YT="/home/pi/.local/lib/python3.13/site-packages/pikaraoke/lib/youtube_dl.py"
FF="/home/pi/.local/lib/python3.13/site-packages/pikaraoke/lib/ffmpeg.py"
SR="/home/pi/.local/lib/python3.13/site-packages/pikaraoke/routes/search.py"

sudo cp "$QM" "$QM.bak.$(date +%Y%m%d-%H%M%S)"
sudo cp "$DM" "$DM.bak.$(date +%Y%m%d-%H%M%S)"
sudo cp "$YT" "$YT.bak.$(date +%Y%m%d-%H%M%S)"
sudo cp "$FF" "$FF.bak.$(date +%Y%m%d-%H%M%S)"
sudo cp "$SR" "$SR.bak.$(date +%Y%m%d-%H%M%S)"

sudo cp /tmp/queue_manager.py "$QM"
sudo cp /tmp/download_manager.py "$DM"
sudo cp /tmp/youtube_dl.py "$YT"
sudo cp /tmp/ffmpeg.py "$FF"
sudo cp /tmp/search.py "$SR"

python3 -m py_compile "$QM" "$DM" "$YT" "$FF" "$SR"
sudo systemctl restart smartokepy
systemctl status smartokepy --no-pager | head -n 20
```

## 7. Monitoramento

### 7.1 Raspberry

```bash
mkdir -p ~/monitor-total && sudo sh -c 'nohup sh -c '"'"'while true; do echo "===== $(date) ====="; echo "[STATUS]"; systemctl is-active smartokepy; systemctl is-active karaoke-internet; findmnt -T /home/pi/pikaraoke-songs; ss -lntp | grep -E ":5555|:8088|:80|:4040" || true; free -h; vcgencmd get_throttled 2>/dev/null || true; echo; echo "[SMARTOKE]"; journalctl -u smartokepy -n 30 --no-pager | egrep -i "error|warning|skip|splash screen closed|traceback|ffmpeg|ended|starting|ending" || true; echo; echo "[KERNEL]"; dmesg -T | tail -n 120 | egrep -i "nfs|rpc|mount|disconnect|reset|I/O error|EXT4-fs|usb|over-current|oom|eth0|wlan0" || true; echo; sleep 60; done'"'"' > /home/pi/monitor-total/supervisao.log 2>&1 & echo $! > /home/pi/monitor-total/supervisao.pid'
```

### 7.2 Dell

```bash
mkdir -p ~/monitor-total && sudo sh -c 'nohup sh -c '"'"'while true; do echo "===== $(date) ====="; echo "[STATUS]"; findmnt /mnt/hd500; ss -lntp | grep :2049 || true; free -h; echo; echo "[NFS]"; journalctl -u nfs-kernel-server -n 30 --no-pager | egrep -i "error|warning|fail|denied|mount|export" || true; echo; echo "[KERNEL]"; dmesg -T | tail -n 120 | egrep -i "nfs|rpc|mount|disconnect|reset|I/O error|EXT4-fs|sda|sdb|oom" || true; echo; sleep 60; done'"'"' > /home/pi/monitor-total/supervisao.log 2>&1 & echo $! > /home/pi/monitor-total/supervisao.pid'
```

Parar:

```bash
sudo kill $(cat ~/monitor-total/supervisao.pid)
```

## 8. Limpeza de Biblioteca

### 8.1 Duplicatas exatas no Dell

```bash
sudo apt update
sudo apt install -y fdupes
sudo sh -c 'fdupes -r /mnt/hd500/pikaraoke-songs > /mnt/hd500/duplicatas_exatas_encontradas.txt'
sed -n '1,120p' /mnt/hd500/duplicatas_exatas_encontradas.txt
sudo fdupes -rdN /mnt/hd500/pikaraoke-songs
```

### 8.2 Atualizar biblioteca no Raspberry

```bash
sudo systemctl restart smartokepy
```

## 9. Testes Rapidos Depois da Reinstalacao

### 9.1 Raspberry

```bash
findmnt /home/pi/pikaraoke-songs
ls /home/pi/pikaraoke-songs | head
ss -lntp | grep :5555
systemctl status smartokepy --no-pager | head -n 20
curl -I http://127.0.0.1:5555/karaoke
```

### 9.2 Dell

```bash
findmnt /mnt/hd500
sudo exportfs -v
ss -lntp | grep :2049
ip -br a
```

### 9.3 ngrok

```bash
systemctl status karaoke-internet --no-pager
curl -s http://127.0.0.1:4040/api/tunnels
```

### 9.4 Fila e download

Validar estes cenarios:

- link normal nao-karaoke deve ser rejeitado
- link de musica ja existente deve avisar duplicata
- link invalido/privado deve mostrar motivo claro
- karaoke valido deve baixar e entrar na fila
- fila justa: se uma pessoa adiciona varias musicas e outras entram depois, toca 1 por rodada

## 10. URLs Importantes

- Portal local: `http://192.168.15.5`
- Karaoke local: `http://192.168.15.5:5555/karaoke`
- Splash local: `http://192.168.15.5:5555/karaoke/splash`
- Dell via SSH no Wi-Fi: `ssh pi@192.168.15.6`
- Dell via cabo a partir do Raspberry: `ssh pi@10.10.10.1`
- Raspberry via Wi-Fi: `ssh pi@192.168.15.5`
- Raspberry via cabo a partir do Dell: `ssh pi@10.10.10.2`

## 11. Licoes Importantes

- nao montar biblioteca por link simbolico se o `systemd` depender do mount
- no Raspberry, o caminho real de dependencia deve ser `/home/pi/pikaraoke-songs`
- o problema grave antigo era HD USB no Raspberry; a arquitetura correta eh NFS via Dell
- no desligamento automatico Dell -> Raspberry, o `ssh` do servico precisa rodar como usuario `pi` do Dell, nao como `root`
- para comandos longos com `nohup`, `systemd` e `sudo`, preferir blocos prontos e evitar colagens parciais
