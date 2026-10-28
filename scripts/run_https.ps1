[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$backendDir = Join-Path $projectRoot 'backend'
$envFile = Join-Path $projectRoot '.env'

if (-not (Test-Path $envFile)) {
    throw "Arquivo .env não encontrado. Copie .env.example para .env e configure APP_API_TOKEN antes de iniciar."
}

$port = 8000
$apiTokenValue = ''
foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match '^\s*APP_PORT\s*=\s*(\d+)\s*$') {
        $port = [int]$Matches[1]
    }
    elseif ($line -match '^\s*APP_API_TOKEN\s*=\s*(.*)$') {
        $apiTokenValue = $Matches[1].Trim().Trim([char]34).Trim([char]39)
    }
}
if ($port -lt 1 -or $port -gt 65535) {
    throw "APP_PORT deve estar entre 1 e 65535."
}
if ($apiTokenValue.Length -lt 32) {
    throw "APP_API_TOKEN deve ter pelo menos 32 caracteres no .env. O valor não será exibido pelo script."
}

$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path $venvPython) {
    $pythonExe = $venvPython
}
else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python não encontrado. Instale Python e as dependências do projeto conforme o README."
    }
    $pythonExe = $pythonCommand.Source
}
& $pythonExe -c 'import fastapi, uvicorn' 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "FastAPI/Uvicorn não estão instalados neste Python. Execute: python -m pip install -r requirements.lock"
}

function Find-Mkcert {
    $command = Get-Command mkcert -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    if ($env:LOCALAPPDATA) {
        $wingetLink = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\mkcert.exe'
        if (Test-Path $wingetLink) {
            return $wingetLink
        }
    }
    return $null
}

$mkcertExe = Find-Mkcert
if (-not $mkcertExe) {
    $installAnswer = Read-Host 'mkcert não está instalado. Autoriza instalar pelo WinGet/Chocolatey? (S/N)'
    if ($installAnswer -notmatch '^(?i:s|sim)$') {
        throw 'Instalação cancelada. Nenhum certificado foi instalado.'
    }
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    $choco = Get-Command choco -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host 'Instalando mkcert pelo WinGet (FiloSottile.mkcert)...'
        & $winget.Source install --id FiloSottile.mkcert --exact --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) {
            throw "O WinGet não conseguiu instalar mkcert. Instale mkcert e execute este script novamente."
        }
    }
    elseif ($choco) {
        Write-Host 'Instalando mkcert pelo Chocolatey...'
        & $choco.Source install mkcert --yes
        if ($LASTEXITCODE -ne 0) {
            throw "O Chocolatey não conseguiu instalar mkcert. Instale mkcert e execute este script novamente."
        }
    }
    else {
        throw "mkcert não está instalado e WinGet/Chocolatey não foi encontrado. Instale mkcert pela documentação oficial (https://github.com/FiloSottile/mkcert) e execute novamente."
    }

    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machinePath;$userPath;$env:Path"
    $mkcertExe = Find-Mkcert
    if (-not $mkcertExe) {
        throw "mkcert foi instalado. Feche e reabra o PowerShell e execute este script novamente."
    }
}

$localAppData = $env:LOCALAPPDATA
if (-not $localAppData) {
    throw 'LOCALAPPDATA não está definido; não foi possível escolher um local privado para os certificados.'
}
$certDir = Join-Path $localAppData 'JSA_CRM\localhost-https'
New-Item -ItemType Directory -Path $certDir -Force | Out-Null
$certFile = Join-Path $certDir 'localhost.pem'
$keyFile = Join-Path $certDir 'localhost-key.pem'

Write-Host 'Instalando/conferindo a CA local no armazenamento confiável do usuário...'
Write-Host 'A CA permite confiar em certificados locais desta máquina. Não compartilhe rootCA-key.pem nem a chave privada gerada.'
$trustAnswer = Read-Host 'Autoriza adicionar/conferir a CA mkcert no armazenamento confiável do usuário? (S/N)'
if ($trustAnswer -notmatch '^(?i:s|sim)$') {
    throw 'Instalação da CA cancelada. Nenhum certificado foi ativado.'
}
& $mkcertExe -install
if ($LASTEXITCODE -ne 0) {
    throw 'mkcert -install falhou. Confira as permissões do Windows e tente novamente; o script não altera permissões nem eleva privilégios automaticamente.'
}

if (Test-Path $certFile) { Remove-Item -LiteralPath $certFile -Force }
if (Test-Path $keyFile) { Remove-Item -LiteralPath $keyFile -Force }
& $mkcertExe -cert-file $certFile -key-file $keyFile 'localhost' '127.0.0.1'
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $certFile) -or -not (Test-Path $keyFile)) {
    throw 'Não foi possível gerar o certificado local de localhost.'
}

Write-Host ''
Write-Host "Certificado local pronto. A chave privada fica fora do projeto em: $certDir"
Write-Host "Abrir no navegador: https://127.0.0.1:$port"
Write-Host 'O servidor ficará acessível somente neste computador; pressione Ctrl+C para parar.'
Write-Host 'Este HTTPS local não corrige TLS de saída do backend para a API InfinitePay.'
Write-Host 'Se o navegador já estava aberto, feche-o e abra novamente para atualizar a confiança do certificado.'
Write-Host ''

$uvicornArgs = @(
    '-m', 'uvicorn', 'app:app',
    '--host', '127.0.0.1',
    '--port', [string]$port,
    '--ssl-certfile', $certFile,
    '--ssl-keyfile', $keyFile,
    '--no-proxy-headers'
)
Push-Location $backendDir
try {
    & $pythonExe @uvicornArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Uvicorn terminou com código $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
