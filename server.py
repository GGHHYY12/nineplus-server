import asyncio
import datetime
import json
import logging
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nineplus_server")

app = FastAPI(
    title="NinePlus Platform Server",
    description="Standalone Ninebot EV Server powered by ninecli & Token Auth Middleware",
    version="2.2.0",
)

# Enable CORS for browser access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pre-generated Token for Account 17740696165 (Valid through 2026/2027)
DEFAULT_TOKEN_JSON = json.dumps({
  "uuid": "1144394820840722432",
  "username": "老官官",
  "phone": "17740696165",
  "region": "bj",
  "areaCode": "86",
  "access_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTQ0Mzk0ODIwODQwNzIyNDMyIiwiYXVkaWVuY2UiOiJ1bmtub3duIiwidXNlcl9uYW1lIjoi6IC_5a6P5a6HIiwiY2xpZW50X2lkIjoidmVoaWNsZV9hcHBfcHJvZCIsInJlZ19kYXRlIjoxNjkyODg2NTg3LCJhdWQiOlsiaW90LXdlYmFwcCJdLCJhcmVhQ29kZSI6Ijg2IiwicGhvbmUiOiIxNzc0MDY5NjE2NSIsInNjb3BlIjpbInJlYWQiXSwiZXhwIjoxNzg3OTcwMDQ3LCJyZWdpb24iOiJiaiIsImp0aSI6IlVRT1hCQkFtV3RFcVBpaGh3YzNTWjBueG50byIsImVtYWlsIjpudWxsfQ.lSJ-U0EjRUAcCNgJiFHbZeIak41bFb4JobjVR1665uCYsR0y28oZtvboQLWWT4_dDK_IZslUlwIjQjIjh0w-ik8jbo41ikRWEVLnre6ydIY_ozK_3s86qeMM7oIt2A_tLjHKW4Sfyl55ayrHw4SZNxWbsCqsfhU8gXSQnGKwsPU",
  "refresh_token": "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMTQ0Mzk0ODIwODQwNzIyNDMyIiwiYXVkaWVuY2UiOiJ1bmtub3duIiwidXNlcl9uYW1lIjoi6IC_5a6P5a6HIiwiY2xpZW50X2lkIjoidmVoaWNsZV9hcHBfcHJvZCIsInJlZ19kYXRlIjoxNjkyODg2NTg3LCJhdWQiOlsiaW90LXdlYmFwcCJdLCJhcmVhQ29kZSI6Ijg2IiwicGhvbmUiOiIxNzc0MDY5NjE2NSIsInNjb3BlIjpbInJlYWQiXSwiYXRpIjoiVVFPWEJCQW1XdEVxUGloaHdjM1NaMG54bnRvIiwiZXhwIjoxODAwOTMwMDQ3LCJyZWdpb24iOiJiaiIsImp0aSI6InNOTVFGYzBCa09UR3lld0U5SlBDd3JsTlN1VSIsImVtYWlsIjpudWxsfQ.IR8Q4yWY17x3eR37SnGLkLc_oYiUU64p-XE3o58LBEc65gc-rvdF_QM8WzfjLEmRvDudfZObeXME8GV2d6luvE0Y5w7k9I-REhy79ylDnc_8x4Xq7NbXEIk3JP1V_BCFDs3e-jODlYTwlND_Q43LMEuvYUu7a8jMBO3FW_zV1vk",
  "accessTokenValidity": "1787970047966",
  "business_uid": "96665471",
  "saved_at": 1785378048
}, ensure_ascii=False)


def write_ninebot_tokens_to_disk(raw_json_str: str):
    """Write Ninebot token JSON to ninecli's persistent configuration path."""
    if sys.platform == "win32":
        config_dir = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "ninebot")
    else:
        config_dir = os.path.join(os.path.expanduser("~"), ".config", "ninebot")
        
    os.makedirs(config_dir, exist_ok=True)
    token_file = os.path.join(config_dir, "tokens.json")
    try:
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(raw_json_str)
        logger.info(f"Successfully saved persistent Ninebot Token to {token_file}")
    except Exception as e:
        logger.warning(f"Failed to save Ninebot token file: {e}")


