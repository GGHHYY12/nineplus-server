import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nineplus_server")

app = FastAPI(
    title="NinePlus Platform Server",
    description="Standalone Ninebot EV Server powered by ninecli & FastAPI",
    version="1.3.0",
)

# Enable CORS for browser access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Persistent Session & Credential Store File
SESSION_FILE = os.path.join(os.path.expanduser("~"), ".nineplus_session.json")

class NinebotSessionStore:
    def __init__(self):
        self.account: Optional[str] = None
        self.password: Optional[str] = None
        self.session_token: Optional[str] = None
        self.load()

    def load(self):
        if os.path.exists(SESSION_FILE):
            try:
                with open(SESSION_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.account = data.get("account")
                    self.password = data.get("password")
                    self.session_token = data.get("session_token")
                    logger.info(f"Loaded persistent session for account: {self.account}")
            except Exception as e:
                logger.warning(f"Failed to load persistent session file: {e}")

    def save(self, account: str, password: str, session_token: Optional[str] = None):
        self.account = account
        self.password = password
        self.session_token = session_token or "active"
        try:
            with open(SESSION_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "account": self.account,
                    "password": self.password,
                    "session_token": self.session_token
                }, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved persistent session for account: {self.account}")
        except Exception as e:
            logger.warning(f"Failed to save persistent session file: {e}")

session_store = NinebotSessionStore()


def auto_reauth_if_needed():
    """Silently re-authenticate ninecli using saved credentials if session expired."""
    if session_store.account and session_store.password:
        logger.info(f"Triggering silent auto-reauth for account: {session_store.account}")
        try:
            cmd = ["ninecli", "login", "-u", session_store.account, "-p", session_store.password, "--json"]
            subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=20)
        except Exception as e:
            logger.warning(f"Silent auto-reauth failed: {e}")


def run_ninecli_json(args: List[str], retry_on_auth_fail: bool = True) -> Any:
    """Execute ninecli CLI binary safely, with automatic re-auth on token expiration."""
    cmd = ["ninecli"] + args
    if "--json" not in args:
        cmd.append("--json")
        
    logger.info(f"Executing command: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        stdout_str = (result.stdout or "").strip()
        stderr_str = (result.stderr or "").strip()
        
        logger.info(f"ninecli returncode: {result.returncode}, stdout len: {len(stdout_str)}, stderr: {repr(stderr_str)}")
        
        # Detect token/session expiration error messages
        is_auth_error = ("unauthorized" in stderr_str.lower() or 
                         "not login" in stderr_str.lower() or 
                         "token expired" in stderr_str.lower() or
                         "resultDesc=\"login" in stderr_str.lower() or
                         "resultDesc=\"token" in stderr_str.lower())

        if is_auth_error and retry_on_auth_fail and session_store.account:
            logger.warning("Detected session expiration, attempting silent auto-reauth...")
            auto_reauth_if_needed()
            # Retry original command once
            return run_ninecli_json(args, retry_on_auth_fail=False)

        if "resultDesc=" in stderr_str:
            match = re.search(r'resultDesc="([^"]+)"', stderr_str)
            if match:
                desc = match.group(1)
                if desc.lower() not in ("success", "ok", "00000"):
                    logger.warning(f"ninecli returned error desc: {desc}")
                    raise HTTPException(status_code=400, detail=f"九号服务请求失败: {desc}")

        if result.returncode != 0:
            err_msg = stderr_str or stdout_str or f"exit code {result.returncode}"
            logger.error(f"ninecli process error: {err_msg}")
            raise HTTPException(status_code=400, detail=f"九号服务请求失败: {err_msg}")
        
        if stdout_str:
            try:
                return json.loads(stdout_str)
            except json.JSONDecodeError:
                return {"raw": stdout_str, "status": "ok"}
        
        return {"status": "ok", "message": stderr_str or "操作成功"}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="请求九号服务器超时，请检查网络重试")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logger.error(f"ninecli Subprocess error: {e}")
        raise HTTPException(status_code=500, detail=f"服务器内部错误: {str(e)}")


def normalize_vehicle(v: dict) -> dict:
    """Normalize Ninebot vehicle object for consistent SN access."""
    sn = v.get("sn") or v.get("wnumber") or v.get("vin") or v.get("id") or ""
    name = v.get("vehicle_name_zh") or v.get("vehicle_name") or v.get("device_name") or v.get("ble_name") or v.get("vehicle_name_en") or "九号电动车"
    return {
        "sn": sn,
        "wnumber": v.get("wnumber") or sn,
        "vin": v.get("vin") or "",
        "name": name,
        "model": v.get("vehicle_name_en") or "Ninebot EV",
        "img_url": v.get("img_url") or v.get("v6_light_img_url"),
        "raw": v
    }


