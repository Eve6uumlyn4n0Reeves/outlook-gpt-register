import asyncio
import json
import logging
from typing import Optional, List, Dict, Any, Union
from pathlib import Path
import time

from fastapi import FastAPI, HTTPException, Query, Depends, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from fastapi.middleware.cors import CORSMiddleware

# 导入配置模块
from config import verify_admin_password, get_config_info, get_cors_allow_origins, is_passwords_endpoint_enabled

import mail_service as ms




# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================================
# 数据模型 (Pydantic)
# ============================================================================

class AccountCredentials(BaseModel):
    email: EmailStr
    refresh_token: str
    client_id: str
    password: Optional[str] = None  # 可选：用于将邮箱密码与账户一并保存，便于 GPT 注册复用

class AccountStatus(BaseModel):
    email: EmailStr
    status: str = "unknown"  # "active", "inactive", "unknown"

class AccountDeleteRequest(BaseModel):
    emails: List[EmailStr]

class AccountVerificationRequest(BaseModel):
    accounts: List[AccountCredentials]

class AccountVerificationResult(BaseModel):
    email: EmailStr
    status: str  # "success" 或 "error"
    message: str = ""
    credentials: Optional[AccountCredentials] = None

class EmailItem(BaseModel):
    message_id: str
    folder: str
    subject: str
    from_email: str
    date: str
    is_read: bool = False
    has_attachments: bool = False
    sender_initial: str = "?"

class EmailListResponse(BaseModel):
    email_id: str
    folder_view: str
    page: int
    page_size: int
    total_emails: int
    emails: List[EmailItem]

class DualViewEmailResponse(BaseModel):
    email_id: str
    inbox_emails: List[EmailItem]
    junk_emails: List[EmailItem]
    inbox_total: int
    junk_total: int

class EmailDetailsResponse(BaseModel):
    message_id: str
    subject: str
    from_email: str
    to_email: str
    date: str
    body_plain: Optional[str] = None
    body_html: Optional[str] = None

class AccountResponse(BaseModel):
    email_id: str
    message: str

# 简化的认证模型已移除，直接使用Bearer密码验证


# ============================================================================
# 极简认证函数
# ============================================================================

security = HTTPBearer()