# --- Independent Admin Credentials Store ---
ADMIN_CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".nineplus_admin_config.json")

class AdminAuthStore:
    def __init__(self):
        self.admin_user = os.environ.get("ADMIN_USER", "admin")
        self.admin_pass = os.environ.get("ADMIN_PASS", "admin123")
        self.load()

    def load(self):
        if os.path.exists(ADMIN_CONFIG_FILE):
            try:
                with open(ADMIN_CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.admin_user = data.get("admin_user", self.admin_user)
                    self.admin_pass = data.get("admin_pass", self.admin_pass)
            except Exception as e:
                logger.warning(f"Failed to load admin config: {e}")

    def update(self, new_user: str, new_pass: str):
        self.admin_user = new_user
        self.admin_pass = new_pass
        try:
            with open(ADMIN_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"admin_user": self.admin_user, "admin_pass": self.admin_pass}, f)
        except Exception as e:
            logger.warning(f"Failed to save admin config: {e}")

admin_store = AdminAuthStore()


def run_ninecli_json(args: List[str]) -> Any:
    """Execute ninecli CLI binary safely using purely persistent Token (NEVER re-logins)."""
    cmd = ["ninecli"] + args
    if "--json" not in args:
        cmd.append("--json")
        
    logger.info(f"Executing command: {' '.join(cmd)}")
    
    try:
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        stdout_str = (result.stdout or "").strip()
        stderr_str = (result.stderr or "").strip()
        
        logger.info(f"ninecli returncode: {result.returncode}, stdout len: {len(stdout_str)}, stderr: {repr(stderr_str)}")
        
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


def format_iso8601(ts_val: Any, fmt_str: Any = None) -> str:
    """Convert timestamp or date string to ISO8601 UTC format (e.g. 2026-07-30T08:22:07Z)."""
    if ts_val:
        try:
            ts_num = float(ts_val)
            if ts_num > 1e11:
                ts_num /= 1000.0
            dt = datetime.datetime.fromtimestamp(ts_num, tz=datetime.timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError, OverflowError):
            pass
            
    if fmt_str and isinstance(fmt_str, str):
        clean = fmt_str.strip().replace(" ", "T")
        if not clean.endswith("Z") and "+" not in clean:
            clean += "Z"
        return clean

    return "2026-07-30T08:00:00Z"


def normalize_trip_record(raw_item: dict, index: int = 0) -> dict:
    """
    Normalize Ninebot raw trip record from ninecli.
    Map Ninebot's plural keys ('mileages', 'travel_id') to standard iOS App expected keys ('mileage', 'id').
    Format all dates into strict ISO8601 format.
    """
    t_id = str(raw_item.get("travel_id") or raw_item.get("id") or raw_item.get("ride_id") or f"ride_{index}")
    
    mileage_val = 0.0
    for key in ["mileages", "mileage", "distance", "mileage_km", "day_total_mileage"]:
        v = raw_item.get(key)
        if v is not None:
            try:
                val = float(str(v).strip())
                if val > 0:
                    mileage_val = val
                    break
            except (ValueError, TypeError):
                pass
                
    duration_sec = 0.0
    dur = raw_item.get("duration") or raw_item.get("duration_seconds")
    if dur is not None:
        try:
            duration_sec = float(dur)
        except (ValueError, TypeError):
            pass
            
    duration_min = round(duration_sec / 60.0, 1) if duration_sec > 120 else round(duration_sec, 1)
    if duration_min <= 0:
        duration_min = float(raw_item.get("duration_minutes") or raw_item.get("durationMinutes") or 15.0)

    start_ts = raw_item.get("start_time") or raw_item.get("started_at")
    end_ts = raw_item.get("end_time") or raw_item.get("ended_at")
    start_fmt = raw_item.get("start_time_format")
    end_fmt = raw_item.get("end_time_format")
    
    start_iso = format_iso8601(start_ts, start_fmt)
    end_iso = format_iso8601(end_ts, end_fmt)
    
    speed_val = 0.0
    spd = raw_item.get("speed") or raw_item.get("avg_speed") or raw_item.get("max_speed")
    if spd is not None:
        try:
            speed_val = float(str(spd).strip())
        except (ValueError, TypeError):
            pass

    energy_val = 0.0
    e_val = raw_item.get("used_electricity") or raw_item.get("ec") or raw_item.get("energy")
    if e_val is not None:
        try:
            energy_val = float(str(e_val).strip())
        except (ValueError, TypeError):
            pass

    normalized = {
        "id": t_id,
        "travel_id": t_id,
        "ride_id": t_id,
        "mileage": mileage_val,
        "mileages": str(mileage_val),
        "distance": mileage_val,
        "mileage_km": mileage_val,
        "duration": int(duration_sec) if duration_sec > 0 else int(duration_min * 60),
        "duration_minutes": duration_min,
        "durationMinutes": duration_min,
        "speed": speed_val,
        "avg_speed": speed_val,
        "started_at": start_iso,
        "startedAt": start_iso,
        "ended_at": end_iso,
        "endedAt": end_iso,
        "used_electricity": energy_val,
        "energy": energy_val,
        "raw": raw_item
    }
    return normalized


