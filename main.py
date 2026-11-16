import asyncio
import base64
import os
import random
import secrets
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified
from pydantic import BaseModel

from app.database import Base, engine, get_db
from app.models import DiscordAccount, Schedule, MessageLog
from app.crypto import encrypt_token, decrypt_token
from app.discord_api import (
    fetch_user_profile,
    fetch_joined_guilds,
    fetch_guild_text_channels,
    fetch_single_channel,
    send_channel_message,
    delete_channel_message,
    DiscordRateLimitError,
)

Base.metadata.create_all(bind=engine)

# AUTO-MIGRATE kolom baru
inspector = inspect(engine)
if "message_logs" in inspector.get_table_names():
    log_cols = [c["name"] for c in inspector.get_columns("message_logs")]
    if "account_id" not in log_cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE message_logs ADD COLUMN account_id INTEGER"))

if "schedules" in inspector.get_table_names():
    sched_cols = [c["name"] for c in inspector.get_columns("schedules")]
    with engine.begin() as conn:
        if "account_id" not in sched_cols:
            conn.execute(text("ALTER TABLE schedules ADD COLUMN account_id INTEGER"))
        if "last_sent_at" not in sched_cols:
            conn.execute(text("ALTER TABLE schedules ADD COLUMN last_sent_at TIMESTAMP"))
        if "next_run_at" not in sched_cols:
            conn.execute(text("ALTER TABLE schedules ADD COLUMN next_run_at TIMESTAMP"))

app = FastAPI(title="PulseCord SaaS Multi-Account")
templates = Jinja2Templates(directory="templates")


@app.middleware("http")
async def basic_auth_guard(request: Request, call_next):
    # Biarkan endpoint cron berjalan memakai CRON_SECRET tanpa terhalang Basic Auth
    if request.url.path == "/api/cron/tick":
        return await call_next(request)

    admin_user = os.getenv("DASHBOARD_USER", "admin")
    admin_pass = os.getenv("DASHBOARD_PASSWORD", "")

    # Jika DASHBOARD_PASSWORD diisi di Vercel, wajibkan login
    if admin_pass:
        auth_header = request.headers.get("Authorization", "")
        is_valid = False
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                user_in, pass_in = decoded.split(":", 1)
                is_valid = secrets.compare_digest(user_in, admin_user) and secrets.compare_digest(pass_in, admin_pass)
            except Exception:
                is_valid = False

        if not is_valid:
            return Response(
                content="Akses Ditolak: Masukkan Username & Password PulseCord.",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="PulseCord Private Area"'},
            )

    return await call_next(request)


class ManualTokenPayload(BaseModel):
    token: str


class AutomationStartPayload(BaseModel):
    channels: list
    message_content: str = ""
    min_delay: int = 1800
    max_delay: int = 1830
    auto_delete: bool = True


class ChannelControlPayload(BaseModel):
    message_content: str = ""
    min_delay: int = 1800
    max_delay: int = 1830
    auto_delete: bool = True


def _format_duration(seconds: int) -> str:
    jam = seconds // 3600
    menit = (seconds % 3600) // 60
    detik = seconds % 60
    return f"{jam}j {menit}m {detik}d" if jam > 0 else f"{menit}m {detik}d"


def _enrich_channels_with_cd(channels: list, acct_status: str, fallback_msg: str = "", fallback_min: int = 1800, fallback_max: int = 1830, fallback_del: bool = True) -> list:
    now = datetime.now()
    enriched = []
    for ch in (channels or []):
        item = dict(ch)
        ch_status = item.get("status") or ("Running" if acct_status == "Running" else "Paused")
        item["status"] = ch_status
        item["guild_name"] = item.get("guild_name") or "Discord Server"
        item["message_content"] = item.get("message_content") if item.get("message_content") is not None else fallback_msg
        item["min_delay"] = max(15, int(item.get("min_delay") or fallback_min or 1800))
        item["max_delay"] = max(item["min_delay"] + 5, int(item.get("max_delay") or fallback_max or 1830))
        item["auto_delete"] = fallback_del if item.get("auto_delete") is None else bool(item.get("auto_delete"))

        rem_sec = 0
        next_time_str = None
        if ch_status == "Running" and item.get("next_run_at"):
            try:
                dt = datetime.fromisoformat(item["next_run_at"])
                rem_sec = max(0, int((dt - now).total_seconds()))
                next_time_str = dt.strftime("%H:%M:%S")
            except ValueError:
                rem_sec = 0

        item["remaining_seconds"] = rem_sec
        item["next_run_time"] = next_time_str
        enriched.append(item)
    return enriched


