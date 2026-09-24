$ErrorActionPreference = 'Stop'
$scriptPath = (Resolve-Path (Join-Path $PSScriptRoot '../tools/template-access/template-access.ps1')).Path
$testRoot = Join-Path ([IO.Path]::GetTempPath()) "template-access-tests-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $testRoot | Out-Null

function Assert {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

function Invoke-Access {
    param(
        [string]$Case,
        [string]$Repository = 'data-platform-infrastructure',
        [switch]$Vault,
        [switch]$Migrate
    )
    $root = Join-Path $testRoot $Case
    $arguments = @{
        PlatformInstanceKey = 'test-instance'
        TemplateRepositoryKey = $Repository
        SshRoot = Join-Path $root '.ssh/data-platform'
        UserSshConfigPath = Join-Path $root '.ssh/config'
    }
    if ($Vault) { $arguments.KeyVaultName = 'test-vault' }
    if ($Migrate) { $arguments.MigrateLegacyInfrastructureKey = $true }
    return (& $scriptPath @arguments 6>&1 | Out-String)
}

$global:TestVault = @{}
function global:az {
    $arguments = @($args)
    $nameIndex = [array]::IndexOf($arguments, '--name')
    $name = if ($nameIndex -ge 0) { $arguments[$nameIndex + 1] } else { '' }
    if ($arguments[0] -ne 'keyvault' -or $arguments[1] -ne 'secret') {
        throw 'Unexpected Azure CLI command in test.'
    }
    switch ($arguments[2]) {
        'show' {
            if ($global:TestVault.ContainsKey($name)) {
                $global:LASTEXITCODE = 0
                "https://test-vault.vault.azure.net/secrets/$name"
            }
            else {
                $global:LASTEXITCODE = 1
                'SecretNotFound'
            }
        }
        'download' {
            if (-not $global:TestVault.ContainsKey($name)) { throw 'Secret absent.' }
            $path = $arguments[[array]::IndexOf($arguments, '--file') + 1]
            [IO.File]::WriteAllText($path, $global:TestVault[$name])
            $global:LASTEXITCODE = 0
        }
        'set' {
            $path = $arguments[[array]::IndexOf($arguments, '--file') + 1]
            $global:TestVault[$name] = [IO.File]::ReadAllText($path)
            $global:LASTEXITCODE = 0
        }
        default { throw 'Unexpected Azure CLI operation in test.' }
    }
}

try {
    $basicRoot = Join-Path $testRoot 'basic'
    $configPath = Join-Path $basicRoot '.ssh/config'
    New-Item -ItemType Directory -Path (Split-Path $configPath -Parent) -Force | Out-Null
    [IO.File]::WriteAllText($configPath, "Host existing`n    HostName example.invalid`n")
    $first = Invoke-Access -Case 'basic'
    $key = Join-Path $basicRoot '.ssh/data-platform/test-instance/data-platform-infrastructure/template-read'
    Assert (Test-Path "$key.pub") 'The public key was not created.'
    Assert ($first.Contains('Created a new Template Read Keypair.')) 'New-key result was not reported.'
    Assert (-not $first.Contains('BEGIN OPENSSH PRIVATE KEY')) 'Private key appeared in output.'
    $sshConfig = [IO.File]::ReadAllText($configPath)
    Assert ($sshConfig.StartsWith('Include ')) 'SSH Include must precede existing Host blocks.'
    Assert ($sshConfig.Contains('Host existing')) 'Existing SSH settings were removed.'
    $publicKey = [IO.File]::ReadAllText("$key.pub")
    $second = Invoke-Access -Case 'basic'
    Assert (([IO.File]::ReadAllText("$key.pub")) -eq $publicKey) 'Rerun rotated the key.'
    Assert (([regex]::Matches(([IO.File]::ReadAllText($configPath)), '^Include ', 'Multiline')).Count -eq 1) 'Rerun duplicated the Include.'
    Assert (-not $second.Contains('Created a new')) 'Rerun reported a new key.'
    if (-not $IsWindows) {
        $privateMode = [IO.File]::GetUnixFileMode($key)
        $expectedPrivateMode = [IO.UnixFileMode]::UserRead -bor [IO.UnixFileMode]::UserWrite
        Assert ($privateMode -eq $expectedPrivateMode) 'Private key mode is not 0600.'
    }

    $other = Invoke-Access -Case 'other-repository' -Repository 'data-platform-new-component'
    Assert ($other.Contains('dm-test-instance-data-platform-new-component')) 'A valid repository key was rejected.'
    try {
        Invoke-Access -Case 'invalid-repository' -Repository '../another-directory' | Out-Null
        throw 'An unsafe repository key was accepted.'
    }
    catch {
        Assert ($_.Exception.Message -match 'Cannot validate argument') 'Invalid repository key did not fail validation.'
    }

    $global:TestVault = @{}
    $vaultOutput = Invoke-Access -Case 'vault' -Vault
    $vaultKey = Join-Path $testRoot 'vault/.ssh/data-platform/test-instance/data-platform-infrastructure/template-read'
    $secretName = 'dm-template-read-data-platform-infrastructure'
    Assert ($global:TestVault.ContainsKey($secretName)) 'Key Vault secret was not written.'
    Assert ($global:TestVault[$secretName] -eq [IO.File]::ReadAllText($vaultKey)) 'Key Vault secret did not match the local key.'
    Assert (-not $vaultOutput.Contains('BEGIN OPENSSH PRIVATE KEY')) 'Private key appeared in vault output.'
    Remove-Item -LiteralPath $vaultKey, "$vaultKey.pub"
    $restored = Invoke-Access -Case 'vault' -Vault
    Assert ($restored.Contains('Restored the Template Read Private Key')) 'Vault restore was not reported.'
    Assert ($global:TestVault[$secretName] -eq [IO.File]::ReadAllText($vaultKey)) 'Vault restore changed the key.'

    $differentKey = Join-Path $testRoot 'different-template-read'
    & ssh-keygen -q -t ed25519 -N '' -f $differentKey | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Test key generation failed.' }
    $global:TestVault[$secretName] = [IO.File]::ReadAllText($differentKey)
    try {
        Invoke-Access -Case 'vault' -Vault | Out-Null
        throw 'A mismatched Key Vault secret was accepted.'
    }
    catch {
        Assert ($_.Exception.Message -match 'do not match') 'Vault mismatch did not fail clearly.'
    }
    Assert ([IO.File]::ReadAllText($vaultKey) -ne $global:TestVault[$secretName]) 'Vault mismatch changed the local key.'

    $conflictRoot = Join-Path $testRoot 'conflict'
    $conflictConfig = Join-Path $conflictRoot '.ssh/config'
    New-Item -ItemType Directory -Path (Split-Path $conflictConfig -Parent) -Force | Out-Null
    [IO.File]::WriteAllText($conflictConfig, "Host *`n    IdentityFile ~/.ssh/another-key`n")
    try {
        Invoke-Access -Case 'conflict' | Out-Null
        throw 'A conflicting SSH identity was accepted.'
    }
    catch {
        Assert ($_.Exception.Message -match 'dedicated key') 'The SSH conflict did not fail clearly.'
    }
    Assert (([IO.File]::ReadAllText($conflictConfig)).StartsWith('Host *')) 'Failed SSH validation changed the user config.'

    $legacyRoot = Join-Path $testRoot 'legacy'
    $legacyKey = Join-Path $legacyRoot '.ssh/data-platform/test-instance/template-read'
    New-Item -ItemType Directory -Path (Split-Path $legacyKey -Parent) -Force | Out-Null
    & ssh-keygen -q -t ed25519 -N '' -f $legacyKey | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Test key generation failed.' }
    $global:TestVault = @{
        'dm-template-read-key' = ('{"private_key":' + (ConvertTo-Json -Compress ([IO.File]::ReadAllText($legacyKey))) + '}')
    }
    try {
        Invoke-Access -Case 'legacy' -Vault | Out-Null
        throw 'Legacy key was adopted without explicit migration.'
    }
    catch {
        Assert ($_.Exception.Message -match 'MigrateLegacyInfrastructureKey') 'Legacy detection did not explain migration.'
    }
    $migration = Invoke-Access -Case 'legacy' -Vault -Migrate
    $migratedKey = Join-Path $legacyRoot '.ssh/data-platform/test-instance/data-platform-infrastructure/template-read'
    Assert ($migration.Contains('Adopted the existing Infrastructure key')) 'Legacy adoption was not reported.'
    Assert (([IO.File]::ReadAllText($migratedKey)) -eq ([IO.File]::ReadAllText($legacyKey))) 'Migration rotated the key.'
    Assert ($global:TestVault[$secretName] -eq [IO.File]::ReadAllText($legacyKey)) 'Migration did not write the current secret.'
    Assert ($global:TestVault.ContainsKey('dm-template-read-key')) 'Migration deleted the legacy secret.'

    $global:TestVault = @{
        'dm-template-read-key' = ('{"private_key":' + (ConvertTo-Json -Compress ([IO.File]::ReadAllText($differentKey))) + '}')
    }
    try {
        Invoke-Access -Case 'legacy' -Vault -Migrate | Out-Null
        throw 'A mismatched legacy Key Vault secret was accepted.'
    }
    catch {
        Assert ($_.Exception.Message -match 'do not match') 'Legacy mismatch did not fail clearly.'
    }
    Assert ([IO.File]::ReadAllText($migratedKey) -eq [IO.File]::ReadAllText($legacyKey)) 'Legacy mismatch changed the local key.'

    Write-Host 'Template access tests passed.'
}
finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item Function:\az -ErrorAction SilentlyContinue
}