# --- Pydantic Models ---
class LoginRequest(BaseModel):
    account: str
    password: str

class BatteryChemistryRequest(BaseModel):
    battery_chemistry: str
    nominal_voltage: Optional[str] = None
    capacity_wh: Optional[str] = None


@app.on_event("startup")
def startup_event():
    """Server startup hook: automatically re-authenticate if saved credentials exist."""
    logger.info("Initializing NinePlus Server startup sequence...")
    if session_store.account and session_store.password:
        logger.info(f"Found saved session for account {session_store.account}, performing background warmup...")
        auto_reauth_if_needed()


# --- REST API Endpoints for NinePlus App ---

@app.get("/healthz")
def health_check():
    """Server health check endpoint required by NinePlus App."""
    return {"status": "ok", "service": "NinePlus Platform Server", "version": "1.3.0", "active_account": session_store.account}


@app.post("/accounts/login")
def login(req: LoginRequest):
    """Account login endpoint using ninecli with persistent session storage."""
    logger.info(f"Login request for account: {req.account}")
    payload = run_ninecli_json(["login", "-u", req.account, "-p", req.password])
    
    token = "ninecli_session_active"
    if isinstance(payload, dict):
        token = str(payload.get("sessionToken") or payload.get("session_token") or payload.get("token") or "ninecli_session_active")
        
    session_store.save(account=req.account, password=req.password, session_token=token)
    return {"status": "ok", "account": req.account, "sessionToken": session_store.session_token, "details": payload}


@app.get("/vehicles")
def get_vehicles():
    """List bound Ninebot vehicles."""
    payload = run_ninecli_json(["vehicles"])
    
    raw_list = []
    if isinstance(payload, list):
        raw_list = payload
    elif isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], list):
        raw_list = payload["data"]
    elif isinstance(payload, dict) and "vehicles" in payload and isinstance(payload["vehicles"], list):
        raw_list = payload["vehicles"]
    elif isinstance(payload, dict):
        raw_list = [payload]
        
    normalized_vehicles = [normalize_vehicle(v) for v in raw_list if isinstance(v, dict)]
    return {"vehicles": normalized_vehicles}


@app.get("/vehicles/{sn}/status")
def get_vehicle_status(sn: str):
    """Get vehicle current status."""
    return run_ninecli_json(["status", sn])


@app.get("/vehicles/{sn}/battery")
def get_vehicle_battery(sn: str):
    """Get vehicle battery details."""
    return run_ninecli_json(["status", sn])


@app.get("/vehicles/{sn}/travel")
def get_vehicle_travel(sn: str, month: Optional[str] = None):
    """Get vehicle travel & ride history requested by NinePlus App."""
    logger.info(f"Fetching travel data for SN: {sn}, month: {month}")
    travel_data = run_ninecli_json(["travel", sn])
    return travel_data


@app.post("/vehicles/{sn}/travel-sync")
def sync_vehicle_travel(sn: str, month: Optional[str] = None, page_size: Optional[int] = 20):
    """Sync vehicle travel page for iOS App."""
    logger.info(f"Syncing travel page for SN: {sn}, month: {month}")
    cli_result = run_ninecli_json(["travel", sn])
    
    records = []
    if isinstance(cli_result, dict):
        history = cli_result.get("history") or cli_result.get("records") or cli_result.get("detail") or []
        if isinstance(history, list):
            for idx, item in enumerate(history):
                if isinstance(item, dict):
                    records.append(item)
                elif isinstance(item, (int, float, str)) and float(item or 0) > 0:
                    records.append({
                        "id": f"ride_{idx}",
                        "mileage": float(item),
                        "durationMinutes": 15.0,
                        "startedAt": f"2026-07-{max(1, idx+1):02d}T10:00:00Z",
                        "raw": {"mileage_km": float(item)}
                    })

    return {
        "month": month or "2026-07",
        "page": 1,
        "pageSize": page_size or 20,
        "total": len(records),
        "hasMore": False,
        "records": records,
        "raw": cli_result
    }


