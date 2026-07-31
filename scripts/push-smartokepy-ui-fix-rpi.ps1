param(
    [string]$PiHost = "pi@192.168.15.5"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$LocalIndexTemplate = Join-Path $ProjectRoot "templates\index.html"
$LocalBaseTemplate = Join-Path $ProjectRoot "templates\base.html"
$LocalHomeTemplate = Join-Path $ProjectRoot "templates\home.html"
$LocalSplashTemplate = Join-Path $ProjectRoot "templates\splash.html"
$LocalQueueTemplate = Join-Path $ProjectRoot "templates\queue.html"
$LocalSplashJs = Join-Path $ProjectRoot "static\js\splash.js"
$LocalSpaNavigationJs = Join-Path $ProjectRoot "static\spa-navigation.js"
$LocalControllerRoute = Join-Path $ProjectRoot "routes\controller.py"
$LocalQueueRoute = Join-Path $ProjectRoot "routes\queue.py"
$LocalImagesRoute = Join-Path $ProjectRoot "routes\images.py"
$LocalCurrentApp = Join-Path $ProjectRoot "lib\current_app.py"
$LocalKaraokeCore = Join-Path $ProjectRoot "karaoke.py"
$LocalAppPy = Join-Path $ProjectRoot "app.py"
$LocalRemoteScript = Join-Path ([System.IO.Path]::GetTempPath()) "smartokepy-apply-rpi.sh"
$LocalStageDir = Join-Path ([System.IO.Path]::GetTempPath()) "smartokepy-deploy"
$RemoteTempDir = "smartokepy-deploy"

if (-not (Test-Path $LocalIndexTemplate)) {
    throw "Template nao encontrado em: $LocalIndexTemplate"
}

if (-not (Test-Path $LocalBaseTemplate)) {
    throw "Template nao encontrado em: $LocalBaseTemplate"
}

if (-not (Test-Path $LocalHomeTemplate)) {
    throw "Template nao encontrado em: $LocalHomeTemplate"
}

if (-not (Test-Path $LocalSplashTemplate)) {
    throw "Template nao encontrado em: $LocalSplashTemplate"
}

if (-not (Test-Path $LocalQueueTemplate)) {
    throw "Template nao encontrado em: $LocalQueueTemplate"
}

if (-not (Test-Path $LocalSplashJs)) {
    throw "Arquivo JS nao encontrado em: $LocalSplashJs"
}

if (-not (Test-Path $LocalSpaNavigationJs)) {
    throw "Arquivo JS nao encontrado em: $LocalSpaNavigationJs"
}

if (-not (Test-Path $LocalControllerRoute)) {
    throw "Arquivo route nao encontrado em: $LocalControllerRoute"
}

if (-not (Test-Path $LocalQueueRoute)) {
    throw "Arquivo route nao encontrado em: $LocalQueueRoute"
}

if (-not (Test-Path $LocalImagesRoute)) {
    throw "Arquivo route nao encontrado em: $LocalImagesRoute"
}

if (-not (Test-Path $LocalCurrentApp)) {
    throw "Arquivo lib nao encontrado em: $LocalCurrentApp"
}

if (-not (Test-Path $LocalKaraokeCore)) {
    throw "Arquivo core nao encontrado em: $LocalKaraokeCore"
}

if (-not (Test-Path $LocalAppPy)) {
    throw "Arquivo app nao encontrado em: $LocalAppPy"
}

Write-Host "[1/3] Enviando arquivos atualizados para o Raspberry..."

$remoteScript = @'
set -euo pipefail

DEPLOY_DIR="$HOME/smartokepy-deploy"
TMP_INDEX="$DEPLOY_DIR/index.html"
TMP_BASE="$DEPLOY_DIR/base.html"
TMP_HOME="$DEPLOY_DIR/home.html"
TMP_SPLASH="$DEPLOY_DIR/splash.html"
TMP_QUEUE="$DEPLOY_DIR/queue.html"
TMP_SPLASH_JS="$DEPLOY_DIR/splash.js"
TMP_SPA_NAV_JS="$DEPLOY_DIR/spa-navigation.js"
TMP_CONTROLLER_ROUTE="$DEPLOY_DIR/controller.py"
TMP_QUEUE_ROUTE="$DEPLOY_DIR/queue.py"
TMP_IMAGES_ROUTE="$DEPLOY_DIR/images.py"
TMP_CURRENT_APP="$DEPLOY_DIR/current_app.py"
TMP_KARAOKE_CORE="$DEPLOY_DIR/karaoke.py"
TMP_APP_PY="$DEPLOY_DIR/app.py"

rm -f "$HOME/queue.py" "$HOME/images.py" "$HOME/app.py"

echo "[RPI] Descobrindo pasta real do pacote instalado..."
PACKAGE_DIR="$(python3 -c 'import pathlib, pikaraoke; print(pathlib.Path(pikaraoke.__file__).resolve().parent)')"
APP_FILE="$(python3 -c 'import pathlib, pikaraoke.app; print(pathlib.Path(pikaraoke.app.__file__).resolve())')"
TEMPLATE_DIR="$PACKAGE_DIR/templates"
STATIC_JS_DIR="$PACKAGE_DIR/static/js"
ROUTES_DIR="$PACKAGE_DIR/routes"
LIB_DIR="$PACKAGE_DIR/lib"
TARGET_INDEX="$TEMPLATE_DIR/index.html"
TARGET_BASE="$TEMPLATE_DIR/base.html"
TARGET_HOME="$TEMPLATE_DIR/home.html"
TARGET_SPLASH="$TEMPLATE_DIR/splash.html"
TARGET_QUEUE="$TEMPLATE_DIR/queue.html"
TARGET_SPLASH_JS="$STATIC_JS_DIR/splash.js"
TARGET_SPA_NAV_JS="$PACKAGE_DIR/static/spa-navigation.js"
TARGET_CONTROLLER_ROUTE="$ROUTES_DIR/controller.py"
TARGET_QUEUE_ROUTE="$ROUTES_DIR/queue.py"
TARGET_IMAGES_ROUTE="$ROUTES_DIR/images.py"
TARGET_CURRENT_APP="$LIB_DIR/current_app.py"
TARGET_KARAOKE_CORE="$PACKAGE_DIR/karaoke.py"
TARGET_APP_PY="$APP_FILE"

echo "[RPI] index.html -> $TARGET_INDEX"
echo "[RPI] base.html  -> $TARGET_BASE"
echo "[RPI] home.html  -> $TARGET_HOME"
echo "[RPI] splash.html -> $TARGET_SPLASH"
echo "[RPI] queue.html  -> $TARGET_QUEUE"
echo "[RPI] splash.js   -> $TARGET_SPLASH_JS"
echo "[RPI] spa-navigation.js -> $TARGET_SPA_NAV_JS"
echo "[RPI] controller.py -> $TARGET_CONTROLLER_ROUTE"
echo "[RPI] queue.py    -> $TARGET_QUEUE_ROUTE"
echo "[RPI] images.py   -> $TARGET_IMAGES_ROUTE"
echo "[RPI] current_app.py -> $TARGET_CURRENT_APP"
echo "[RPI] karaoke.py  -> $TARGET_KARAOKE_CORE"
echo "[RPI] app.py      -> $TARGET_APP_PY"

echo "[RPI] Validando credenciais sudo..."
sudo -v

if install -m 0644 "$TMP_INDEX" "$TARGET_INDEX" 2>/dev/null; then
  echo "[RPI] index.html atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_INDEX" "$TARGET_INDEX"
  echo "[RPI] index.html atualizado com sudo."
fi

if install -m 0644 "$TMP_BASE" "$TARGET_BASE" 2>/dev/null; then
  echo "[RPI] base.html atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_BASE" "$TARGET_BASE"
  echo "[RPI] base.html atualizado com sudo."
fi

if install -m 0644 "$TMP_HOME" "$TARGET_HOME" 2>/dev/null; then
  echo "[RPI] home.html atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_HOME" "$TARGET_HOME"
  echo "[RPI] home.html atualizado com sudo."
fi

if install -m 0644 "$TMP_SPLASH" "$TARGET_SPLASH" 2>/dev/null; then
  echo "[RPI] splash.html atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_SPLASH" "$TARGET_SPLASH"
  echo "[RPI] splash.html atualizado com sudo."
fi

if install -m 0644 "$TMP_QUEUE" "$TARGET_QUEUE" 2>/dev/null; then
  echo "[RPI] queue.html atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_QUEUE" "$TARGET_QUEUE"
  echo "[RPI] queue.html atualizado com sudo."
fi

if install -m 0644 "$TMP_SPLASH_JS" "$TARGET_SPLASH_JS" 2>/dev/null; then
  echo "[RPI] splash.js atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_SPLASH_JS" "$TARGET_SPLASH_JS"
  echo "[RPI] splash.js atualizado com sudo."
fi

if install -m 0644 "$TMP_SPA_NAV_JS" "$TARGET_SPA_NAV_JS" 2>/dev/null; then
  echo "[RPI] spa-navigation.js atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_SPA_NAV_JS" "$TARGET_SPA_NAV_JS"
  echo "[RPI] spa-navigation.js atualizado com sudo."
fi

if install -m 0644 "$TMP_CONTROLLER_ROUTE" "$TARGET_CONTROLLER_ROUTE" 2>/dev/null; then
  echo "[RPI] controller.py atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_CONTROLLER_ROUTE" "$TARGET_CONTROLLER_ROUTE"
  echo "[RPI] controller.py atualizado com sudo."
fi

if install -m 0644 "$TMP_QUEUE_ROUTE" "$TARGET_QUEUE_ROUTE" 2>/dev/null; then
  echo "[RPI] queue.py atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_QUEUE_ROUTE" "$TARGET_QUEUE_ROUTE"
  echo "[RPI] queue.py atualizado com sudo."
fi

if install -m 0644 "$TMP_IMAGES_ROUTE" "$TARGET_IMAGES_ROUTE" 2>/dev/null; then
  echo "[RPI] images.py atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_IMAGES_ROUTE" "$TARGET_IMAGES_ROUTE"
  echo "[RPI] images.py atualizado com sudo."
fi

if install -m 0644 "$TMP_CURRENT_APP" "$TARGET_CURRENT_APP" 2>/dev/null; then
  echo "[RPI] current_app.py atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_CURRENT_APP" "$TARGET_CURRENT_APP"
  echo "[RPI] current_app.py atualizado com sudo."
fi

if install -m 0644 "$TMP_KARAOKE_CORE" "$TARGET_KARAOKE_CORE" 2>/dev/null; then
  echo "[RPI] karaoke.py atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_KARAOKE_CORE" "$TARGET_KARAOKE_CORE"
  echo "[RPI] karaoke.py atualizado com sudo."
fi

if install -m 0644 "$TMP_APP_PY" "$TARGET_APP_PY" 2>/dev/null; then
  echo "[RPI] app.py atualizado sem sudo."
else
  sudo install -m 0644 "$TMP_APP_PY" "$TARGET_APP_PY"
  echo "[RPI] app.py atualizado com sudo."
fi

echo "[RPI] Reiniciando SmartokePy..."
sudo systemctl restart smartokepy
sudo systemctl --no-pager --full status smartokepy | sed -n '1,20p'
echo "[RPI] Health local:"
curl -I http://127.0.0.1:5555/
'@

$remoteScriptLf = $remoteScript -replace "`r?`n", "`n"
[System.IO.File]::WriteAllText($LocalRemoteScript, $remoteScriptLf, [System.Text.Encoding]::ASCII)

if (Test-Path $LocalStageDir) {
    Remove-Item -Recurse -Force $LocalStageDir
}

New-Item -ItemType Directory -Path $LocalStageDir | Out-Null

Copy-Item $LocalIndexTemplate (Join-Path $LocalStageDir "index.html")
Copy-Item $LocalBaseTemplate (Join-Path $LocalStageDir "base.html")
Copy-Item $LocalHomeTemplate (Join-Path $LocalStageDir "home.html")
Copy-Item $LocalSplashTemplate (Join-Path $LocalStageDir "splash.html")
Copy-Item $LocalQueueTemplate (Join-Path $LocalStageDir "queue.html")
Copy-Item $LocalSplashJs (Join-Path $LocalStageDir "splash.js")
Copy-Item $LocalSpaNavigationJs (Join-Path $LocalStageDir "spa-navigation.js")
Copy-Item $LocalControllerRoute (Join-Path $LocalStageDir "controller.py")
Copy-Item $LocalQueueRoute (Join-Path $LocalStageDir "queue.py")
Copy-Item $LocalImagesRoute (Join-Path $LocalStageDir "images.py")
Copy-Item $LocalCurrentApp (Join-Path $LocalStageDir "current_app.py")
Copy-Item $LocalKaraokeCore (Join-Path $LocalStageDir "karaoke.py")
Copy-Item $LocalAppPy (Join-Path $LocalStageDir "app.py")
Copy-Item $LocalRemoteScript (Join-Path $LocalStageDir "smartokepy-apply-rpi.sh")

scp -r "$LocalStageDir" "${PiHost}:~/"

Write-Host "[2/3] Aplicando arquivos no pacote instalado e reiniciando SmartokePy..."
ssh -tt $PiHost "chmod +x ~/$RemoteTempDir/smartokepy-apply-rpi.sh && ~/$RemoteTempDir/smartokepy-apply-rpi.sh"

Write-Host "[3/3] Concluido."
