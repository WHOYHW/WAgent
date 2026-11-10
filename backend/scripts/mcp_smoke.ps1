# MCP L4 真机冒烟（文档 19 §七）：真网络，手动执行，不进 CI。
#
# 用法（backend 目录下，需 .env 指向可达数据库）：
#   powershell -ExecutionPolicy Bypass -File scripts/mcp_smoke.ps1
#
# 覆盖：uvicorn 起 mcp_enabled=true 的主 app → curl initialize 验 401（无令牌）
# 与 200（有效 Bearer 令牌）→ 收尾杀进程。

$ErrorActionPreference = "Stop"
$py = "D:\Anaconda_envs\envs\WAgent\python.exe"
$base = "http://127.0.0.1:8765"

# 1) 签发一枚测试令牌（dev-only secret，与 .env 的 JWT_SECRET 同源）
$token = & $py -c "import sys; sys.path.insert(0, r'$PWD'); from app.security import encode_token; print(encode_token('20230001'))"

# 2) 起 uvicorn（MCP 显式开启；数据库沿用 .env）
$env:MCP_ENABLED = "true"
$proc = Start-Process -FilePath $py -ArgumentList "-m", "uvicorn", "app.main:app", "--port", "8765" `
    -WorkingDirectory $PWD -PassThru -WindowStyle Hidden
try {
    # 等 lifespan 就绪（轮询 /docs，最多 30s）
    $ready = $false
    foreach ($i in 1..30) {
        Start-Sleep -Milliseconds 1000
        try {
            $null = Invoke-WebRequest "$base/docs" -UseBasicParsing -TimeoutSec 2
            $ready = $true; break
        } catch {}
    }
    if (-not $ready) { throw "uvicorn 30s 内未就绪（检查 .env 数据库可达性）" }
    "env-check: MCP_ENABLED=$env:MCP_ENABLED"
    $openapi = Invoke-RestMethod "$base/openapi.json"
    "paths: " + ($openapi.paths.PSObject.Properties.Name -join ", ")

    # 3) 401：无令牌（PS5 无 -SkipHttpErrorCheck，借异常响应取状态码）
    $code401 = 0
    try {
        $null = Invoke-WebRequest "$base/mcp" -Method Post -ContentType "application/json" `
            -Headers @{ Accept = "application/json, text/event-stream" } `
            -Body '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' `
            -UseBasicParsing
    } catch {
        $code401 = [int]$_.Exception.Response.StatusCode
    }
    "401-check: $code401 (expect 401)"

    # 4) 200：有效 Bearer 完成握手
    $r = Invoke-WebRequest "$base/mcp" -Method Post -ContentType "application/json" `
        -Headers @{ Accept = "application/json, text/event-stream"; Authorization = "Bearer $token" } `
        -Body '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}' `
        -UseBasicParsing
    "200-check: $($r.StatusCode) (expect 200)"
    "session-id: $($r.Headers['mcp-session-id'])"

    if ($code401 -eq 401 -and $r.StatusCode -eq 200) { "SMOKE PASS" } else { "SMOKE FAIL"; exit 1 }
} finally {
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
}