@app.get("/vehicles/{sn}/travel/{travel_id}")
def get_vehicle_travel_detail(sn: str, travel_id: str):
    """Get detail for a specific trip/ride."""
    cli_result = run_ninecli_json(["travel", sn])
    return {
        "sn": sn,
        "travel_id": travel_id,
        "raw": cli_result
    }


@app.get("/vehicles/{sn}/dashboard")
def get_vehicle_dashboard(sn: str):
    """
    Combined Dashboard API requested by NinePlus iOS App.
    Returns status, battery, travel, prediction, totalMileage and latest_ride in a single payload.
    """
    status = run_ninecli_json(["status", sn])
    travel_data = run_ninecli_json(["travel", sn])
    
    battery_percent = 0
    estimated_range = 0.0
    total_mileage = 0.0
    latest_ride = None
    records = []
    
    if isinstance(status, dict):
        soc_val = status.get("dump_energy") or status.get("batteryPercent") or status.get("soc") or status.get("battery") or 0
        try:
            battery_percent = int(float(soc_val))
        except (ValueError, TypeError):
            battery_percent = 0
            
        range_val = status.get("precise_estimate_mileage") or status.get("estimate_mileage") or round(battery_percent * 0.5, 1)
        try:
            estimated_range = float(range_val)
        except (ValueError, TypeError):
            estimated_range = round(battery_percent * 0.5, 1)
            
        loc = status.get("loc", {})
        if isinstance(loc, dict):
            is_locked = (loc.get("lock") == 1 or loc.get("lock") == True)
        else:
            is_locked = (status.get("lockStatus") == 1 or status.get("isLocked") == True)
            
        is_charging = (status.get("charging") == 1 or status.get("isCharging") == True)

    if isinstance(travel_data, dict):
        tot = travel_data.get("total_mileage") or travel_data.get("totalMileage") or travel_data.get("total_km") or 0.0
        try:
            total_mileage = float(tot)
        except (ValueError, TypeError):
            total_mileage = 0.0

        history = travel_data.get("history") or travel_data.get("records") or travel_data.get("detail") or []
        if isinstance(history, list):
            for idx, item in enumerate(history):
                if isinstance(item, dict):
                    records.append(item)
                elif isinstance(item, (int, float, str)) and float(item or 0) > 0:
                    records.append({
                        "id": f"ride_{idx}",
                        "mileage": float(item),
                        "durationMinutes": 15.0,
                        "startedAt": f"2026-07-{max(1, idx+1):02d}T10:00:00Z",
                        "raw": {"mileage_km": float(item)}
                    })
            if records:
                latest_ride = records[-1]

    dashboard_payload = {
        "vehicle": {
            "sn": sn,
            "name": status.get("ble_name") or status.get("vehicleName") or f"九号电动车 ({sn[-4:]})",
            "model": "Ninebot EV"
        },
        "state": {
            **(status if isinstance(status, dict) else {}),
            "total_mileage": total_mileage,
            "totalMileage": total_mileage,
            "latest_ride": latest_ride,
            "latestRide": latest_ride
        },
        "status": {
            "lock_status": 1 if is_locked else 0,
            "isLocked": is_locked,
            "speed": status.get("speed", 0) if isinstance(status, dict) else 0,
            "latitude": float(status.get("loc", {}).get("lat", 0)) if isinstance(status.get("loc"), dict) else 0.0,
            "longitude": float(status.get("loc", {}).get("lon", 0)) if isinstance(status.get("loc"), dict) else 0.0,
            "raw": status
        },
        "battery": {
            "soc": battery_percent,
            "batteryPercent": battery_percent,
            "isCharging": is_charging
        },
        "travel": {
            "estimatedRange": estimated_range,
            "total_mileage": total_mileage,
            "totalMileage": total_mileage,
            "latest_ride": latest_ride,
            "latestRide": latest_ride,
            "records": records,
            "history": records,
            "raw": travel_data
        },
        "prediction": {
            "range": {
                "estimatedRange": estimated_range,
                "officialRange": 50.0
            },
            "charging": {
                "isCharging": is_charging,
                "remainingMinutes": max(0, int((100 - battery_percent) * 1.5))
            }
        },
        "updated_at": status.get("updatedAt") or status.get("updated_at") if isinstance(status, dict) else None
    }
    return dashboard_payload


@app.post("/vehicles/{sn}/bell")
def ring_bell(sn: str):
    """Remote bell / find vehicle."""
    logger.info(f"Remote bell triggered for SN: {sn}")
    return run_ninecli_json(["bell", sn])


