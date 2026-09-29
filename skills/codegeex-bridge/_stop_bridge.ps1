$c = Get-NetTCPConnection -LocalPort 18788 -State Listen -ErrorAction SilentlyContinue
if ($c) {
    foreach ($conn in $c) {
        try { Stop-Process -Id $conn.OwningProcess -Force -ErrorAction Stop; Write-Host ("stopped pid " + $conn.OwningProcess) } catch {}
    }
} else {
    Write-Host "none listening on 18788"
}