def verify_admin_bearer(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """验证Bearer密码（极简认证）"""
    if not verify_admin_password(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="管理密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return True

async def get_current_admin(authenticated: bool = Depends(verify_admin_bearer)):
    """获取当前管理员用户（依赖注入）"""
    return authenticated






# ============================================================================
# IMAP核心服务 - 邮件列表
# ============================================================================






# ============================================================================
# FastAPI应用和API端点
# ============================================================================

app = FastAPI(
    title="Outlook邮件API服务",
    description="基于FastAPI的邮件管理服务（邮件逻辑由 mail_service 提供）",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件服务
app.mount("/static", StaticFiles(directory="static"), name="static")

# 挂载运行日志目录（供前端查看运行记录）
Path("run-logs").mkdir(exist_ok=True)
app.mount("/run-logs", StaticFiles(directory="run-logs"), name="run_logs")

@app.get("/auth/config")
async def get_auth_config(current_admin: bool = Depends(get_current_admin)):
    """获取认证配置信息（用于前端判断认证状态）"""
    return get_config_info()

@app.post("/accounts", response_model=Union[AccountResponse, List[AccountResponse]])
async def register_account(
    credentials: Union[AccountCredentials, List[AccountCredentials]],
    current_admin: bool = Depends(get_current_admin)
):
    """注册或更新邮箱账户，支持单个或批量

    Args:
        credentials: 单个账户凭证或账户凭证列表

    Returns:
        单个账户响应或账户响应列表
    """
    # 处理单个凭证的情况
    if isinstance(credentials, AccountCredentials):
        return await register_single_account(credentials)

    # 批量处理优化：并行验证 + 批量保存
    return await register_multiple_accounts_optimized(credentials)

async def register_multiple_accounts_optimized(credentials_list: List[AccountCredentials]) -> List[AccountResponse]:
    """优化的批量账户注册：并行验证 + 批量保存"""
    start_time = time.time()

    # 使用信号量控制并发，避免过多HTTP请求
    semaphore = asyncio.Semaphore(10)  # 最多同时10个请求

    async def verify_single_with_semaphore(cred: AccountCredentials) -> tuple[AccountCredentials, bool, str]:
        async with semaphore:
            try:
                token = await ms.get_access_token(cred, check_only=True)
                if token:
                    return cred, True, "验证成功"
                else:
                    return cred, False, "获取访问令牌失败"
            except Exception as e:
                return cred, False, f"验证错误: {str(e)}"

    # 并行验证所有账户
    logger.info(f"开始并行验证 {len(credentials_list)} 个账户...")
    verify_start_time = time.time()
    verification_tasks = [verify_single_with_semaphore(cred) for cred in credentials_list]
    verification_results = await asyncio.gather(*verification_tasks, return_exceptions=True)
    verify_time = time.time() - verify_start_time
    logger.info(f"并行验证完成，耗时: {verify_time:.2f}秒")

    # 收集验证成功的账户
    valid_credentials = []
    results = []

    for result in verification_results:
        if isinstance(result, Exception):
            # 处理异常
            results.append(AccountResponse(
                email_id="unknown",
                message=f"验证异常: {str(result)}"
            ))
        else:
            cred, is_valid, message = result
            if is_valid:
                valid_credentials.append(cred)
                results.append(AccountResponse(
                    email_id=cred.email,
                    message="账户验证成功，正在保存..."
                ))
            else:
                results.append(AccountResponse(
                    email_id=cred.email,
                    message=f"验证失败: {message}"
                ))

    # 批量保存有效的账户
    if valid_credentials:
        logger.info(f"批量保存 {len(valid_credentials)} 个有效账户...")
        save_start_time = time.time()
        try:
            await ms.save_multiple_accounts_batch(valid_credentials)
            save_time = time.time() - save_start_time
            logger.info(f"批量保存完成，耗时: {save_time:.2f}秒")

            # 更新成功保存的账户状态
            for i, (cred, is_valid, _) in enumerate([r for r in verification_results if not isinstance(r, Exception)]):
                if is_valid:
                    for result in results:
                        if result.email_id == cred.email:
                            result.message = "账户验证成功并已保存"
                            break
        except Exception as e:
            logger.error(f"批量保存失败: {e}")
            # 更新失败状态
            for result in results:
                if "正在保存" in result.message:
                    result.message = f"验证成功但保存失败: {str(e)}"

    total_time = time.time() - start_time
    logger.info(f"批量注册完成，总耗时: {total_time:.2f}秒，处理了 {len(credentials_list)} 个账户，成功 {len(valid_credentials)} 个")

    return results

async def register_single_account(credentials: AccountCredentials) -> AccountResponse:
    """注册或更新单个邮箱账户"""
    try:
        # 验证凭证有效性
        await ms.get_access_token(credentials)

        # 保存凭证
        await ms.save_account_credentials(credentials.email, credentials)

        return AccountResponse(
            email_id=credentials.email,
            message="Account verified and saved successfully."
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error registering account: {e}")
        raise HTTPException(status_code=500, detail="Account registration failed")

@app.get("/accounts", response_model=List[AccountStatus])
async def get_accounts(
    check_status: bool = False,
    current_admin: bool = Depends(get_current_admin)
):
    """获取所有账户列表，可选择检查账户活性状态

    Args:
        check_status: 是否检查账户活性状态
    """
    accounts = await ms.get_all_accounts()

    if not check_status:
        # 仅返回邮箱列表，不检查状态
        return [AccountStatus(email=email) for email in accounts.keys()]

    # 并行检查所有账户的活性
    result = []
    tasks = []

    for email, account_data in accounts.items():
        credentials = AccountCredentials(
            email=email,
            refresh_token=account_data['refresh_token'],
            client_id=account_data['client_id']
        )
        # 创建异步任务
        task = asyncio.create_task(check_account_status(credentials))
        tasks.append((email, task))

    # 等待所有任务完成
    for email, task in tasks:
        is_active = await task
        status = "active" if is_active else "inactive"
        result.append(AccountStatus(email=email, status=status))

    return result

@app.get("/accounts/passwords")
async def get_account_passwords(current_admin: bool = Depends(get_current_admin)):
    """返回 accounts.json 中的邮箱->密码映射（如未设置则为空字符串）。
    仅用于 GPT 批量注册在前端自动填充密码。
    """
    if not is_passwords_endpoint_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    accounts = await ms.get_all_accounts()
    try:
        return {email: (data.get("password") or "") for email, data in (accounts or {}).items()}
    except Exception:
        # 防御性返回空映射
        return {}


async def check_account_status(credentials: AccountCredentials) -> bool:
    """检查账户活性状态"""
    token = await ms.get_access_token(credentials, check_only=True)
    return token is not None

@app.get("/emails/{email_id}", response_model=EmailListResponse)
async def get_emails(
    email_id: str,
    folder: str = Query("all", pattern="^(inbox|junk|all)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    force_refresh: bool = Query(False),
    current_admin: bool = Depends(get_current_admin)
):
    """获取邮件列表"""
    credentials = await ms.get_account_credentials(email_id)
    return await ms.list_emails(credentials, folder, page, page_size, force_refresh)


@app.get("/emails/{email_id}/dual-view")
async def get_dual_view_emails(
    email_id: str,
    inbox_page: int = Query(1, ge=1),
    junk_page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    force_refresh: bool = Query(False),
    current_admin: bool = Depends(get_current_admin)
):
    """获取双栏视图邮件（收件箱和垃圾箱）"""
    credentials = await ms.get_account_credentials(email_id)

    # 并行获取收件箱和垃圾箱邮件（mail_service 返回 dict，这里做字段访问兼容）
    inbox_response = await ms.list_emails(credentials, "inbox", inbox_page, page_size, force_refresh)
    junk_response = await ms.list_emails(credentials, "junk", junk_page, page_size, force_refresh)

    # 兼容对象或字典两种形式
    def _get(obj, key, default=None):
        try:
            return getattr(obj, key)
        except Exception:
            return (obj or {}).get(key, default)

    return DualViewEmailResponse(
        email_id=email_id,
        inbox_emails=_get(inbox_response, "emails", []),
        junk_emails=_get(junk_response, "emails", []),
        inbox_total=_get(inbox_response, "total_emails", 0),
        junk_total=_get(junk_response, "total_emails", 0)
    )


@app.get("/emails/{email_id}/{message_id}", response_model=EmailDetailsResponse)
async def get_email_detail(email_id: str, message_id: str, current_admin: bool = Depends(get_current_admin)):
    """获取邮件详细内容"""
    credentials = await ms.get_account_credentials(email_id)
    return await ms.get_email_details(credentials, message_id)


@app.get("/")
async def root():
    """根路径 - 返回前端页面"""
    return FileResponse("static/index.html")

@app.get("/api")
async def api_status():
    """API状态检查"""
    return {
        "message": "Outlook邮件API服务正在运行",
        "version": "1.0.0",
        "endpoints": {
            "register_account": "POST /accounts",
            "get_emails": "GET /emails/{email_id}",
            "get_email_detail": "GET /emails/{email_id}/{message_id}"
        }
    }

@app.post("/accounts/verify", response_model=List[AccountVerificationResult])
async def verify_accounts(
    request: AccountVerificationRequest,
    current_admin: bool = Depends(get_current_admin)
):
    """批量验证账户凭证有效性（优化版本）

    Args:
        request: 包含多个账户凭证的请求

    Returns:
        每个账户的验证结果列表
    """
    start_time = time.time()
    logger.info(f"开始批量验证 {len(request.accounts)} 个账户...")

    # 使用信号量控制并发
    semaphore = asyncio.Semaphore(10)

    async def verify_with_semaphore(credentials: AccountCredentials) -> AccountVerificationResult:
        async with semaphore:
            return await verify_single_account(credentials)

    # 并行验证所有账户
    tasks = [verify_with_semaphore(credentials) for credentials in request.accounts]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 处理异常情况
    final_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            final_results.append(AccountVerificationResult(
                email=request.accounts[i].email,
                status="error",
                message=f"验证异常: {str(result)}"
            ))
        else:
            final_results.append(result)

    total_time = time.time() - start_time
    success_count = sum(1 for r in final_results if r.status == "success")
    logger.info(f"批量验证完成，总耗时: {total_time:.2f}秒，处理了 {len(request.accounts)} 个账户，成功 {success_count} 个")

    return final_results

async def verify_single_account(credentials: AccountCredentials) -> AccountVerificationResult:
    """验证单个账户凭证的有效性"""
    try:
        token = await ms.get_access_token(credentials, check_only=True)
        if token:
            return AccountVerificationResult(
                email=credentials.email,
                status="success",
                message="账户验证成功",
                credentials=credentials
            )
        else:
            return AccountVerificationResult(
                email=credentials.email,
                status="error",
                message="获取访问令牌失败，请检查凭证"
            )
    except Exception as e:
        logger.error(f"Error verifying account {credentials.email}: {e}")
        return AccountVerificationResult(
            email=credentials.email,
            status="error",
            message=f"验证过程出错: {str(e)}"
        )

@app.post("/accounts/import", response_model=List[AccountResponse])
async def import_verified_accounts(
    credentials: List[AccountCredentials],
    current_admin: bool = Depends(get_current_admin)
):
    """导入已验证的账户（不进行重复验证，直接保存）

    Args:
        credentials: 已验证的账户凭证列表

    Returns:
        导入结果列表
    """
    if not credentials:
        return []

    try:
        # 直接批量保存，不进行验证（使用 mail_service 持久化，包含可选 password 字段）
        await ms.save_multiple_accounts_batch(credentials)

        # 返回成功结果
        results = []
        for cred in credentials:
            results.append(AccountResponse(
                email_id=cred.email,
                message="账户已成功导入"
            ))

        logger.info(f"成功导入 {len(credentials)} 个账户（无重复验证）")
        return results

    except Exception as e:
        logger.error(f"批量导入失败: {e}")
        # 返回失败结果
        results = []
        for cred in credentials:
            results.append(AccountResponse(
                email_id=cred.email,
                message=f"导入失败: {str(e)}"
            ))
        return results

@app.delete("/accounts")
async def delete_multiple_accounts(
    request: AccountDeleteRequest,
    current_admin: bool = Depends(get_current_admin)
):
    """批量删除账户

    Args:
        request: 包含要删除的邮箱地址列表的请求

    Returns:
        删除操作的结果统计
    """
    if not request.emails:
        return {"message": "没有指定要删除的账户", "deleted": 0, "not_found": 0}

    result = await ms.delete_accounts(request.emails)
    return {
        "message": f"成功删除 {result['deleted']} 个账户，{result['not_found']} 个账户未找到",
        **result
    }


# ============================================================================
# 启动配置
# ============================================================================

# =============================
# GPT Register API (Rewritten scaffold)
# =============================
from pydantic import BaseModel
from typing import List, Optional

class RunAccount(BaseModel):
    email: str
    password: Optional[str] = None
    display_name: Optional[str] = "user"
    dob: Optional[str] = "1999-09-09"
    proxy: Optional[str] = None  # 格式: "http://user:pass@ip:port" 或 "socks5://ip:port"
    user_agent: Optional[str] = None  # 自定义 User-Agent

class RunRequest(BaseModel):
    accounts: Optional[List[RunAccount]] = None
    flow: Optional[dict] = None
    headful: bool = True  # 默认显示窗口以查看无痕模式UI
    concurrency: Optional[int] = None
    limit: Optional[int] = None

@app.post("/gpt-register/runs")
async def start_gpt_register_run(req: RunRequest, current_admin: bool = Depends(get_current_admin)):
    # Lazy import to avoid heavy dependencies at import time
    from gpt_register import jobs as ag_jobs
    run_dict = req.model_dump()
    run_id = ag_jobs.create_run(run_dict)
    asyncio.create_task(ag_jobs.execute_run(run_id))
    return {"run_id": run_id}

@app.get("/gpt-register/runs/{run_id}")
async def get_gpt_register_status(run_id: str, current_admin: bool = Depends(get_current_admin)):
    from gpt_register import jobs as ag_jobs
    return ag_jobs.get_status(run_id)

@app.get("/gpt-register/runs/{run_id}/events")
async def gpt_register_events(run_id: str, current_admin: bool = Depends(get_current_admin)):
    from gpt_register import jobs as ag_jobs
    last_event_id = None

    async def event_gen():
        nonlocal last_event_id
        try:
            while True:
                status = ag_jobs.get_status(run_id)
                current_event = status.get("last_event")

                # 使用事件的 event_id 来判断是否为新事件
                current_event_id = current_event.get("event_id") if current_event else None

                # 只有当事件 ID 发生变化时才发送
                if current_event_id and current_event_id != last_event_id:
                    yield f"data: {json.dumps(status, ensure_ascii=False)}\n\n"
                    last_event_id = current_event_id
                elif not current_event and last_event_id is None:
                    # 首次连接时发送状态
                    yield f"data: {json.dumps(status, ensure_ascii=False)}\n\n"

                # 结束条件
                if status.get("status") in ("completed", "failed", "not_found", "cancelled"):
                    break
                # 限频：1s 间隔，避免过于频繁的状态轮询
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            # 客户端断开时优雅结束生成器，避免后台无限循环
            return

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=headers)

@app.post("/gpt-register/runs/{run_id}/cancel")
async def cancel_gpt_run(run_id: str, current_admin: bool = Depends(get_current_admin)):
    from gpt_register import jobs as ag_jobs
    ag_jobs.cancel_run(run_id)
    return {"run_id": run_id, "status": "cancelling"}

@app.post("/gpt-register/runs/{run_id}/pause")
async def pause_gpt_run(run_id: str, current_admin: bool = Depends(get_current_admin)):
    from gpt_register import jobs as ag_jobs
    ag_jobs.pause_run(run_id)
    return {"run_id": run_id, "status": "paused"}

@app.post("/gpt-register/runs/{run_id}/resume")
async def resume_gpt_run(run_id: str, current_admin: bool = Depends(get_current_admin)):
    from gpt_register import jobs as ag_jobs
    ag_jobs.resume_run(run_id)
    return {"run_id": run_id, "status": "resumed"}

# Config editor endpoints (GET/PUT)
@app.get("/config/settings.yaml")
async def get_settings_yaml(current_admin: bool = Depends(get_current_admin)):
    p = Path("config/settings.yaml")
    if not p.exists():
        raise HTTPException(status_code=404, detail="settings.yaml not found")
    return FileResponse(str(p))

@app.put("/config/settings.yaml")
async def put_settings_yaml(body: str, current_admin: bool = Depends(get_current_admin)):
    p = Path("config/settings.yaml")
    p.write_text(body, encoding="utf-8")
    return {"status": "ok"}

@app.get("/config/flow.yaml")
async def get_flow_yaml(current_admin: bool = Depends(get_current_admin)):
    p = Path("config/flow.yaml")
    if not p.exists():
        raise HTTPException(status_code=404, detail="flow.yaml not found")
    return FileResponse(str(p))

@app.put("/config/flow.yaml")
async def put_flow_yaml(body: str, current_admin: bool = Depends(get_current_admin)):
    p = Path("config/flow.yaml")
    p.write_text(body, encoding="utf-8")
    return {"status": "ok"}

@app.get("/gpt-register/runs/{run_id}/export")
async def export_run_results(run_id: str, format: str = "csv", status: str | None = "success", current_admin: bool = Depends(get_current_admin)):
    import sqlite3, io, csv
    db_path = Path("run-logs") / "runs.sqlite"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="database not found")
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        if status:
            cur.execute("SELECT email, status, attempts, duration_sec, error_json FROM results WHERE run_id=? AND status=?", (run_id, status))
        else:
            cur.execute("SELECT email, status, attempts, duration_sec, error_json FROM results WHERE run_id=?", (run_id,))
        rows = cur.fetchall()
    finally:
        conn.close()
    if format == "json":
        data = [
            {"email": r[0], "status": r[1], "attempts": r[2], "duration_sec": r[3], "error": json.loads(r[4]) if r[4] else None}
            for r in rows
        ]
        return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/json")
    # default csv
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["email", "status", "attempts", "duration_sec"])
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3]])
    resp = Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = f"attachment; filename=run_{run_id}_{status or 'all'}.csv"
    return resp


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