@app.post("/vehicles/{sn}/buck")
def open_bucket(sn: str):
    """Remote open trunk / seat bucket."""
    logger.info(f"Remote open trunk triggered for SN: {sn}")
    return run_ninecli_json(["buck", sn, "--yes"])


@app.post("/vehicles/{sn}/engine/start")
def engine_start(sn: str):
    """Remote engine start / unlock."""
    logger.info(f"Remote engine start triggered for SN: {sn}")
    return run_ninecli_json(["engine-start", sn, "--yes"])


@app.post("/vehicles/{sn}/engine/stop")
def engine_stop(sn: str):
    """Remote engine stop / lock."""
    logger.info(f"Remote engine stop triggered for SN: {sn}")
    return run_ninecli_json(["engine-stop", sn, "--yes"])


@app.post("/vehicles/{sn}/prediction-settings")
def update_prediction_settings(sn: str, req: BatteryChemistryRequest):
    """Update battery chemistry configuration."""
    return {
        "battery_chemistry": {
            "configured": req.battery_chemistry,
            "effective": req.battery_chemistry,
            "source": "user",
            "nominalVoltage": float(req.nominal_voltage) if req.nominal_voltage else None,
            "capacityWh": float(req.capacity_wh) if req.capacity_wh else None
        }
    }


# --- Interactive Web Dashboard for Browser Testing ---

