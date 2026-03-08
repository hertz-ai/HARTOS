# Compact WSL2 VHDX to reclaim freed disk space
$ErrorActionPreference = "Stop"

# Find the VHDX
$searchPaths = @(
    "$env:LOCALAPPDATA\Packages\CanonicalGroupLimited.Ubuntu22.04onWindows_*\LocalState\ext4.vhdx",
    "$env:LOCALAPPDATA\Packages\CanonicalGroupLimited.Ubuntu*\LocalState\ext4.vhdx",
    "$env:LOCALAPPDATA\Packages\*Ubuntu*\LocalState\ext4.vhdx"
)

$vhdx = $null
foreach ($p in $searchPaths) {
    $found = Get-ChildItem -Path $p -ErrorAction SilentlyContinue
    if ($found) { $vhdx = $found[0]; break }
}

if (-not $vhdx) {
    Write-Host "ERROR: Could not find WSL VHDX file" -ForegroundColor Red
    exit 1
}

$sizeBefore = [math]::Round($vhdx.Length / 1GB, 2)
Write-Host "VHDX: $($vhdx.FullName)"
Write-Host "Size before: $sizeBefore GB"

# Make sure WSL is shut down
Write-Host "Ensuring WSL is shut down..."
wsl --shutdown 2>$null
Start-Sleep -Seconds 3

# Try Optimize-VHD first (requires Hyper-V module)
try {
    Write-Host "Compacting with Optimize-VHD..."
    Optimize-VHD -Path $vhdx.FullName -Mode Full
    Write-Host "Optimize-VHD succeeded" -ForegroundColor Green
} catch {
    Write-Host "Optimize-VHD not available, using diskpart..."
    $dpScript = @"
select vdisk file="$($vhdx.FullName)"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@
    $dpFile = "$env:TEMP\compact-wsl.txt"
    $dpScript | Out-File -FilePath $dpFile -Encoding ASCII
    diskpart /s $dpFile
    Remove-Item $dpFile -ErrorAction SilentlyContinue
}

# Show result
$vhdxAfter = Get-Item $vhdx.FullName
$sizeAfter = [math]::Round($vhdxAfter.Length / 1GB, 2)
$saved = [math]::Round($sizeBefore - $sizeAfter, 2)
Write-Host ""
Write-Host "Size after:  $sizeAfter GB"
Write-Host "Saved:       $saved GB" -ForegroundColor Green
