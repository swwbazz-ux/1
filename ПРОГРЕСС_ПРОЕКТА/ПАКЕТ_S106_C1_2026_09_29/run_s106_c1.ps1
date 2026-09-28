param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [Parameter(Mandatory = $true)]
    [string]$PythonExe
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding

$ReleaseSha = '8c215ae2a45196e3e74e566c54f2ab5cb321be02'
$Pr106Sha = '17cbb3e913dfdc98aaa9585ba4090d0f4cf17ee0'
$MergeBaseSha = 'd208b84db920aa98ec80612645dac3996c61f76c'
$PackageDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ResultsDir = Join-Path $PackageDir 'results'
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$GitExe = (Get-Command git).Source
$NodeExe = (Get-Command node).Source

New-Item -ItemType Directory -Force -Path $ResultsDir | Out-Null
Get-ChildItem -LiteralPath $ResultsDir -File -ErrorAction SilentlyContinue | Remove-Item -Force

$RunRecords = [System.Collections.Generic.List[object]]::new()
function ConvertTo-StableUtf8Log {
    param([AllowEmptyString()][string]$Value)
    $normalized = [regex]::Replace($Value, "\r+\n?", "`n")
    return [regex]::Replace($normalized, '[ \t]+(?=\n|$)', '')
}