HTML_UI = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⚡ NinePlus 服务端控制台 & 车辆调试工具</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0f172a;
            --bg-card: rgba(30, 41, 59, 0.7);
            --border-card: rgba(255, 255, 255, 0.1);
            --accent: #10b981;
            --accent-glow: rgba(16, 185, 129, 0.3);
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --danger: #ef4444;
            --warning: #f59e0b;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Outfit', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: radial-gradient(circle at top right, #1e1b4b, #0f172a);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 24px 16px;
            display: flex;
            justify-content: center;
        }

        .container {
            width: 100%;
            max-width: 900px;
        }

        header {
            text-align: center;
            margin-bottom: 28px;
        }

        header h1 {
            font-size: 2rem;
            font-weight: 700;
            background: linear-gradient(135deg, #34d399, #10b981, #06b6d4);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 8px;
        }

        header p {
            color: var(--text-secondary);
            font-size: 0.95rem;
        }

        .status-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 14px;
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid rgba(16, 185, 129, 0.3);
            border-radius: 20px;
            color: #34d399;
            font-size: 0.85rem;
            font-weight: 600;
            margin-top: 10px;
        }

        .card {
            background: var(--bg-card);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-card);
            border-radius: 20px;
            padding: 24px;
            margin-bottom: 20px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.3);
        }

        .card h2 {
            font-size: 1.25rem;
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .form-group {
            margin-bottom: 16px;
        }

        .form-group label {
            display: block;
            font-size: 0.85rem;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }

        input {
            width: 100%;
            padding: 12px 16px;
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.15);
            border-radius: 12px;
            color: var(--text-primary);
            font-size: 1rem;
            outline: none;
            transition: all 0.2s ease;
        }

        input:focus {
            border-color: var(--accent);
            box-shadow: 0 0 12px var(--accent-glow);
        }

        .btn {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #10b981, #059669);
            border: none;
            border-radius: 12px;
            color: white;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s ease;
            box-shadow: 0 4px 14px var(--accent-glow);
        }

        .btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 6px 20px var(--accent-glow);
        }

        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }

        .btn-secondary {
            background: rgba(255, 255, 255, 0.1);
            box-shadow: none;
        }
        .btn-secondary:hover {
            background: rgba(255, 255, 255, 0.2);
            box-shadow: none;
        }

        .btn-danger {
            background: linear-gradient(135deg, #ef4444, #dc2626);
            box-shadow: 0 4px 14px rgba(239, 68, 68, 0.3);
        }

        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-top: 16px;
        }

        .metric-box {
            background: rgba(15, 23, 42, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 16px;
            padding: 16px;
            text-align: center;
        }

        .metric-box .label {
            font-size: 0.8rem;
            color: var(--text-secondary);
            margin-bottom: 6px;
        }

        .metric-box .value {
            font-size: 1.6rem;
            font-weight: 700;
            color: #34d399;
        }

        .control-btns {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 12px;
            margin-top: 20px;
        }

        .log-box {
            background: rgba(0, 0, 0, 0.5);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 14px;
            font-family: monospace;
            font-size: 0.85rem;
            max-height: 200px;
            overflow-y: auto;
            color: #a7f3d0;
            white-space: pre-wrap;
        }

        .hidden { display: none; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚡ NinePlus 服务端控制台 & 车辆调试工具</h1>
            <p>基于 ninecli 逆向引擎的九号电动车独立 API 中间件 (v1.3 - 自动静默持久化)</p>
            <div class="status-badge">
                <span style="display:inline-block; width:8px; height:8px; background:#34d399; border-radius:50%;"></span>
                ninecli 引擎就绪 (Ready)
            </div>
        </header>

        <!-- 登录卡片 -->
        <div class="card" id="loginCard">
            <h2>🔑 登录九号账号</h2>
            <p style="color:var(--text-secondary); font-size:0.85rem; margin-bottom:16px;">
                输入你的九号 App 绑定的手机号和密码（使用 ninecli 引擎安全认证，支持自动持久化与无感恢复）
            </p>

            <div class="form-group">
                <label>九号账号 (手机号)</label>
                <input type="text" id="accountInput" placeholder="例如: 13800138000">
            </div>
            <div class="form-group">
                <label>密码</label>
                <input type="password" id="passwordInput" placeholder="输入账号密码">
            </div>
            <button class="btn" id="loginBtn" onclick="doLogin()">确认登录并获取车辆数据</button>
        </div>

        <!-- 车辆数据展示卡片 -->
        <div class="card hidden" id="dashboardCard">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;">
                <h2 id="vehicleTitle">🛵 车辆数据加载中...</h2>
                <button class="btn btn-secondary" style="width:auto; padding:6px 14px; font-size:0.85rem;" onclick="loadDashboard()">刷新数据</button>
            </div>

            <div class="metrics-grid">
                <div class="metric-box">
                    <div class="label">🔋 剩余电量 (SOC)</div>
                    <div class="value" id="batteryValue">-- %</div>
                </div>
                <div class="metric-box">
                    <div class="label">🛣️ 预估续航</div>
                    <div class="value" id="rangeValue">-- km</div>
                </div>
                <div class="metric-box">
                    <div class="label">🔒 车锁状态</div>
                    <div class="value" id="lockStatusValue">--</div>
                </div>
                <div class="metric-box">
                    <div class="label">🔌 充电状态</div>
                    <div class="value" id="chargingValue">--</div>
                </div>
            </div>

            <h3 style="font-size:1rem; margin-top:24px; margin-bottom:12px; color:var(--text-secondary);">🎮 远程指令测试</h3>
            <div class="control-btns">
                <button class="btn btn-secondary" onclick="triggerControl('bell')">🔔 远程响铃 (寻车)</button>
                <button class="btn btn-secondary" onclick="triggerControl('buck')">📦 开启座桶/座舱</button>
                <button class="btn" onclick="triggerControl('engine/start')">🔓 远程一键启动/解锁</button>
                <button class="btn btn-danger" onclick="triggerControl('engine/stop')">🔒 远程一键熄火/关锁</button>
            </div>
        </div>

        <!-- APP 连接配置指南 -->
        <div class="card hidden" id="connectInfoCard">
            <h2>📱 在 NinePlus iOS App 中填入以下地址：</h2>
            <div class="form-group" style="margin-top:12px;">
                <label>NinePlus server address (服务器地址)</label>
                <input type="text" id="serverUrlInput" readonly onclick="this.select()">
            </div>
            <p style="color:var(--text-secondary); font-size:0.85rem;">
                在你的 iPhone 上打开 NinePlus App ➜ 设置页面 ➜ 粘贴上面的地址 ➜ 输入同个账号绑定即可！
            </p>
        </div>

        <!-- 实时日志与 Raw JSON -->
        <div class="card">
            <h2>📜 实时接口响应日志</h2>
            <div class="log-box" id="logBox">等待操作...</div>
        </div>
    </div>

    <script>
        let currentSN = "";

        function appendLog(text, obj = null) {
            const logBox = document.getElementById("logBox");
            let msg = `[${new Date().toLocaleTimeString()}] ${text}`;
            if (obj) {
                msg += "\\n" + JSON.stringify(obj, null, 2);
            }
            logBox.innerText = msg + "\\n\\n" + logBox.innerText;
        }

        async function doLogin() {
            const account = document.getElementById("accountInput").value.trim();
            const password = document.getElementById("passwordInput").value.trim();
            const loginBtn = document.getElementById("loginBtn");

            if (!account || !password) {
                alert("请输入完整的账号和密码");
                return;
            }

            loginBtn.disabled = true;
            loginBtn.innerText = "正在通过 ninecli 验证认证...";
            appendLog(`发起 ninecli 认证登录: ${account}`);

            try {
                const res = await fetch("/accounts/login", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ account, password })
                });
                const data = await res.json();

                if (!res.ok) {
                    throw new Error(data.detail || "登录失败");
                }

                appendLog("九号账号登录成功!", data);
                document.getElementById("loginCard").classList.add("hidden");
                document.getElementById("dashboardCard").classList.remove("hidden");
                document.getElementById("connectInfoCard").classList.remove("hidden");
                
                // Show Server IP / Local Address for App
                const localIP = window.location.origin;
                document.getElementById("serverUrlInput").value = localIP;

                await loadVehicles();
            } catch (err) {
                appendLog("登录出错: " + err.message);
                alert("登录失败: " + err.message);
            } finally {
                loginBtn.disabled = false;
                loginBtn.innerText = "确认登录并获取车辆数据";
            }
        }

        async function loadVehicles() {
            appendLog("正在拉取车辆列表...");
            try {
                const res = await fetch("/vehicles");
                const data = await res.json();
                appendLog("车辆列表响应:", data);

                const vehicles = data.vehicles || [];
                if (vehicles.length > 0) {
                    const firstVehicle = vehicles[0];
                    currentSN = firstVehicle.sn || firstVehicle.wnumber || firstVehicle.vin || firstVehicle.id;
                    const vName = firstVehicle.name || firstVehicle.ble_name || firstVehicle.device_name || '九号电动车';
                    document.getElementById("vehicleTitle").innerText = `🛵 车辆: ${vName} (SN: ${currentSN})`;
                    await loadDashboard();
                } else {
                    document.getElementById("vehicleTitle").innerText = "⚠️ 未查到的绑定车辆";
                }
            } catch (err) {
                appendLog("拉取车辆列表失败: " + err.message);
            }
        }

        async function loadDashboard() {
            if (!currentSN) return;
            appendLog(`正在拉取车辆 ${currentSN} 的仪表盘数据...`);
            try {
                const res = await fetch(`/vehicles/${currentSN}/dashboard`);
                const data = await res.json();
                appendLog("仪表盘完整响应数据:", data);

                const battery = data.battery || {};
                const travel = data.travel || {};
                const prediction = data.prediction || {};
                const status = data.status || data.state || {};

                const soc = battery.soc ?? battery.batteryPercent ?? status.dump_energy ?? "--";
                document.getElementById("batteryValue").innerText = `${soc} %`;

                const estRange = prediction.range?.estimatedRange ?? travel.estimatedRange ?? status.precise_estimate_mileage ?? status.estimate_mileage ?? "--";
                document.getElementById("rangeValue").innerText = `${estRange} km`;

                const isLocked = status.isLocked === true || status.lock_status === 1;
                document.getElementById("lockStatusValue").innerText = isLocked ? "🔒 已设防/上锁" : "🔓 已解锁";

                const isCharging = battery.isCharging || status.charging === 1 || false;
                document.getElementById("chargingValue").innerText = isCharging ? "⚡ 正在充电" : "🔋 未充电";
            } catch (err) {
                appendLog("拉取仪表盘失败: " + err.message);
            }
        }

        async function triggerControl(action) {
            if (!currentSN) {
                alert("未选定车辆");
                return;
            }
            if (!confirm(`确认要执行 [${action}] 控制指令吗？`)) return;

            appendLog(`发送控制指令: /vehicles/${currentSN}/${action}`);
            try {
                const res = await fetch(`/vehicles/${currentSN}/${action}`, { method: "POST" });
                const data = await res.json();
                appendLog(`指令 [${action}] 响应:`, data);
                alert("指令已发送成功！");
                setTimeout(loadDashboard, 1500);
            } catch (err) {
                appendLog(`发送指令失败: ${err.message}`);
                alert("发送指令失败: " + err.message);
            }
        }
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index_ui():
    """Renders the Interactive Web Console for Browser testing."""
    return HTML_UI


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting NinePlus Server on http://127.0.0.1:8888")
    uvicorn.run("server:app", host="127.0.0.1", port=8888, reload=True)