def _merge_channel_configs(old_channels: list, new_channels: list, force_status: Optional[str] = None) -> list:
    old_map = {c["id"]: c for c in (old_channels or [])}
    merged = []
    for ch in (new_channels or []):
        item = dict(ch)
        prev = old_map.get(item["id"])
        min_d = max(15, int(item.get("min_delay") or 1800))
        max_d = max(min_d + 5, int(item.get("max_delay") or 1830))
        item["min_delay"] = min_d
        item["max_delay"] = max_d
        item["message_content"] = item.get("message_content") or ""
        item["auto_delete"] = True if item.get("auto_delete") is None else bool(item.get("auto_delete"))
        item["guild_name"] = item.get("guild_name") or "Discord Server"

        if force_status:
            item["status"] = force_status
        else:
            item["status"] = item.get("status") or (prev.get("status") if prev else "Paused") or "Paused"

        item.pop("remaining_seconds", None)
        item.pop("next_run_time", None)

        if prev:
            if prev.get("last_message_id"):
                item["last_message_id"] = prev["last_message_id"]
            if prev.get("last_sent_at"):
                item["last_sent_at"] = prev["last_sent_at"]
            if item["status"] == "Running" and prev.get("next_run_at"):
                item["next_run_at"] = prev["next_run_at"]
            else:
                item.pop("next_run_at", None)
        else:
            item.pop("next_run_at", None)

        merged.append(item)
    return merged


def _sync_schedule_summary(sched: Schedule):
    channels = sched.target_channels or []
    running_list = [c for c in channels if c.get("status") == "Running"]
    sched.status = "Running" if running_list else "Paused"
    upcoming = []
    for c in running_list:
        if c.get("next_run_at"):
            try:
                upcoming.append(datetime.fromisoformat(c["next_run_at"]))
            except ValueError:
                pass
    sched.next_run_at = min(upcoming) if upcoming else None


@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ENDPOINT PENGGANTI WORKER.PY UNTUK VERCEL CRON
@app.api_route("/api/cron/tick", methods=["GET", "POST"])
async def run_serverless_worker_tick(secret: str = "", db: Session = Depends(get_db)):
    expected_secret = os.getenv("CRON_SECRET", "")
    if expected_secret and secret != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized Cron Secret")

    schedules = db.query(Schedule).all()
    now = datetime.now()
    processed_count = 0

    for sched in schedules:
        acct = db.query(DiscordAccount).filter(DiscordAccount.id == sched.account_id).first()
        if not acct or not sched.target_channels:
            continue

        token = decrypt_token(acct.encrypted_token)
        channels = [dict(c) for c in sched.target_channels]
        changed = False

        for ch in channels:
            if ch.get("status") != "Running":
                continue

            # Batasi maksimal 4 pengiriman per tick agar tidak kena timeout 10 detik Vercel
            if processed_count >= 4:
                break

            next_iso = ch.get("next_run_at")
            is_due = True
            if next_iso:
                try:
                    if now < datetime.fromisoformat(next_iso):
                        is_due = False
                except ValueError:
                    is_due = True

            if not is_due:
                continue

            ch_id = ch["id"]
            ch_name = ch.get("name", ch_id)
            guild_name = ch.get("guild_name", "Server")
            label = f"#{ch_name} ({guild_name})"
            msg_content = (ch.get("message_content") or sched.message_content or "").strip()
            auto_delete = ch.get("auto_delete") if ch.get("auto_delete") is not None else sched.auto_delete
            min_delay = max(15, int(ch.get("min_delay") or sched.min_delay or 1800))
            max_delay = max(min_delay + 5, int(ch.get("max_delay") or sched.max_delay or 1830))

            if not msg_content:
                ch["status"] = "Paused"
                ch.pop("next_run_at", None)
                changed = True
                continue

            # 1. Hapus pesan sebelumnya jika auto_delete aktif
            if auto_delete and ch.get("last_message_id"):
                try:
                    await delete_channel_message(token, ch_id, ch["last_message_id"])
                    db.add(MessageLog(
                        account_id=acct.id,
                        log_type="DELETED",
                        channel_label=label,
                        detail="Pesan sebelumnya dihapus."
                    ))
                except Exception:
                    pass
                await asyncio.sleep(0.8)

            # 2. Kirim pesan baru
            try:
                msg_id = await send_channel_message(token, ch_id, msg_content)
                sent_now = datetime.now()
                delay = random.randint(min_delay, max_delay)
                next_time = sent_now + timedelta(seconds=delay)

                ch["last_message_id"] = msg_id
                ch["last_sent_at"] = sent_now.isoformat()
                ch["next_run_at"] = next_time.isoformat()
                sched.last_sent_at = sent_now
                changed = True
                processed_count += 1

                db.add(MessageLog(
                    account_id=acct.id,
                    log_type="SENT",
                    channel_label=label,
                    detail=f"Terkirim: \"{msg_content[:45]}{'...' if len(msg_content) > 45 else ''}\""
                ))
                db.add(MessageLog(
                    account_id=acct.id,
                    log_type="SLEEP",
                    channel_label=label,
                    detail=f"CD {_format_duration(delay)} (kirim lagi pukul {next_time.strftime('%H:%M:%S')})."
                ))

            except DiscordRateLimitError as rl:
                retry_time = datetime.now() + timedelta(seconds=rl.retry_after)
                ch["next_run_at"] = retry_time.isoformat()
                changed = True
                processed_count += 1
                db.add(MessageLog(
                    account_id=acct.id,
                    log_type="SLEEP",
                    channel_label=label,
                    detail=f"Slowmode Discord aktif: tunggu {_format_duration(rl.retry_after)} (kirim pukul {retry_time.strftime('%H:%M:%S')})."
                ))

            except Exception as e:
                retry_time = datetime.now() + timedelta(seconds=60)
                ch["next_run_at"] = retry_time.isoformat()
                changed = True
                processed_count += 1
                db.add(MessageLog(
                    account_id=acct.id,
                    log_type="ERROR",
                    channel_label=label,
                    detail=str(e)
                ))

            await asyncio.sleep(0.8)

        if changed:
            sched.target_channels = channels
            _sync_schedule_summary(sched)
            flag_modified(sched, "target_channels")
            db.commit()

    return {"status": "ok", "processed": processed_count, "time": now.strftime("%H:%M:%S")}


