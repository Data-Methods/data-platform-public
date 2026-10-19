[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')]
    [ValidateLength(1, 63)]
    [string]$PlatformInstanceKey,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')]
    [ValidateLength(1, 63)]
    [string]$TemplateRepositoryKey,

    [ValidatePattern('^[a-zA-Z0-9-]{3,24}$')]
    [string]$KeyVaultName,

    [switch]$MigrateLegacyInfrastructureKey,

    [Parameter(DontShow = $true)]
    [string]$SshRoot = (
        Join-Path ([Environment]::GetFolderPath('UserProfile')) '.ssh/data-platform'
    ),

    [Parameter(DontShow = $true)]
    [string]$UserSshConfigPath = (
        Join-Path ([Environment]::GetFolderPath('UserProfile')) '.ssh/config'
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ManagedSshConfigPath = Join-Path $SshRoot 'template-access.conf'
$ManagedSshInclude = "Include `"$($ManagedSshConfigPath.Replace('\', '/'))`""
$KeyDirectory = Join-Path (Join-Path $SshRoot $PlatformInstanceKey) $TemplateRepositoryKey
$PrivateKeyPath = Join-Path $KeyDirectory 'template-read'
$PublicKeyPath = "$PrivateKeyPath.pub"
$SecretName = "dm-template-read-$TemplateRepositoryKey"
$LegacyPrivateKeyPath = Join-Path (Join-Path $SshRoot $PlatformInstanceKey) 'template-read'
$LegacySecretName = 'dm-template-read-key'

function Assert-Command {
    param([Parameter(Mandatory = $true)][string]$Name)

    if ($null -eq (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found on PATH."
    }
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [AllowEmptyString()]
        [string[]]$Arguments,
        [switch]$AllowFailure
    )

    $output = & $Command @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        $detail = if ([string]::IsNullOrWhiteSpace($text)) {
            'no command output'
        }
        else {
            $text.Split([Environment]::NewLine)[0]
        }
        throw "$Command failed with exit code $exitCode`: $detail"
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $text
    }
}

function Set-DirectoryPermissions {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ($IsWindows) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Invoke-External -Command 'icacls' -Arguments @(
            $Path,
            '/inheritance:r',
            '/grant:r',
            "${identity}:(OI)(CI)(F)"
        ) | Out-Null
        return
    }
    [System.IO.File]::SetUnixFileMode(
        $Path,
        [System.IO.UnixFileMode]::UserRead -bor
            [System.IO.UnixFileMode]::UserWrite -bor
            [System.IO.UnixFileMode]::UserExecute
    )
}

function Set-KeyPermissions {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Private
    )

    if ($IsWindows) {
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        Invoke-External -Command 'icacls' -Arguments @(
            $Path,
            '/inheritance:r',
            '/grant:r',
            "${identity}:(F)"
        ) | Out-Null
        return
    }
    $mode = [System.IO.UnixFileMode]::UserRead -bor
        [System.IO.UnixFileMode]::UserWrite
    if (-not $Private) {
        $mode = $mode -bor
            [System.IO.UnixFileMode]::GroupRead -bor
            [System.IO.UnixFileMode]::OtherRead
    }
    [System.IO.File]::SetUnixFileMode($Path, $mode)
}

function Ensure-Directory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
    }
    Set-DirectoryPermissions -Path $Path
}

function Write-TextAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value,
        [Parameter(Mandatory = $true)][bool]$Private
    )

    $parent = Split-Path -Parent $Path
    Ensure-Directory -Path $parent
    $temporary = Join-Path $parent ".$([IO.Path]::GetFileName($Path)).$([guid]::NewGuid()).tmp"
    try {
        [IO.File]::WriteAllText(
            $temporary,
            $Value,
            [Text.UTF8Encoding]::new($false)
        )
        Set-KeyPermissions -Path $temporary -Private $Private
        Move-Item -LiteralPath $temporary -Destination $Path -Force
        Set-KeyPermissions -Path $Path -Private $Private
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Get-PublicIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $fields = $Value.Trim().Split(
        [char[]]@(' ', "`t"),
        [StringSplitOptions]::RemoveEmptyEntries
    )
    if ($fields.Count -lt 2 -or $fields[0] -ne 'ssh-ed25519') {
        throw "$Label is not a valid Ed25519 public key."
    }
    return "$($fields[0]) $($fields[1])"
}

