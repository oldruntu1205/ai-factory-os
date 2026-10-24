<#
P2 执行证明：在**一次性 PostgreSQL 集群**上真实执行迁移并实测 §13 隔离。

设计约束（都是踩过的坑）：
- `pg_ctl start` 会派生后台 postgres，任何"等待子进程输出流结束"的调用方式
  （`| Select-Object`、`1> file`、`Start-Process -NoNewWindow -Wait`）都会**永久挂起**
  （本机实测三次，每次 600s 未返回）。因此用 `Start-Job`（独立进程）启动 + **轮询端口**判就绪。
- 收尾按 `postmaster.pid` 里的 PID 显式关停，不依赖 pg_ctl 的等待语义。
- 独立端口 + 独立数据目录，**不碰**用户现有 5432 实例（不读其配置、不改其 pg_hba、不重启它）。
#>

param(
    [string]$DataDir = "C:\pgv3_probe",
    [int]$Port = 5455
)

$ErrorActionPreference = "Continue"
$bin = "C:\pg\pgsql\bin"
$psql = Join-Path $bin "psql.exe"
$initdb = Join-Path $bin "initdb.exe"
$env:PGCLIENTENCODING = "UTF8"
$failures = 0
$checks = 0

function Section([string]$t) { Write-Output ""; Write-Output "=== $t ===" }

function Assert-Eq($actual, $expected, [string]$label) {
    $script:checks++
    $a = "$actual".Trim()
    if ($a -eq "$expected") {
        Write-Output "  [通过] $label = $a"
    } else {
        Write-Output "  [失败] $label 期望 $expected，实际 $a"
        $script:failures++
    }
}

function Sql([string]$sql, [string]$db = "probe") {
    $out = & $psql -h 127.0.0.1 -p $Port -U postgres -d $db -w -tA -c $sql 2>&1
    return ($out -join "`n").Trim()
}

# ---------- 清理既有 ----------
if (Test-Path "$DataDir\postmaster.pid") {
    $oldPid = (Get-Content "$DataDir\postmaster.pid" -First 1).Trim()
    Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}
Remove-Item $DataDir -Recurse -Force -ErrorAction SilentlyContinue

Section "初始化一次性集群（端口 $Port，trust 认证，仅回环）"
& $initdb -D $DataDir -U postgres --no-locale -E UTF8 --auth=trust *> "$env:TEMP\p2_initdb.log"
if ($LASTEXITCODE -ne 0) { Write-Output "  initdb 失败"; Get-Content "$env:TEMP\p2_initdb.log" -Tail 8; exit 1 }
Add-Content "$DataDir\postgresql.conf" "port = $Port"
Add-Content "$DataDir\postgresql.conf" "listen_addresses = '127.0.0.1'"
Write-Output "  initdb 完成"

Section "启动集群（Start-Job，避免 pg_ctl 输出流挂起）"
$job = Start-Job -ScriptBlock {
    param($bin, $dir)
    & (Join-Path $bin "postgres.exe") -D $dir
} -ArgumentList $bin, $DataDir

$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    if (Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue) {
        $ready = $true
        break
    }
}
if (-not $ready) {
    Write-Output "  集群 30s 内未就绪"
    Get-Content "$DataDir\postmaster.pid" -ErrorAction SilentlyContinue | Select-Object -First 1
    Stop-Job $job -ErrorAction SilentlyContinue; Remove-Job $job -Force -ErrorAction SilentlyContinue
    exit 1
}
Write-Output "  集群已就绪（轮询 $i 秒）"