@app.get("/api/accounts")
async def get_all_accounts(db: Session = Depends(get_db)):
    accounts = db.query(DiscordAccount).all()
    results = []
    for acct in accounts:
        sched = db.query(Schedule).filter(Schedule.account_id == acct.id).first()
        enriched_targets = _enrich_channels_with_cd(
            sched.target_channels if sched else [],
            sched.status if sched else "Paused",
            sched.message_content if sched else "",
            sched.min_delay if sched else 1800,
            sched.max_delay if sched else 1830,
            sched.auto_delete if sched else True,
        )

        running_targets = [c for c in enriched_targets if c.get("status") == "Running"]
        worker_status = "Running" if running_targets else "Paused"

        active_cd_list = [c["remaining_seconds"] for c in running_targets if c["remaining_seconds"] > 0]
        remaining_seconds = min(active_cd_list) if active_cd_list else 0
        upcoming = [c["next_run_time"] for c in running_targets if c.get("next_run_time")]
        next_run_time = sorted(upcoming)[0] if upcoming else None
        last_sent_time = sched.last_sent_at.strftime("%H:%M:%S") if (sched and sched.last_sent_at) else "-"

        results.append({
            "id": acct.id,
            "username": acct.username,
            "avatar_url": acct.avatar_url,
            "status": acct.status,
            "targets_count": len(enriched_targets),
            "running_count": len(running_targets),
            "target_channels": enriched_targets,
            "worker_status": worker_status,
            "remaining_seconds": remaining_seconds,
            "max_delay": max([c["max_delay"] for c in enriched_targets], default=1830),
            "next_run_time": next_run_time,
            "last_sent_time": last_sent_time
        })
    return results


@app.post("/api/accounts/connect/manual")
async def connect_manual_token(payload: ManualTokenPayload, db: Session = Depends(get_db)):
    try:
        profile = await fetch_user_profile(payload.token)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e) or "Token Invalid atau Expired")

    acct = db.query(DiscordAccount).filter(DiscordAccount.discord_id == profile["discord_id"]).first()
    if not acct:
        acct = DiscordAccount()
        db.add(acct)
    
    acct.discord_id = profile["discord_id"]
    acct.username = profile["username"]
    acct.avatar_url = profile["avatar_url"]
    acct.encrypted_token = encrypt_token(payload.token)
    acct.auth_method = "Manual Token"
    acct.status = "Active"
    db.commit()
    
    sched = db.query(Schedule).filter(Schedule.account_id == acct.id).first()
    if not sched:
        db.add(Schedule(account_id=acct.id, min_delay=1800, max_delay=1830))
        db.commit()
        
    return {"message": "Account added successfully"}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, db: Session = Depends(get_db)):
    acct = db.query(DiscordAccount).filter(DiscordAccount.id == account_id).first()
    if acct:
        db.delete(acct)
        db.commit()
    return {"status": "Deleted"}