function Get-DerivedPublicKey {
    param([Parameter(Mandatory = $true)][string]$PrivateKey)

    $result = Invoke-External -Command 'ssh-keygen' -Arguments @(
        '-y', '-P', '', '-f', $PrivateKey
    )
    return Get-PublicIdentity -Value $result.Output -Label $PrivateKey
}

function Assert-SameKey {
    param(
        [Parameter(Mandatory = $true)][string]$First,
        [Parameter(Mandatory = $true)][string]$Second
    )

    if ((Get-DerivedPublicKey -PrivateKey $First) -ne
        (Get-DerivedPublicKey -PrivateKey $Second)) {
        throw "Template Read Private Keys do not match: '$First' and '$Second'. Nothing was rotated."
    }
}

function Ensure-PublicKey {
    param(
        [Parameter(Mandatory = $true)][string]$PrivateKey,
        [Parameter(Mandatory = $true)][string]$PublicKey
    )

    $derived = Get-DerivedPublicKey -PrivateKey $PrivateKey
    if (Test-Path -LiteralPath $PublicKey -PathType Leaf) {
        $recorded = Get-PublicIdentity -Value ([IO.File]::ReadAllText($PublicKey)) -Label $PublicKey
        if ($derived -ne $recorded) {
            throw "Template Read Keypair is inconsistent: '$PrivateKey' does not match '$PublicKey'."
        }
        Set-KeyPermissions -Path $PublicKey -Private $false
        return
    }

    $comment = "$PlatformInstanceKey $TemplateRepositoryKey template-read"
    Write-TextAtomically -Path $PublicKey -Value "$derived $comment`n" -Private $false
}

function Copy-LegacyInfrastructureKey {
    Ensure-Directory -Path $KeyDirectory
    Copy-Item -LiteralPath $LegacyPrivateKeyPath -Destination $PrivateKeyPath
    Set-KeyPermissions -Path $PrivateKeyPath -Private $true
    Ensure-PublicKey -PrivateKey $PrivateKeyPath -PublicKey $PublicKeyPath
    Write-Host 'Adopted the existing Infrastructure key without rotation; the legacy file remains in place.'
}

function New-TemplateReadKeypair {
    Ensure-Directory -Path $KeyDirectory
    $comment = "$PlatformInstanceKey $TemplateRepositoryKey template-read"
    $result = Invoke-External -Command 'ssh-keygen' -Arguments @(
        '-q',
        '-t', 'ed25519',
        '-N', '',
        '-C', $comment,
        '-f', $PrivateKeyPath
    )
    if ($result.ExitCode -ne 0) {
        throw 'ssh-keygen did not create the Template Read Keypair.'
    }
    Set-KeyPermissions -Path $PrivateKeyPath -Private $true
    Set-KeyPermissions -Path $PublicKeyPath -Private $false
}