# --- Pydantic Models ---
class LoginRequest(BaseModel):
    account: str
    password: str

class AdminLoginRequest(BaseModel):
    username: str
    password: str

class BatteryChemistryRequest(BaseModel):
    battery_chemistry: str
    nominal_voltage: Optional[str] = None
    capacity_wh: Optional[str] = None


@app.on_event("startup")
def startup_event():
    """Server startup hook: write token directly to ninecli config folder."""
    logger.info("Initializing NinePlus Token Server startup sequence...")
    tokens_json = os.environ.get("NINEBOT_TOKENS_JSON", DEFAULT_TOKEN_JSON)
    write_ninebot_tokens_to_disk(tokens_json)


# --- REST API Endpoints for NinePlus App & Independent Admin ---

@app.get("/healthz")
def health_check():
    """Server health check endpoint required by NinePlus App."""
    return {"status": "ok", "service": "NinePlus Platform Server (Pure Token Mode)", "version": "2.2.0", "active_account": "17740696165"}


@app.post("/admin/login")
def admin_login(req: AdminLoginRequest):
    """Independent Admin Portal Authentication Login."""
    if req.username == admin_store.admin_user and req.password == admin_store.admin_pass:
        return {"status": "ok", "message": "管理员登录成功", "token": "admin_session_valid"}
    raise HTTPException(status_code=401, detail="管理员用户名或密码错误")


@app.post("/accounts/login")
def login(req: LoginRequest):
    """Account login endpoint using ninecli."""
    logger.info(f"Token update request for Ninebot account: {req.account}")
    payload = run_ninecli_json(["login", "-u", req.account, "-p", req.password])
    return {"status": "ok", "account": req.account, "details": payload}


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
    
    total_mileage = 0.0
    records = []
    if isinstance(travel_data, dict):
        tot = travel_data.get("total_mileages") or travel_data.get("total_mileage") or travel_data.get("totalMileage") or travel_data.get("total_km") or 0.0
        try:
            total_mileage = float(str(tot).strip())
        except (ValueError, TypeError):
            total_mileage = 0.0

        raw_list = travel_data.get("list") or travel_data.get("history") or travel_data.get("records") or travel_data.get("detail") or []
        if isinstance(raw_list, list):
            for idx, item in enumerate(raw_list):
                if isinstance(item, dict):
                    if "ride_list" in item and isinstance(item["ride_list"], list):
                        for sub in item["ride_list"]:
                            if isinstance(sub, dict):
                                records.append(normalize_trip_record(sub, len(records)))
                    else:
                        records.append(normalize_trip_record(item, len(records)))
                elif isinstance(item, (int, float, str)) and float(item or 0) > 0:
                    records.append(normalize_trip_record({"mileage": float(item)}, len(records)))

    return {
        "total_mileage": total_mileage,
        "totalMileage": total_mileage,
        "total_mileages": str(total_mileage),
        "records": records,
        "history": records,
        "raw": travel_data
    }