try {
    Section "建库并执行全部 owner 迁移"
    Sql "CREATE DATABASE probe;" "postgres" | Out-Null

    $owners = @("security","control","decision","runtime","manufacturing","tools","transaction","evidence")
    foreach ($owner in $owners) {
        $file = "H:\制造业 AI 员工平台全量开发\migrations\$owner\0001_schema.sql"
        if (-not (Test-Path $file)) { Write-Output "  [$owner] 缺文件"; $failures++; continue }
        $env:PGCLIENTENCODING = "UTF8"
        & $psql -h 127.0.0.1 -p $Port -U postgres -d probe -w -q -v ON_ERROR_STOP=1 -f $file *> "$env:TEMP\p2_mig_$owner.log"
        $code = $LASTEXITCODE
        $script:checks++
        if ($code -eq 0) {
            Write-Output "  [$owner] 迁移执行成功"
        } else {
            Write-Output "  [$owner] **迁移执行失败** exit=$code"
            Get-Content "$env:TEMP\p2_mig_$owner.log" | Select-Object -First 5 | ForEach-Object { "      $_" }
            $failures++
        }
    }

    Section "校验：schema / 角色 / RLS 是否真的落地"
    Assert-Eq (Sql "select count(*) from pg_namespace where nspname in ('security-gateway','control-plane','decision-service','hermes-runtime','manufacturing-core','tool-gateway','transaction-service','trajectory-eval');") 8 "schema 数"
    Assert-Eq (Sql "select count(*) from pg_roles where rolname like '%\_app' or rolname like '%\_owner';") 16 "owner/app 角色数"
    $forceRls = Sql "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace where n.nspname in ('control-plane','transaction-service','manufacturing-core') and c.relkind='r' and c.relrowsecurity and c.relforcerowsecurity;"
    Write-Output "  [信息] FORCE RLS 表数（control+transaction+manufacturing）= $forceRls"
    $policies = Sql "select count(*) from pg_policies;"
    Write-Output "  [信息] RLS 策略总数 = $policies"

    Section "实测 1：未设置 app.tenant_id 时必须查不到数据（fail-closed）"
    Sql "insert into ""control-plane"".tasks (tenant_id, task_id, payload_json) values ('tenant-demo','task-1','{}'::jsonb);" | Out-Null
    Sql "insert into ""control-plane"".tasks (tenant_id, task_id, payload_json) values ('tenant-c3','task-2','{}'::jsonb);" | Out-Null
    Assert-Eq (Sql "select count(*) from ""control-plane"".tasks;") 2 "属主（superuser）视角"
    Assert-Eq (Sql "set role control_app; select count(*) from ""control-plane"".tasks;") 0 "app 角色未设 tenant"

    Section "实测 2：设置 app.tenant_id 后只能看到本租户"
    Assert-Eq (Sql "set role control_app; set app.tenant_id='tenant-demo'; select count(*) from ""control-plane"".tasks;") 1 "app 角色 tenant-demo 视角"
    Assert-Eq (Sql "set role control_app; set app.tenant_id='tenant-c3'; select count(*) from ""control-plane"".tasks;") 1 "app 角色 tenant-c3 视角"

    Section "实测 3：FORCE RLS 下**表属主**也必须受策略约束"
    Assert-Eq (Sql "set role control_owner; set app.tenant_id='tenant-demo'; select count(*) from ""control-plane"".tasks;") 1 "owner 视角（FORCE 生效）"
    Assert-Eq (Sql "set role control_owner; select count(*) from ""control-plane"".tasks;") 0 "owner 未设 tenant"

    Section "实测 4：跨 owner 访问必须被拒"
    $script:checks++
    $cross = & $psql -h 127.0.0.1 -p $Port -U postgres -d probe -w -tA -c "set role control_app; set app.tenant_id='tenant-demo'; select count(*) from ""transaction-service"".commands;" 2>&1
    $crossCode = $LASTEXITCODE
    if ($crossCode -ne 0) {
        Write-Output "  [通过] 跨 owner 读被拒 -> $((($cross | Select-String -Pattern 'permission denied|拒绝') | Select-Object -First 1) -join '')"
    } else {
        Write-Output "  [失败] 跨 owner 读**未被拒** -> $($cross -join ' ')"
        $failures++
    }

    Section "实测 5：app 角色不得具备超级权限"
    Assert-Eq (Sql "select count(*) from pg_roles where rolname like '%\_app' and (rolsuper or rolcreatedb or rolcreaterole or rolbypassrls);") 0 "带超级权限的 app 角色数"

    Section "实测 6：跨 owner 外键必须为 0（生成器的承诺要在真库成立）"
    Assert-Eq (Sql "select count(*) from pg_constraint c join pg_class t on t.oid=c.conrelid join pg_namespace n on n.oid=t.relnamespace join pg_class f on f.oid=c.confrelid join pg_namespace fn on fn.oid=f.relnamespace where c.contype='f' and n.nspname <> fn.nspname;") 0 "跨 schema 外键数"

} finally {
    Section "收尾：关停并删除一次性集群"
    if (Test-Path "$DataDir\postmaster.pid") {
        $pid = (Get-Content "$DataDir\postmaster.pid" -First 1).Trim()
        & $psql -h 127.0.0.1 -p $Port -U postgres -d postgres -w -tA -c "select pg_terminate_backend(pid) from pg_stat_activity where pid <> pg_backend_pid();" *> $null
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
        Write-Output "  已按 PID $pid 关停"
    }
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Remove-Item $DataDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $DataDir) { Write-Output "  警告：数据目录未删除干净" } else { Write-Output "  已删除 $DataDir" }
}

Section "结论"
Write-Output "  检查项 $checks / 失败 $failures"
if ($failures -eq 0) {
    Write-Output "  **全部通过：§13 隔离在真实 PostgreSQL 上成立（执行层证据）**"
    exit 0
}
Write-Output "  **$failures 项失败**"
exit 1