function Test-KeyVaultSecret {
    param(
        [Parameter(Mandatory = $true)][string]$VaultName,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $result = Invoke-External -Command 'az' -Arguments @(
        'keyvault', 'secret', 'show',
        '--vault-name', $VaultName,
        '--name', $Name,
        '--query', 'id',
        '--output', 'tsv',
        '--only-show-errors'
    ) -AllowFailure
    if ($result.ExitCode -eq 0) {
        return $true
    }
    if ($result.Output -match '(?i)SecretNotFound|not found') {
        return $false
    }
    throw "Could not determine whether Key Vault secret '$VaultName/$Name' exists: $($result.Output)"
}

function Download-KeyVaultSecret {
    param(
        [Parameter(Mandatory = $true)][string]$VaultName,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    Write-TextAtomically -Path $Destination -Value '' -Private $true
    try {
        Invoke-External -Command 'az' -Arguments @(
            'keyvault', 'secret', 'download',
            '--vault-name', $VaultName,
            '--name', $Name,
            '--file', $Destination,
            '--encoding', 'utf-8',
            '--overwrite',
            '--output', 'none',
            '--only-show-errors'
        ) | Out-Null
    }
    finally {
        if (Test-Path -LiteralPath $Destination -PathType Leaf) {
            Set-KeyPermissions -Path $Destination -Private $true
        }
    }
}

function ConvertTo-PrivateKeyText {
    param([Parameter(Mandatory = $true)][string]$Value)

    # ssh-keygen refuses an OpenSSH private key without its final newline.
    $text = $Value.Replace("`r`n", "`n")
    if (-not $text.EndsWith("`n")) {
        $text += "`n"
    }
    return $text
}

function Convert-LegacySecretFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $value = [IO.File]::ReadAllText($Source)
    if ($value.TrimStart().StartsWith('-----BEGIN OPENSSH PRIVATE KEY-----')) {
        Write-TextAtomically -Path $Destination -Value (ConvertTo-PrivateKeyText -Value $value) -Private $true
        return
    }

    try {
        $payload = ConvertFrom-Json -InputObject $value
    }
    catch {
        throw "Legacy Key Vault secret '$LegacySecretName' is neither an OpenSSH private key nor valid JSON."
    }
    if (
        $null -eq $payload.private_key -or
        -not ($payload.private_key -is [string]) -or
        [string]::IsNullOrWhiteSpace($payload.private_key)
    ) {
        throw "Legacy Key Vault secret '$LegacySecretName' does not contain 'private_key'."
    }
    Write-TextAtomically -Path $Destination -Value (ConvertTo-PrivateKeyText -Value $payload.private_key) -Private $true
}

function Restore-KeyFromVault {
    param(
        [Parameter(Mandatory = $true)][string]$VaultName,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$Legacy
    )

    Ensure-Directory -Path $KeyDirectory
    $download = Join-Path $KeyDirectory ".template-read.$([guid]::NewGuid()).download"
    $candidate = Join-Path $KeyDirectory ".template-read.$([guid]::NewGuid()).candidate"
    try {
        Download-KeyVaultSecret -VaultName $VaultName -Name $Name -Destination $download
        Set-KeyPermissions -Path $download -Private $true
        if ($Legacy) {
            Convert-LegacySecretFile -Source $download -Destination $candidate
        }
        else {
            Move-Item -LiteralPath $download -Destination $candidate
            Set-KeyPermissions -Path $candidate -Private $true
        }
        Get-DerivedPublicKey -PrivateKey $candidate | Out-Null
        Move-Item -LiteralPath $candidate -Destination $PrivateKeyPath
        Set-KeyPermissions -Path $PrivateKeyPath -Private $true
        Ensure-PublicKey -PrivateKey $PrivateKeyPath -PublicKey $PublicKeyPath
    }
    finally {
        Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
    }
}

function Assert-LegacyVaultMatchesLocal {
    param([Parameter(Mandatory = $true)][string]$LocalKey)

    $download = Join-Path $KeyDirectory ".template-read.$([guid]::NewGuid()).download"
    $candidate = Join-Path $KeyDirectory ".template-read.$([guid]::NewGuid()).candidate"
    try {
        Download-KeyVaultSecret -VaultName $KeyVaultName -Name $LegacySecretName -Destination $download
        Convert-LegacySecretFile -Source $download -Destination $candidate
        Assert-SameKey -First $LocalKey -Second $candidate
    }
    finally {
        Remove-Item -LiteralPath $download -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
    }
}

function Assert-LocalMatchesVault {
    param(
        [Parameter(Mandatory = $true)][string]$VaultName,
        [Parameter(Mandatory = $true)][string]$Name,
        [string]$LocalKey = $PrivateKeyPath
    )

    $temporary = Join-Path $KeyDirectory ".template-read.$([guid]::NewGuid()).verify"
    try {
        Download-KeyVaultSecret -VaultName $VaultName -Name $Name -Destination $temporary
        Set-KeyPermissions -Path $temporary -Private $true
        $localIdentity = Get-DerivedPublicKey -PrivateKey $LocalKey
        $vaultIdentity = Get-DerivedPublicKey -PrivateKey $temporary
        if ($localIdentity -ne $vaultIdentity) {
            throw (
                "The local Template Read Private Key and Key Vault secret '$Name' do not match. " +
                'Neither copy was changed; use an explicit recovery or rotation procedure.'
            )
        }
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Save-KeyToVault {
    param(
        [Parameter(Mandatory = $true)][string]$VaultName,
        [Parameter(Mandatory = $true)][string]$Name
    )

    Invoke-External -Command 'az' -Arguments @(
        'keyvault', 'secret', 'set',
        '--vault-name', $VaultName,
        '--name', $Name,
        '--file', $PrivateKeyPath,
        '--encoding', 'utf-8',
        '--content-type', 'application/x-openssh-private-key',
        '--tags',
        "platform-instance-key=$PlatformInstanceKey",
        "template-repository-key=$TemplateRepositoryKey",
        '--output', 'none',
        '--only-show-errors'
    ) | Out-Null
}

function Get-ManagedKeyEntries {
    $entries = @()
    if (-not (Test-Path -LiteralPath $SshRoot -PathType Container)) {
        return $entries
    }

    foreach ($instanceDirectory in Get-ChildItem -LiteralPath $SshRoot -Directory) {
        if ($instanceDirectory.Name -notmatch '^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$') {
            continue
        }
        foreach ($templateDirectory in Get-ChildItem -LiteralPath $instanceDirectory.FullName -Directory) {
            if ($templateDirectory.Name -notmatch '^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$') {
                continue
            }
            $privateKey = Join-Path $templateDirectory.FullName 'template-read'
            if (-not (Test-Path -LiteralPath $privateKey -PathType Leaf)) {
                continue
            }
            $entries += [pscustomobject]@{
                HostAlias = "dm-$($instanceDirectory.Name)-$($templateDirectory.Name)"
                PrivateKey = $privateKey.Replace('\', '/')
            }
        }
    }
    return @($entries | Sort-Object HostAlias)
}

function Update-ManagedSshConfig {
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('# Managed by template-access.ps1. Do not edit by hand.')
    foreach ($entry in Get-ManagedKeyEntries) {
        $lines.Add('')
        $lines.Add("Host $($entry.HostAlias)")
        $lines.Add('    HostName github.com')
        $lines.Add('    User git')
        $lines.Add("    IdentityFile `"$($entry.PrivateKey)`"")
        $lines.Add('    IdentitiesOnly yes')
    }
    $lines.Add('')
    Write-TextAtomically -Path $ManagedSshConfigPath -Value (($lines -join "`n")) -Private $true
}

function Assert-EffectiveSshConfig {
    param([Parameter(Mandatory = $true)][string]$ConfigPath)

    $alias = "dm-$PlatformInstanceKey-$TemplateRepositoryKey"
    $settings = (Invoke-External -Command 'ssh' -Arguments @(
        '-G', '-F', $ConfigPath, $alias
    )).Output -split "`r?`n"
    $values = @{}
    foreach ($line in $settings) {
        if ($line -match '^(\S+)\s+(.+)$') {
            $name = $Matches[1].ToLowerInvariant()
            if (-not $values.ContainsKey($name)) {
                $values[$name] = @()
            }
            $values[$name] += $Matches[2]
        }
    }
    $expectedKey = $PrivateKeyPath.Replace('\', '/')
    $identities = @($values['identityfile'] | ForEach-Object { $_.Trim('"').Replace('\', '/') })
    if (
        $values['hostname'][0] -ne 'github.com' -or
        $values['user'][0] -ne 'git' -or
        $values['identitiesonly'][0] -ne 'yes' -or
        $identities.Count -ne 1 -or
        $identities[0] -ne $expectedKey
    ) {
        throw "The effective SSH configuration for '$alias' does not use only its dedicated key. Review conflicting Host rules before retrying."
    }
}

function Ensure-UserSshInclude {
    $parent = Split-Path -Parent $UserSshConfigPath
    Ensure-Directory -Path $parent
    if (Test-Path -LiteralPath $UserSshConfigPath) {
        $item = Get-Item -LiteralPath $UserSshConfigPath -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "SSH config '$UserSshConfigPath' is a symlink. Configure the Include manually; it was not replaced."
        }
    }
    $current = if (Test-Path -LiteralPath $UserSshConfigPath -PathType Leaf) {
        [IO.File]::ReadAllText($UserSshConfigPath)
    }
    else {
        ''
    }
    $newline = if ($current.Contains("`r`n")) { "`r`n" } else { "`n" }
    $expectedPath = $ManagedSshConfigPath.Replace('\', '/')
    $lines = [System.Collections.Generic.List[string]]::new()
    foreach ($line in $current -split "`r?`n") {
        $content = ($line -split '#', 2)[0].Trim()
        if ($content -match '^Include\s+(.+)$') {
            $candidate = $Matches[1].Trim().Trim('"').Replace('\', '/')
            if ($candidate -eq $expectedPath) {
                continue
            }
        }
        $lines.Add($line)
    }
    while ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq '') {
        $lines.RemoveAt($lines.Count - 1)
    }
    $updated = $ManagedSshInclude + $newline
    if ($lines.Count -gt 0) {
        $updated += $newline + ($lines -join $newline) + $newline
    }
    if ($current -eq $updated) {
        Assert-EffectiveSshConfig -ConfigPath $UserSshConfigPath
        return
    }

    $candidatePath = Join-Path $parent ".config.$([guid]::NewGuid()).candidate"
    try {
        Write-TextAtomically -Path $candidatePath -Value $updated -Private $true
        Assert-EffectiveSshConfig -ConfigPath $candidatePath
        Move-Item -LiteralPath $candidatePath -Destination $UserSshConfigPath -Force
        Set-KeyPermissions -Path $UserSshConfigPath -Private $true
    }
    finally {
        Remove-Item -LiteralPath $candidatePath -Force -ErrorAction SilentlyContinue
    }
}

function Get-PublicFingerprint {
    $result = Invoke-External -Command 'ssh-keygen' -Arguments @(
        '-l', '-E', 'sha256', '-f', $PublicKeyPath
    )
    $fields = $result.Output.Trim().Split(
        [char[]]@(' ', "`t"),
        [StringSplitOptions]::RemoveEmptyEntries
    )
    if ($fields.Count -lt 2 -or -not $fields[1].StartsWith('SHA256:')) {
        throw "Could not derive the public-key fingerprint from '$PublicKeyPath'."
    }
    return $fields[1]
}

Assert-Command -Name 'ssh-keygen'
Assert-Command -Name 'ssh'
if ($IsWindows) {
    Assert-Command -Name 'icacls'
}
if ($MigrateLegacyInfrastructureKey -and $TemplateRepositoryKey -ne 'data-platform-infrastructure') {
    throw '-MigrateLegacyInfrastructureKey applies only to data-platform-infrastructure.'
}
Ensure-Directory -Path $SshRoot
Ensure-Directory -Path $KeyDirectory

$localExists = Test-Path -LiteralPath $PrivateKeyPath -PathType Leaf
$legacyLocalExists = $TemplateRepositoryKey -eq 'data-platform-infrastructure' -and
    (Test-Path -LiteralPath $LegacyPrivateKeyPath -PathType Leaf)

$vaultHasCurrent = $false
$vaultHasLegacy = $false
if (-not [string]::IsNullOrWhiteSpace($KeyVaultName)) {
    Assert-Command -Name 'az'
    $vaultHasCurrent = Test-KeyVaultSecret -VaultName $KeyVaultName -Name $SecretName
    if ($TemplateRepositoryKey -eq 'data-platform-infrastructure') {
        $vaultHasLegacy = Test-KeyVaultSecret -VaultName $KeyVaultName -Name $LegacySecretName
    }
}

if ($MigrateLegacyInfrastructureKey -and -not $legacyLocalExists -and -not $vaultHasLegacy) {
    throw 'No legacy Infrastructure key was found locally or in the selected Key Vault.'
}
if (
    -not $MigrateLegacyInfrastructureKey -and -not $localExists -and
    -not $vaultHasCurrent -and ($legacyLocalExists -or $vaultHasLegacy)
) {
    throw 'A legacy Infrastructure key exists. Rerun with -MigrateLegacyInfrastructureKey to adopt it without rotation.'
}

if ($MigrateLegacyInfrastructureKey -and $legacyLocalExists -and $vaultHasLegacy) {
    Assert-LegacyVaultMatchesLocal -LocalKey $LegacyPrivateKeyPath
}
if ($MigrateLegacyInfrastructureKey -and $legacyLocalExists -and $vaultHasCurrent) {
    Assert-LocalMatchesVault -VaultName $KeyVaultName -Name $SecretName -LocalKey $LegacyPrivateKeyPath
}
if ($MigrateLegacyInfrastructureKey -and $localExists -and $legacyLocalExists) {
    Assert-SameKey -First $PrivateKeyPath -Second $LegacyPrivateKeyPath
}

if (-not $localExists -and $vaultHasCurrent) {
    Restore-KeyFromVault -VaultName $KeyVaultName -Name $SecretName -Legacy $false
    $localExists = $true
    Write-Host "Restored the Template Read Private Key from Key Vault secret '$SecretName'."
}
elseif (-not $localExists -and $MigrateLegacyInfrastructureKey -and $legacyLocalExists) {
    Copy-LegacyInfrastructureKey
    $localExists = $true
}
elseif (-not $localExists -and $MigrateLegacyInfrastructureKey -and $vaultHasLegacy) {
    Restore-KeyFromVault -VaultName $KeyVaultName -Name $LegacySecretName -Legacy $true
    $localExists = $true
    Write-Host "Restored the infrastructure key from legacy Key Vault secret '$LegacySecretName'."
}

if (-not $localExists) {
    New-TemplateReadKeypair
    $localExists = $true
    Write-Host 'Created a new Template Read Keypair.'
}

Set-KeyPermissions -Path $PrivateKeyPath -Private $true
Ensure-PublicKey -PrivateKey $PrivateKeyPath -PublicKey $PublicKeyPath
if ($MigrateLegacyInfrastructureKey -and $vaultHasLegacy) {
    Assert-LegacyVaultMatchesLocal -LocalKey $PrivateKeyPath
}

if (-not [string]::IsNullOrWhiteSpace($KeyVaultName)) {
    if ($vaultHasCurrent) {
        Assert-LocalMatchesVault -VaultName $KeyVaultName -Name $SecretName
        Write-Host "Verified the local key matches Key Vault secret '$SecretName'."
    }
    else {
        Save-KeyToVault -VaultName $KeyVaultName -Name $SecretName
        Write-Host "Stored the Template Read Private Key in Key Vault secret '$SecretName'."
    }
}

Update-ManagedSshConfig
Ensure-UserSshInclude

$hostAlias = "dm-$PlatformInstanceKey-$TemplateRepositoryKey"
$publicValue = [IO.File]::ReadAllText($PublicKeyPath).Trim()
$fingerprint = Get-PublicFingerprint

Write-Host ''
Write-Host 'Template access is configured locally.'
Write-Host "Private key: $PrivateKeyPath"
if (-not [string]::IsNullOrWhiteSpace($KeyVaultName)) {
    Write-Host "Key Vault secret: $KeyVaultName/$SecretName"
}
else {
    Write-Host 'Key Vault secret: not configured yet; rerun with -KeyVaultName after it exists.'
}
Write-Host "SSH host alias: $hostAlias"
Write-Host ''
Write-Host 'Send this non-secret authorization request to Data Methods:'
Write-Host "Platform Instance Key: $PlatformInstanceKey"
Write-Host "Template repository key: $TemplateRepositoryKey"
Write-Host "Template Read Public Key: $publicValue"
Write-Host "Fingerprint: $fingerprint"
Write-Host 'Data Methods validates the request and returns the repository-specific clone URL.'