@app.get("/api/discord/{account_id}/guilds")
async def get_guilds(account_id: int, db: Session = Depends(get_db)):
    acct = db.query(DiscordAccount).filter(DiscordAccount.id == account_id).first()
    if not acct:
        raise HTTPException(status_code=404)
    return await fetch_joined_guilds(decrypt_token(acct.encrypted_token))


@app.get("/api/discord/{account_id}/guilds/{guild_id}/channels")
async def get_channels(account_id: int, guild_id: str, guild_name: str = "", db: Session = Depends(get_db)):
    acct = db.query(DiscordAccount).filter(DiscordAccount.id == account_id).first()
    if not acct:
        raise HTTPException(status_code=404)
    return await fetch_guild_text_channels(decrypt_token(acct.encrypted_token), guild_id, guild_name)


@app.get("/api/discord/{account_id}/channels/{channel_id}")
async def get_single_channel(account_id: int, channel_id: str, guild_name: str = "Custom Target", db: Session = Depends(get_db)):
    acct = db.query(DiscordAccount).filter(DiscordAccount.id == account_id).first()
    if not acct:
        raise HTTPException(status_code=404)
    try:
        return await fetch_single_channel(decrypt_token(acct.encrypted_token), channel_id, guild_name)
    except Exception:
        raise HTTPException(status_code=404)


@app.get("/api/automation/{account_id}")
async def get_schedule(account_id: int, db: Session = Depends(get_db)):
    sched = db.query(Schedule).filter(Schedule.account_id == account_id).first()
    if not sched:
        sched = Schedule(account_id=account_id, min_delay=1800, max_delay=1830)
        db.add(sched)
        db.commit()
        db.refresh(sched)

    enriched_channels = _enrich_channels_with_cd(
        sched.target_channels or [],
        sched.status or "Paused",
        sched.message_content or "",
        sched.min_delay or 1800,
        sched.max_delay or 1830,
        True if sched.auto_delete is None else sched.auto_delete,
    )
    overall_status = "Running" if any(c.get("status") == "Running" for c in enriched_channels) else "Paused"

    logs = db.query(MessageLog).filter(MessageLog.account_id == account_id).order_by(MessageLog.id.desc()).limit(25).all()
    return {
        "config": {
            "channels": enriched_channels,
            "message_content": sched.message_content or "",
            "min_delay": sched.min_delay or 1800,
            "max_delay": sched.max_delay or 1830,
            "auto_delete": True if sched.auto_delete is None else sched.auto_delete,
            "status": overall_status
        },
        "logs": [
            {
                "type": l.log_type,
                "channel": l.channel_label,
                "detail": l.detail,
                "time": l.created_at.strftime("%H:%M:%S") if l.created_at else ""
            }
            for l in logs
        ]
    }


@app.post("/api/automation/{account_id}/save")
async def save_draft(account_id: int, payload: AutomationStartPayload, db: Session = Depends(get_db)):
    sched = db.query(Schedule).filter(Schedule.account_id == account_id).first()
    if not sched:
        sched = Schedule(account_id=account_id)
        db.add(sched)
    sched.target_channels = _merge_channel_configs(sched.target_channels, payload.channels)
    _sync_schedule_summary(sched)
    flag_modified(sched, "target_channels")
    db.commit()
    return {"status": "Saved"}