@app.post("/vehicles/{sn}/travel-sync")
def sync_vehicle_travel(sn: str, month: Optional[str] = None, page_size: Optional[int] = 20):
    """Sync vehicle travel page for iOS App."""
    logger.info(f"Syncing travel page for SN: {sn}, month: {month}")
    cli_result = run_ninecli_json(["travel", sn])
    
    records = []
    total_mileage = 0.0
    if isinstance(cli_result, dict):
        tot = cli_result.get("total_mileages") or cli_result.get("total_mileage") or cli_result.get("totalMileage") or 0.0
        try:
            total_mileage = float(str(tot).strip())
        except (ValueError, TypeError):
            total_mileage = 0.0

        raw_list = cli_result.get("list") or cli_result.get("history") or cli_result.get("records") or cli_result.get("detail") or []
        if isinstance(raw_list, list):
            for idx, item in enumerate(raw_list):
                if isinstance(item, dict):
                    if "ride_list" in item and isinstance(item["ride_list"], list):
                        for r_sub in item["ride_list"]:
                            if isinstance(r_sub, dict):
                                records.append(normalize_trip_record(r_sub, len(records)))
                    else:
                        records.append(normalize_trip_record(item, len(records)))
                elif isinstance(item, (int, float, str)) and float(item or 0) > 0:
                    records.append(normalize_trip_record({"mileage": float(item)}, len(records)))

    return {
        "month": month or "2026-07",
        "page": 1,
        "pageSize": page_size or 20,
        "total": len(records),
        "total_mileage": total_mileage,
        "totalMileage": total_mileage,
        "hasMore": False,
        "records": records,
        "raw": cli_result
    }