function Get-BackendRoot {
    param([string]$WorktreeRoot)
    $matches = @(
        Get-ChildItem -LiteralPath $WorktreeRoot -Directory |
            ForEach-Object { Join-Path $_.FullName 'backend' } |
            Where-Object { Test-Path -LiteralPath (Join-Path $_ 'manage.py') }
    )
    if ($matches.Count -ne 1) {
        throw "Expected exactly one Django backend below $WorktreeRoot, found $($matches.Count)"
    }
    return $matches[0]
}

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $slashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($slashes * 2) + 1)))
            [void]$builder.Append('"')
        }
        else {
            [void]$builder.Append(('\' * $slashes))
            [void]$builder.Append($character)
        }
        $slashes = 0
    }
    [void]$builder.Append(('\' * ($slashes * 2)))
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Invoke-Logged {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [int[]]$ExpectedExitCodes = @(0)
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    $psi.WorkingDirectory = $WorkingDirectory
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.EnvironmentVariables['PYTHONUTF8'] = '1'
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $psi.Arguments = (($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $psi
    if (-not $process.Start()) { throw "Failed to start $Name" }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $stdout = ConvertTo-StableUtf8Log ($stdoutTask.GetAwaiter().GetResult())
    $stderr = ConvertTo-StableUtf8Log ($stderrTask.GetAwaiter().GetResult())
    [IO.File]::WriteAllText((Join-Path $ResultsDir "$Name.stdout.log"), $stdout, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $ResultsDir "$Name.stderr.log"), $stderr, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $ResultsDir "$Name.exit-code.txt"), "$($process.ExitCode)`n", [Text.UTF8Encoding]::new($false))
    $RunRecords.Add([pscustomobject]@{
        name = $Name
        command = @($FilePath) + $Arguments
        cwd = $WorkingDirectory
        exit_code = $process.ExitCode
        expected_exit_codes = $ExpectedExitCodes
    })
    if ($ExpectedExitCodes -notcontains $process.ExitCode) {
        throw "$Name exited $($process.ExitCode), expected $($ExpectedExitCodes -join ',')"
    }
    return $process.ExitCode
}

$TempBase = Join-Path ([IO.Path]::GetTempPath()) ("s106-c1-" + [guid]::NewGuid().ToString('N'))
$ReleaseRoot = Join-Path $TempBase 'release'
$PrRoot = Join-Path $TempBase 'pr106'
$PrOverlayRoot = Join-Path $TempBase 'pr106-overlay'
$MergeRoot = Join-Path $TempBase 'merge-probe'
New-Item -ItemType Directory -Force -Path $TempBase | Out-Null

try {
    Invoke-Logged 'git-release-object' $GitExe @('-C', $RepoRoot, 'cat-file', '-e', "$ReleaseSha^{commit}") $RepoRoot
    Invoke-Logged 'git-pr106-object' $GitExe @('-C', $RepoRoot, 'cat-file', '-e', "$Pr106Sha^{commit}") $RepoRoot
    Invoke-Logged 'git-add-release-worktree' $GitExe @('-C', $RepoRoot, 'worktree', 'add', '--detach', $ReleaseRoot, $ReleaseSha) $RepoRoot
    Invoke-Logged 'git-add-pr106-worktree' $GitExe @('-C', $RepoRoot, 'worktree', 'add', '--detach', $PrRoot, $Pr106Sha) $RepoRoot
    Invoke-Logged 'git-add-pr106-overlay-worktree' $GitExe @('-C', $RepoRoot, 'worktree', 'add', '--detach', $PrOverlayRoot, $Pr106Sha) $RepoRoot
    Invoke-Logged 'git-add-merge-worktree' $GitExe @('-C', $RepoRoot, 'worktree', 'add', '--detach', $MergeRoot, $ReleaseSha) $RepoRoot

    $releaseBackend = Get-BackendRoot $ReleaseRoot
    $prBackend = Get-BackendRoot $PrRoot
    $prOverlayBackend = Get-BackendRoot $PrOverlayRoot
    foreach ($backend in @($releaseBackend, $prBackend, $prOverlayBackend)) {
        New-Item -ItemType Directory -Force -Path (Join-Path $backend 'media') | Out-Null
    }
    Copy-Item -LiteralPath (Join-Path $PackageDir 'test_s106_c1_probe.py') -Destination (Join-Path $releaseBackend 'core/test_s106_c1_probe.py')
    Copy-Item -LiteralPath (Join-Path $PackageDir 'test_s106_c1_probe.py') -Destination (Join-Path $prBackend 'core/test_s106_c1_probe.py')

    $actualBase = (& $GitExe -C $RepoRoot merge-base $ReleaseSha $Pr106Sha).Trim()
    if ($actualBase -ne $MergeBaseSha) { throw "Merge base drift: $actualBase" }
    Invoke-Logged 'merge-probe' $GitExe @('-C', $MergeRoot, 'merge', '--no-commit', '--no-ff', $Pr106Sha) $MergeRoot @(1)
    Invoke-Logged 'merge-unmerged-files' $GitExe @('-C', $MergeRoot, 'diff', '--name-only', '--diff-filter=U') $MergeRoot
    Invoke-Logged 'merge-status' $GitExe @('-C', $MergeRoot, 'status', '--short') $MergeRoot

    Invoke-Logged 'contract-cases' $PythonExe @((Join-Path $PackageDir 'verify_contract_cases.py')) $PackageDir
    Invoke-Logged 'client-release' $NodeExe @((Join-Path $PackageDir 'probe_client_recovery.cjs'), $ReleaseRoot, 'release') $PackageDir
    Invoke-Logged 'client-pr106' $NodeExe @((Join-Path $PackageDir 'probe_client_recovery.cjs'), $PrRoot, 'pr106') $PackageDir

    Invoke-Logged 'release-three-coordinator-regressions' $PythonExe @(
        'manage.py', 'test',
        'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_manual_load_under_bucket_cancelled_by_server_ttl_is_recorded_by_fact',
        'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_already_rejected_cancelled_bucket_load_is_accepted_on_resend',
        'core.test_offline_sync.OfflineEventSyncTests.test_dependency_chain_recovers_when_only_the_parent_clock_was_ahead',
        '--verbosity', '1'
    ) $releaseBackend
    Invoke-Logged 'release-negative-domain-parent' $PythonExe @(
        'manage.py', 'test',
        'core.test_offline_sync.OfflineEventSyncTests.test_real_conflict_chain_is_not_reopened_by_clock_recovery',
        '--verbosity', '1'
    ) $releaseBackend
    Invoke-Logged 'pr106-existing-replay-regressions' $PythonExe @(
        'manage.py', 'test',
        'core.test_offline_sync.OfflineEventSyncTests.test_dependency_chain_recovers_when_only_the_parent_clock_was_ahead',
        'core.test_offline_sync.OfflineEventSyncTests.test_legacy_open_trip_changed_chain_replays_worker_truth',
        'core.test_offline_sync.OfflineEventSyncTests.test_legacy_dependency_receipts_reprocess_independently',
        'core.test_offline_sync.OfflineEventSyncTests.test_legacy_unload_before_load_receipt_reprocesses_without_duplicate',
        '--verbosity', '1'
    ) $prBackend
    Invoke-Logged 'release-node-regressions' $NodeExe @(
        '--test', '--test-name-pattern',
        'restart recovers only clock conflict|restart resends a manual load refused|restart retries a legacy device clock conflict',
        'static/js/tests/driver-offline-outbox-v2.test.js',
        'static/js/tests/excavator-field-outbox.test.js'
    ) $releaseBackend
    Invoke-Logged 'pr106-node-regressions' $NodeExe @(
        '--test', '--test-name-pattern',
        'restart recovers only clock conflict|restart retries a legacy device clock conflict|restart independently retries every legacy dependency cascade code|restart retries every legacy dependency cascade code independently',
        'static/js/tests/driver-offline-outbox-v2.test.js',
        'static/js/tests/excavator-field-outbox.test.js'
    ) $prBackend

    $targetTests = @(
        'core.test_s106_c1_probe.S106C1ReplayContractProbe.test_target_child_keeps_its_own_time_when_only_parent_clock_was_bad',
        'core.test_s106_c1_probe.S106C1ReplayContractProbe.test_target_child_replays_after_parent_was_already_accepted',
        'core.test_s106_c1_probe.S106C1ReplayContractProbe.test_target_foreign_parent_neither_vetoes_nor_supplies_child_references',
        'core.test_s106_c1_probe.S106C1ReplayContractProbe.test_same_event_id_from_another_device_cannot_bypass_identity',
        'core.test_s106_c1_probe.S106C1ReplayContractProbe.test_same_code_on_unrelated_event_type_is_not_replayed'
    )
    Invoke-Logged 'release-target-contract-known-gap' $PythonExe (@('manage.py', 'test') + $targetTests + @('--verbosity', '1')) $releaseBackend @(1)
    Invoke-Logged 'pr106-target-contract' $PythonExe (@('manage.py', 'test') + $targetTests + @('--verbosity', '1')) $prBackend

    Invoke-Logged 'prepare-pr106-release-test-overlay' $PythonExe @(
        (Join-Path $PackageDir 'prepare_pr106_test_overlay.py'), $ReleaseRoot, $PrOverlayRoot
    ) $PackageDir
    Invoke-Logged 'pr106-unchanged-release-django-tests-known-incompatible' $PythonExe @(
        'manage.py', 'test',
        'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_manual_load_under_bucket_cancelled_by_server_ttl_is_recorded_by_fact',
        'core.test_free_bucket_sync.FreeBucketServerIntegrationTests.test_already_rejected_cancelled_bucket_load_is_accepted_on_resend',
        '--verbosity', '1'
    ) $prOverlayBackend @(1)
    Invoke-Logged 'pr106-unchanged-release-js-test-known-gap' $NodeExe @(
        '--test', '--test-name-pattern',
        'restart resends a manual load refused as free_bucket_not_available and its chain',
        'static/js/tests/driver-offline-outbox-v2.test.js'
    ) $prOverlayBackend @(1)

    $summary = [ordered]@{
        schema = 's106-c1-run-summary-v1'
        generated_at = [DateTimeOffset]::Now.ToString('o')
        repo_root = $RepoRoot
        release_sha = $ReleaseSha
        pr106_sha = $Pr106Sha
        merge_base_sha = $MergeBaseSha
        python = $PythonExe
        node = (& $NodeExe --version).Trim()
        database = 'isolated Django test database; default engine from checkout (SQLite); PostgreSQL NOT_RUN'
        mock_boundary = 'client probes use real JS runtime with MOCK transport; no HTTP server or phone'
        runs = $RunRecords
    }
    [IO.File]::WriteAllText(
        (Join-Path $ResultsDir 'run-summary.json'),
        ($summary | ConvertTo-Json -Depth 8),
        [Text.UTF8Encoding]::new($false)
    )
}
finally {
    if (Test-Path -LiteralPath (Join-Path $MergeRoot '.git')) {
        $mergeHeadPath = (& $GitExe -C $MergeRoot rev-parse --git-path MERGE_HEAD 2>$null)
        if ($mergeHeadPath -and (Test-Path -LiteralPath $mergeHeadPath)) {
            & $GitExe -C $MergeRoot merge --abort 2>$null | Out-Null
        }
    }
    foreach ($root in @($MergeRoot, $PrOverlayRoot, $PrRoot, $ReleaseRoot)) {
        if (Test-Path -LiteralPath $root) {
            & $GitExe -C $RepoRoot worktree remove --force $root | Out-Null
        }
    }
    & $GitExe -C $RepoRoot worktree prune | Out-Null
    if (Test-Path -LiteralPath $TempBase) {
        $resolvedTemp = [IO.Path]::GetFullPath($TempBase)
        $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
        if (-not $resolvedTemp.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing cleanup outside temp: $resolvedTemp"
        }
        Remove-Item -LiteralPath $resolvedTemp -Recurse -Force
    }
}

Write-Host "S106-C1 package completed. Results: $ResultsDir"