@app.post("/api/automation/{account_id}/channels/{channel_id}/start")
async def start_single_channel(account_id: int, channel_id: str, payload: ChannelControlPayload, db: Session = Depends(get_db)):
    acct = db.query(DiscordAccount).filter(DiscordAccount.id == account_id).first()
    sched = db.query(Schedule).filter(Schedule.account_id == account_id).first()
    if not acct or not sched:
        raise HTTPException(status_code=404)

    token = decrypt_token(acct.encrypted_token)
    updated = []

    for ch in (sched.target_channels or []):
        item = dict(ch)
        if item["id"] == channel_id:
            min_d = max(15, int(payload.min_delay))
            max_d = max(min_d + 5, int(payload.max_delay))
            msg_content = (payload.message_content or "").strip()

            item["message_content"] = msg_content
            item["min_delay"] = min_d
            item["max_delay"] = max_d
            item["auto_delete"] = payload.auto_delete
            item["status"] = "Running"
            item.pop("next_run_at", None)

            ch_name = item.get("name", channel_id)
            guild_name = item.get("guild_name", "Server")
            label = f"#{ch_name} ({guild_name})"

            if msg_content:
                if payload.auto_delete and item.get("last_message_id"):
                    try:
                        await delete_channel_message(token, channel_id, item["last_message_id"])
                        db.add(MessageLog(
                            account_id=acct.id,
                            log_type="DELETED",
                            channel_label=label,
                            detail="Pesan sebelumnya dihapus."
                        ))
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)

                try:
                    msg_id = await send_channel_message(token, channel_id, msg_content)
                    sent_now = datetime.now()
                    delay = random.randint(min_d, max_d)
                    next_time = sent_now + timedelta(seconds=delay)

                    item["last_message_id"] = msg_id
                    item["last_sent_at"] = sent_now.isoformat()
                    item["next_run_at"] = next_time.isoformat()
                    sched.last_sent_at = sent_now

                    db.add(MessageLog(
                        account_id=acct.id,
                        log_type="SENT",
                        channel_label=label,
                        detail=f"Terkirim: \"{msg_content[:45]}{'...' if len(msg_content) > 45 else ''}\""
                    ))
                    db.add(MessageLog(
                        account_id=acct.id,
                        log_type="SLEEP",
                        channel_label=label,
                        detail=f"CD {_format_duration(delay)} (kirim lagi pukul {next_time.strftime('%H:%M:%S')})."
                    ))
                except DiscordRateLimitError as rl:
                    retry_time = datetime.now() + timedelta(seconds=rl.retry_after)
                    item["next_run_at"] = retry_time.isoformat()
                    db.add(MessageLog(
                        account_id=acct.id,
                        log_type="SLEEP",
                        channel_label=label,
                        detail=f"Slowmode Discord aktif: tunggu {_format_duration(rl.retry_after)}."
                    ))
                except Exception as e:
                    item["status"] = "Paused"
                    db.add(MessageLog(
                        account_id=acct.id,
                        log_type="ERROR",
                        channel_label=label,
                        detail=str(e)
                    ))

        updated.append(item)

    sched.target_channels = updated
    _sync_schedule_summary(sched)
    flag_modified(sched, "target_channels")
    db.commit()
    return {"status": "Running"}


@app.post("/api/automation/{account_id}/channels/{channel_id}/stop")
async def stop_single_channel(account_id: int, channel_id: str, payload: Optional[ChannelControlPayload] = None, db: Session = Depends(get_db)):
    sched = db.query(Schedule).filter(Schedule.account_id == account_id).first()
    if not sched:
        raise HTTPException(status_code=404)

    updated = []
    for ch in (sched.target_channels or []):
        item = dict(ch)
        if item["id"] == channel_id:
            if payload:
                min_d = max(15, int(payload.min_delay))
                max_d = max(min_d + 5, int(payload.max_delay))
                item["message_content"] = payload.message_content
                item["min_delay"] = min_d
                item["max_delay"] = max_d
                item["auto_delete"] = payload.auto_delete
            item["status"] = "Paused"
            item.pop("next_run_at", None)
        updated.append(item)

    sched.target_channels = updated
    _sync_schedule_summary(sched)
    flag_modified(sched, "target_channels")
    db.commit()
    return {"status": "Paused"}


@app.post("/api/automation/{account_id}/stop-all")
async def stop_all_channels(account_id: int, payload: Optional[AutomationStartPayload] = None, db: Session = Depends(get_db)):
    sched = db.query(Schedule).filter(Schedule.account_id == account_id).first()
    if sched:
        base_list = payload.channels if payload else (sched.target_channels or [])
        sched.target_channels = _merge_channel_configs(sched.target_channels, base_list, force_status="Paused")
        _sync_schedule_summary(sched)
        flag_modified(sched, "target_channels")
        db.commit()
    return {"status": "Paused"}


@app.get("/api/logs")
async def get_global_logs(account_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(MessageLog)
    if account_id:
        query = query.filter(MessageLog.account_id == account_id)
    logs = query.order_by(MessageLog.id.desc()).limit(50).all()

    acct_map = {a.id: a.username for a in db.query(DiscordAccount).all()}
    return [
        {
            "id": l.id,
            "account_id": l.account_id,
            "username": acct_map.get(l.account_id, "Unknown"),
            "type": l.log_type,
            "channel": l.channel_label,
            "detail": l.detail,
            "time": l.created_at.strftime("%H:%M:%S") if l.created_at else ""
        }
        for l in logs
    ]


@app.delete("/api/logs")
async def clear_global_logs(account_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(MessageLog)
    if account_id:
        query = query.filter(MessageLog.account_id == account_id)
    query.delete()
    db.commit()
    return {"status": "Cleared"}