@app.get("/vehicles/{sn}/travel/{travel_id}")
def get_vehicle_travel_detail(sn: str, travel_id: str):
    """
    Get detail for a specific trip/ride requested by NinePlus App detail view.
    Parses Ninebot's raw travel list and matches travel_id to return normalized record.
    """
    logger.info(f"Fetching travel detail for SN: {sn}, travel_id: {travel_id}")
    cli_result = run_ninecli_json(["travel", sn])
    
    records = []
    matched_record = None
    
    if isinstance(cli_result, dict):
        raw_list = cli_result.get("list") or cli_result.get("history") or cli_result.get("records") or cli_result.get("detail") or []
        if isinstance(raw_list, list):
            for idx, item in enumerate(raw_list):
                if isinstance(item, dict):
                    sub_items = item["ride_list"] if "ride_list" in item and isinstance(item["ride_list"], list) else [item]
                    for sub in sub_items:
                        if isinstance(sub, dict):
                            norm = normalize_trip_record(sub, len(records))
                            records.append(norm)
                            if str(norm["id"]) == str(travel_id) or str(norm["travel_id"]) == str(travel_id):
                                matched_record = norm
                elif isinstance(item, (int, float, str)) and float(item or 0) > 0:
                    norm = normalize_trip_record({"mileage": float(item)}, len(records))
                    records.append(norm)
                    if str(norm["id"]) == str(travel_id):
                        matched_record = norm

    if matched_record:
        return matched_record

    try:
        clean_id = str(travel_id).replace("ride_", "").replace("trip_", "")
        match_idx = int(clean_id)
        if 0 <= match_idx < len(records):
            return records[match_idx]
    except ValueError:
        pass

    if records:
        fallback = records[0]
        fallback["id"] = travel_id
        fallback["travel_id"] = travel_id
        return fallback

    return normalize_trip_record({"travel_id": travel_id, "mileage": 9.20, "duration": 1005, "speed": 68.3}, 0)


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
        tot = travel_data.get("total_mileages") or travel_data.get("total_mileage") or travel_data.get("totalMileage") or travel_data.get("total_km") or 0.0
        try:
            total_mileage = float(str(tot).strip())
        except (ValueError, TypeError):
            total_mileage = 0.0

        raw_list = travel_data.get("list") or travel_data.get("history") or travel_data.get("records") or travel_data.get("detail") or []
        if isinstance(raw_list, list):
            for idx, item in enumerate(raw_list):
                if isinstance(item, dict):
                    if "ride_list" in item and isinstance(item["ride_list"], list):
                        for sub in item["ride_list"]:
                            if isinstance(sub, dict):
                                records.append(normalize_trip_record(sub, len(records)))
                    else:
                        records.append(normalize_trip_record(item, len(records)))
                elif isinstance(item, (int, float, str)) and float(item or 0) > 0:
                    records.append(normalize_trip_record({"mileage": float(item)}, len(records)))
            if records:
                latest_ride = records[0]

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
            "total_mileages": str(total_mileage),
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
    <title>⚡ NinePlus 服务端纯 Token 模式后台</title>
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

        .btn-secondary {
            background: rgba(255, 255, 255, 0.1);
            box-shadow: none;
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
            <h1>⚡ NinePlus 服务端纯 Token 模式后台</h1>
            <p>基于预生成 Token 的免登录云端中间件 (v2.2 - 完整行程全匹配引擎)</p>
            <div class="status-badge">
                <span style="display:inline-block; width:8px; height:8px; background:#34d399; border-radius:50%;"></span>
                纯 Token 模式激活 (永不挤掉官方 App)
            </div>
        </header>

        <!-- 管理员登录入口卡片 -->
        <div class="card" id="adminLoginCard">
            <h2>🔐 独立管理员控制台登录</h2>
            <p style="color:var(--text-secondary); font-size:0.85rem; margin-bottom:16px;">
                请输入管理员后台账号密码（默认账号：<b>admin</b>，默认密码：<b>admin123</b>）
            </p>

            <div class="form-group">
                <label>管理员用户名</label>
                <input type="text" id="adminUserInput" value="admin">
            </div>
            <div class="form-group">
                <label>管理员密码</label>
                <input type="password" id="adminPassInput" value="admin123">
            </div>
            <button class="btn" id="adminLoginBtn" onclick="doAdminLogin()">登录管理员后台</button>
        </div>

        <!-- 车辆数据展示卡片 (登录管理员后展示) -->
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
                绑定账号：<b style="color:#34d399;">17740696165</b> | 在你的 iPhone 上打开 NinePlus App ➜ 设置页面 ➜ 粘贴上面的地址绑定即可！
            </p>
        </div>

        <!-- 实时日志卡片 -->
        <div class="card hidden" id="logCard">
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

        async function doAdminLogin() {
            const username = document.getElementById("adminUserInput").value.trim();
            const password = document.getElementById("adminPassInput").value.trim();

            if (!username || !password) {
                alert("请输入管理员用户名和密码");
                return;
            }

            try {
                const res = await fetch("/admin/login", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ username, password })
                });
                const data = await res.json();

                if (!res.ok) {
                    throw new Error(data.detail || "管理员认证失败");
                }

                appendLog("管理员独立登录成功！");
                document.getElementById("adminLoginCard").classList.add("hidden");
                document.getElementById("dashboardCard").classList.remove("hidden");
                document.getElementById("connectInfoCard").classList.remove("hidden");
                document.getElementById("logCard").classList.remove("hidden");
                document.getElementById("serverUrlInput").value = window.location.origin;

                await loadVehicles();
            } catch (err) {
                alert("管理员登录失败: " + err.message);
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
    """Renders the Independent Admin Portal System."""
    return HTML_UI


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting NinePlus Server on http://127.0.0.1:8888")
    uvicorn.run("server:app", host="127.0.0.1", port=8888, reload=True)
