# HarnessHub one-line installer for Windows (PowerShell 5.1+ and 7+).
# Usage:
#   irm https://raw.githubusercontent.com/Alpoxd/HarnessHub/main/install.ps1 | iex
#
# Environment variables:
#   HARNESSHUB_REPO            GitHub owner/repo (default: Alpoxd/HarnessHub)
#   HARNESSHUB_REF             Branch, tag, or commit SHA (default: main)
#   HARNESSHUB_SOURCE          Explicit package source URL or local path
#   HARNESSHUB_PYTHON          Python version spec for uv (default: >=3.11)
#   HARNESSHUB_NO_MODIFY_PATH  Set to 1 to skip PATH updates
#   HARNESSHUB_UNINSTALL       Set to 1 to uninstall harnesshub

param(
    [string]$Repo = "",
    [string]$Ref = "",
    [string]$Source = "",
    [string]$Python = "",
    [switch]$NoModifyPath,
    [switch]$Uninstall,
    [switch]$Help
)

& {
    param(
        [string]$ArgRepo,
        [string]$ArgRef,
        [string]$ArgSource,
        [string]$ArgPython,
        [bool]$ArgNoModifyPath,
        [bool]$ArgUninstall,
        [bool]$ArgHelp
    )

    $ErrorActionPreference = "Stop"

    try {
        [Net.ServicePointManager]::SecurityProtocol = (
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        )
    } catch {
        # Ignore on platforms where SecurityProtocolType.Tls12 is managed automatically.
    }

    function Write-HubInfo {
        param([string]$Message)
        Write-Host "[harnesshub] $Message"
    }

    function Write-HubWarn {
        param([string]$Message)
        Write-Warning "[harnesshub] $Message"
    }

    function Show-Usage {
        Write-Host @"
HarnessHub installer for Windows

Usage:
  irm https://raw.githubusercontent.com/Alpoxd/HarnessHub/main/install.ps1 | iex
  .\install.ps1 [OPTIONS]

Options:
  -Repo <owner/name>   GitHub repository (default: Alpoxd/HarnessHub)
  -Ref <ref>           Branch, tag, or commit SHA (default: main)
  -Source <src>        Explicit package URL, archive, wheel, or local path
  -Python <spec>       Python version spec for uv (default: >=3.11)
  -NoModifyPath        Do not modify User PATH or current session PATH
  -Uninstall           Uninstall harnesshub (keeps local data and profiles)
  -Help                Show this help message
"@
    }

    function Find-UvExecutable {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -and (Test-Path -LiteralPath $cmd.Source)) {
            return $cmd.Source
        }

        $candidateDirs = @()
        if ($env:UV_INSTALL_DIR) {
            $candidateDirs += $env:UV_INSTALL_DIR
        }
        if ($env:XDG_BIN_HOME) {
            $candidateDirs += $env:XDG_BIN_HOME
        }
        if ($env:USERPROFILE) {
            $localDir = Join-Path $env:USERPROFILE ".local"
            $candidateDirs += (Join-Path $localDir "bin")
            $cargoDir = Join-Path $env:USERPROFILE ".cargo"
            $candidateDirs += (Join-Path $cargoDir "bin")
        }
        if ($env:LOCALAPPDATA) {
            $progDir = Join-Path $env:LOCALAPPDATA "Programs"
            $candidateDirs += (Join-Path $progDir "uv")
        }

        foreach ($dir in $candidateDirs) {
            if (-not $dir) {
                continue
            }
            foreach ($name in @("uv.exe", "uv")) {
                $cand = Join-Path $dir $name
                if (Test-Path -LiteralPath $cand -PathType Leaf) {
                    return [System.IO.Path]::GetFullPath($cand)
                }
            }
        }

        return $null
    }

    function Test-PathContains {
        param(
            [string]$PathValue,
            [string]$Directory
        )
        if (-not $PathValue -or -not $Directory) {
            return $false
        }
        $pathSep = [System.IO.Path]::PathSeparator
        $target = [System.IO.Path]::GetFullPath($Directory).TrimEnd("\", "/")
        foreach ($entry in ($PathValue -split [regex]::Escape($pathSep))) {
            $trimmed = $entry.Trim()
            if (-not $trimmed) {
                continue
            }
            try {
                $normalized = [System.IO.Path]::GetFullPath($trimmed).TrimEnd("\", "/")
            } catch {
                $normalized = $trimmed.TrimEnd("\", "/")
            }
            if ($normalized.Equals($target, [System.StringComparison]::OrdinalIgnoreCase)) {
                return $true
            }
        }
        return $false
    }

    function Add-ToSessionPath {
        param([string]$Directory)
        if (-not $Directory) {
            return
        }
        if (-not (Test-PathContains -PathValue $env:Path -Directory $Directory)) {
            $pathSep = [System.IO.Path]::PathSeparator
            if ($env:Path) {
                $env:Path = "$Directory$pathSep$env:Path"
            } else {
                $env:Path = $Directory
            }
        }
    }

    function Install-Uv {
        param([bool]$SkipPath)

        $uvUrl = if ($env:HARNESSHUB_UV_INSTALL_URL) {
            $env:HARNESSHUB_UV_INSTALL_URL
        } else {
            "https://astral.sh/uv/install.ps1"
        }

        Write-HubInfo "uv not found; installing uv from $uvUrl..."
        $tmpScript = Join-Path ([System.IO.Path]::GetTempPath()) ("harnesshub-uv-install-" + [guid]::NewGuid().ToString("N") + ".ps1")
        $prevNoModify = $env:UV_NO_MODIFY_PATH
        try {
            Invoke-RestMethod -Uri $uvUrl -OutFile $tmpScript
            if ($SkipPath) {
                $env:UV_NO_MODIFY_PATH = "1"
            }
            & $tmpScript
        } finally {
            if ($SkipPath) {
                if ($null -eq $prevNoModify) {
                    Remove-Item Env:UV_NO_MODIFY_PATH -ErrorAction SilentlyContinue
                } else {
                    $env:UV_NO_MODIFY_PATH = $prevNoModify
                }
            }
            Remove-Item -LiteralPath $tmpScript -Force -ErrorAction SilentlyContinue
        }
    }

    if ($ArgHelp) {
        Show-Usage
        return
    }

    $effectiveRepo = if ($ArgRepo) {
        $ArgRepo
    } elseif ($env:HARNESSHUB_REPO) {
        $env:HARNESSHUB_REPO
    } else {
        "Alpoxd/HarnessHub"
    }

    $effectiveRef = if ($ArgRef) {
        $ArgRef
    } elseif ($env:HARNESSHUB_REF) {
        $env:HARNESSHUB_REF
    } else {
        "main"
    }

    $effectiveSource = if ($ArgSource) {
        $ArgSource
    } elseif ($env:HARNESSHUB_SOURCE) {
        $env:HARNESSHUB_SOURCE
    } else {
        "https://github.com/$effectiveRepo/archive/$effectiveRef.tar.gz"
    }

    $effectivePython = if ($ArgPython) {
        $ArgPython
    } elseif ($env:HARNESSHUB_PYTHON) {
        $env:HARNESSHUB_PYTHON
    } else {
        ">=3.11"
    }

    $effectiveNoModifyPath = $ArgNoModifyPath -or ($env:HARNESSHUB_NO_MODIFY_PATH -eq "1")
    $effectiveUninstall = $ArgUninstall -or ($env:HARNESSHUB_UNINSTALL -eq "1")

    if ($effectiveUninstall) {
        $uvBin = Find-UvExecutable
        if (-not $uvBin) {
            throw "[harnesshub] uv was not found; nothing to uninstall."
        }
        Write-HubInfo "Uninstalling harnesshub..."
        & $uvBin tool uninstall harnesshub
        if ($LASTEXITCODE -ne 0) {
            throw "[harnesshub] uv tool uninstall failed with exit code $LASTEXITCODE."
        }
        Write-HubInfo "Done. Local data and profiles in %LOCALAPPDATA%\harnesshub were kept."
        return
    }

    $uvBin = Find-UvExecutable
    if (-not $uvBin) {
        Install-Uv -SkipPath $effectiveNoModifyPath
        $uvBin = Find-UvExecutable
        if (-not $uvBin) {
            $pathSep = [System.IO.Path]::PathSeparator
            foreach ($scope in @("User", "Machine")) {
                $scopePath = [Environment]::GetEnvironmentVariable("Path", $scope)
                if ($scopePath) {
                    foreach ($entry in ($scopePath -split [regex]::Escape($pathSep))) {
                        $trimmed = $entry.Trim()
                        if ($trimmed -and -not $effectiveNoModifyPath) {
                            Add-ToSessionPath -Directory $trimmed
                        }
                    }
                }
            }
            $uvBin = Find-UvExecutable
        }
        if (-not $uvBin) {
            throw "[harnesshub] uv was installed, but uv executable could not be found in PATH or standard bin directories."
        }
    }

    Write-HubInfo "Installing HarnessHub from $effectiveSource..."
    & $uvBin tool install --force --reinstall-package harnesshub --python $effectivePython $effectiveSource
    if ($LASTEXITCODE -ne 0) {
        throw "[harnesshub] uv tool install failed with exit code $LASTEXITCODE."
    }

    $rawBinDir = (& $uvBin tool dir --bin | Select-Object -First 1)
    if ($LASTEXITCODE -ne 0 -or -not $rawBinDir) {
        throw "[harnesshub] Could not determine uv tool bin directory."
    }
    $binDir = [System.IO.Path]::GetFullPath($rawBinDir.Trim())
    $binPath = $null
    foreach ($candidateName in @("hhub.exe", "hhub", "harnesshub.exe", "harnesshub")) {
        $candidatePath = Join-Path $binDir $candidateName
        if (Test-Path -LiteralPath $candidatePath -PathType Leaf) {
            $binPath = $candidatePath
            break
        }
    }
    if (-not $binPath) {
        $binPath = Join-Path $binDir "hhub.exe"
    }

    if (-not $effectiveNoModifyPath) {
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if (-not (Test-PathContains -PathValue $userPath -Directory $binDir)) {
            Write-HubInfo "Adding $binDir to User PATH..."
            & $uvBin tool update-shell
            if ($LASTEXITCODE -ne 0) {
                Write-HubWarn "Could not update User PATH automatically. Add $binDir to PATH manually."
            }
        }
        Add-ToSessionPath -Directory $binDir
    }

    if (Test-Path -LiteralPath $binPath) {
        $version = (& $binPath --version 2>$null | Select-Object -First 1)
        if (-not $version) {
            $version = "unknown"
        }
        Write-HubInfo "Installed HarnessHub $(([string]$version).Trim()) -> $binPath"
    } else {
        throw "[harnesshub] Installation finished, but $binPath was not found."
    }

    if (-not (Test-PathContains -PathValue $env:Path -Directory $binDir)) {
        $pathSep = [System.IO.Path]::PathSeparator
        Write-Host ""
        Write-Host "To use hhub in this session, run:"
        Write-Host "  `$env:Path = `"$binDir$pathSep`$env:Path`""
        Write-Host "Or open a new terminal window."
    }

    Write-Host ""
    Write-Host "Quick start:"
    Write-Host "  hhub          # launch TUI (alias: harnesshub)"
    Write-Host "  hhub doctor   # check environment and clients"
    Write-Host "  hhub list     # list supported harnesses"
} -ArgRepo $Repo -ArgRef $Ref -ArgSource $Source -ArgPython $Python -ArgNoModifyPath $NoModifyPath.IsPresent -ArgUninstall $Uninstall.IsPresent -ArgHelp $Help.IsPresent
