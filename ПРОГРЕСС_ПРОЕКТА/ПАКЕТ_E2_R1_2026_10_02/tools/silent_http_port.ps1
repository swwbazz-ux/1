param(
    [int]$Port = 8765,
    [string]$BindAddress = '127.0.0.1',
    [string]$LogPath = ''
)

$ErrorActionPreference = 'Stop'
$listener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Parse($BindAddress),
    $Port
)
$clients = [System.Collections.Generic.List[System.Net.Sockets.TcpClient]]::new()

function Write-EvidenceLine([string]$Message) {
    $line = '{0:o} {1}' -f [DateTimeOffset]::UtcNow, $Message
    if ($LogPath) {
        Add-Content -LiteralPath $LogPath -Value $line -Encoding utf8
    }
    Write-Output $line
}

try {
    $listener.Start()
    Write-EvidenceLine "LISTENING address=$BindAddress port=$Port pid=$PID"
    while ($true) {
        if ($listener.Pending()) {
            $client = $listener.AcceptTcpClient()
            $clients.Add($client)
            Write-EvidenceLine "ACCEPTED remote=$($client.Client.RemoteEndPoint) open_clients=$($clients.Count)"
        }
        Start-Sleep -Milliseconds 25
    }
}
finally {
    foreach ($client in $clients) {
        $client.Dispose()
    }
    $listener.Stop()
    Write-EvidenceLine 'STOPPED'
}
