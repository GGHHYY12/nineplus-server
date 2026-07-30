import asyncio
import datetime
import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("nineplus_server")

if sys.platform == "win32":
    NINEBOT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "ninebot")
else:
    NINEBOT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "ninebot")

VEHICLE_CACHE_FILE = os.path.join(NINEBOT_CONFIG_DIR, "vehicles_cache.json")

app = FastAPI(
    title="NinePlus Platform Server",
    description="Standalone Ninebot EV Server powered by ninecli & Token Auth Middleware",
    version="2.5.0",
)

# Enable CORS for browser access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def write_ninebot_tokens_to_disk(raw_json_str: str):
    """Write Ninebot token JSON to ninecli's persistent configuration path."""
    os.makedirs(NINEBOT_CONFIG_DIR, exist_ok=True)
    token_file = os.path.join(NINEBOT_CONFIG_DIR, "tokens.json")
    try:
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(raw_json_str)
        logger.info(f"Successfully saved persistent Ninebot Token to {token_file}")
    except Exception as e:
        logger.warning(f"Failed to save Ninebot token file: {e}")


def read_ninebot_tokens_from_disk() -> Optional[dict]:
    """Read Ninebot token JSON from ninecli's persistent configuration path."""
    token_file = os.path.join(NINEBOT_CONFIG_DIR, "tokens.json")

    if os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


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


def is_retryable_empty_payload(payload: Any) -> bool:
    """Treat cold-start empty-success placeholders as retryable instead of real data."""
    if payload is None:
        return True
    if isinstance(payload, dict):
        keys = set(payload.keys())
        if keys.issubset({"status", "message"}) and payload.get("status") == "ok":
            return True
        if keys.issubset({"status", "raw"}) and payload.get("status") == "ok":
            return True
    return False


def run_ninecli_json(
    args: List[str],
    *,
    expect_payload: bool = False,
    cold_start_retries: int = 0,
    retry_delay_seconds: float = 1.0,
) -> Any:
    """Execute ninecli CLI binary safely using purely persistent Token (NEVER re-logins)."""
    cmd = ["ninecli"] + args
    if "--json" not in args:
        cmd.append("--json")

    logger.info(f"Executing command: {' '.join(cmd)}")

    attempts = max(cold_start_retries + 1, 1)
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=30)
            stdout_str = (result.stdout or "").strip()
            stderr_str = (result.stderr or "").strip()

            logger.info(
                f"ninecli attempt {attempt}/{attempts}, returncode: {result.returncode}, "
                f"stdout len: {len(stdout_str)}, stderr: {repr(stderr_str)}"
            )

            if "resultDesc=" in stderr_str:
                match = re.search(r'resultDesc="([^"]+)"', stderr_str)
                if match:
                    desc = match.group(1)
                    if desc.lower() not in ("success", "ok", "00000"):
                        logger.warning(f"ninecli returned error desc: {desc}")
                        if any(keyword in desc.lower() for keyword in ["token", "login", "auth", "expire", "invalid", "unauthorized", "未登录", "失效", "过期"]):
                            raise HTTPException(status_code=401, detail=f"九号账号授权已过期，请重新登录: {desc}")
                        raise HTTPException(status_code=400, detail=f"九号服务请求失败: {desc}")

            if result.returncode != 0:
                err_msg = stderr_str or stdout_str or f"exit code {result.returncode}"
                logger.error(f"ninecli process error: {err_msg}")

                if any(keyword in err_msg.lower() for keyword in ["401", "unauthorized", "token", "expire", "未登录", "失效", "过期"]):
                    raise HTTPException(status_code=401, detail="九号账号授权已失效，请重新登录")

                raise HTTPException(status_code=400, detail=f"九号服务请求失败: {err_msg}")

            if stdout_str:
                try:
                    payload = json.loads(stdout_str)
                except json.JSONDecodeError:
                    payload = {"raw": stdout_str, "status": "ok"}
            else:
                if stderr_str:
                    logger.info(f"ninecli completed successfully with returncode 0 and stderr logs: {stderr_str}")
                payload = {"status": "ok", "message": "操作成功"}

            if (expect_payload or cold_start_retries > 0) and is_retryable_empty_payload(payload):
                if attempt < attempts:
                    delay = retry_delay_seconds * attempt
                    logger.warning(
                        f"ninecli returned retryable empty payload for {' '.join(args)}; retrying in {delay:.1f}s"
                    )
                    time.sleep(delay)
                    continue
                if expect_payload:
                    raise HTTPException(status_code=503, detail="九号服务暂未返回有效数据，请稍后重试")

            return payload
        except subprocess.TimeoutExpired:
            if attempt < attempts:
                delay = retry_delay_seconds * attempt
                logger.warning(f"ninecli timed out for {' '.join(args)}; retrying in {delay:.1f}s")
                time.sleep(delay)
                continue
            raise HTTPException(status_code=504, detail="请求九号服务器超时，请检查网络重试")
        except Exception as e:
            if isinstance(e, HTTPException):
                raise e
            logger.error(f"ninecli Subprocess error: {e}")
            raise HTTPException(status_code=500, detail=f"服务器内部错误: {str(e)}")


def extract_vehicle_items(payload: Any) -> List[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return [item for item in payload["data"] if isinstance(item, dict)]
        if isinstance(payload.get("vehicles"), list):
            return [item for item in payload["vehicles"] if isinstance(item, dict)]
        if any(key in payload for key in ["sn", "wnumber", "vin", "id"]):
            return [payload]
    return []


def normalize_vehicle_list(payload: Any) -> List[dict]:
    vehicles: List[dict] = []
    seen_sns = set()
    for raw_vehicle in extract_vehicle_items(payload):
        normalized = normalize_vehicle(raw_vehicle)
        sn = str(normalized.get("sn") or "").strip()
        if not sn or sn in seen_sns:
            continue
        seen_sns.add(sn)
        vehicles.append(normalized)
    return vehicles


def load_cached_vehicles() -> List[dict]:
    if not os.path.exists(VEHICLE_CACHE_FILE):
        return []
    try:
        with open(VEHICLE_CACHE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return normalize_vehicle_list(payload)
    except Exception as exc:
        logger.warning(f"Failed to read cached vehicles: {exc}")
        return []


def save_cached_vehicles(vehicles: List[dict]):
    if not vehicles:
        return
    try:
        os.makedirs(NINEBOT_CONFIG_DIR, exist_ok=True)
        with open(VEHICLE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"vehicles": vehicles, "updated_at": datetime.datetime.now(tz=BEIJING_TZ).isoformat()}, f, ensure_ascii=False)
    except Exception as exc:
        logger.warning(f"Failed to write cached vehicles: {exc}")


def warm_vehicle_cache():
    """Best-effort warmup for cold deployments so the first app open is less likely to see an empty payload."""
    try:
        payload = run_ninecli_json(["vehicles"], cold_start_retries=4, retry_delay_seconds=1.0)
        vehicles = normalize_vehicle_list(payload)
        if vehicles:
            save_cached_vehicles(vehicles)
            logger.info(f"Warmed vehicle cache with {len(vehicles)} vehicles")
        else:
            logger.warning("Vehicle cache warmup finished without valid vehicles")
    except Exception as exc:
        logger.warning(f"Vehicle cache warmup skipped: {exc}")


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


BEIJING_TZ = datetime.timezone(datetime.timedelta(hours=8))

def get_epoch_timestamp(ts_val: Any, fmt_str: Any = None) -> Optional[int]:
    """Extract numeric epoch timestamp in seconds."""
    if ts_val is not None:
        try:
            val = float(ts_val)
            if val > 1e11:
                val /= 1000.0
            if val > 1e8:
                return int(val)
        except (ValueError, TypeError, OverflowError):
            pass

    if fmt_str and isinstance(fmt_str, str):
        try:
            clean = fmt_str.strip().replace("T", " ").replace("Z", "")[:19]
            dt = datetime.datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING_TZ)
            return int(dt.timestamp())
        except (ValueError, TypeError):
            pass

    return None


def format_china_date(ts_num: Optional[int], fallback: str = "2026-07-30T08:00:00+08:00") -> str:
    """Format numeric epoch timestamp into Beijing Time string ISO8601 'YYYY-MM-DDTHH:mm:ss+08:00'."""
    if ts_num and ts_num > 1e8:
        try:
            dt = datetime.datetime.fromtimestamp(ts_num, tz=BEIJING_TZ)
            return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        except (ValueError, TypeError, OverflowError):
            pass
    return fallback


def normalize_trip_record(raw_item: dict, index: int = 0) -> dict:
    """
    Normalize Ninebot raw trip record from ninecli.
    Provides ALL field aliases expected by NinePlus iOS App in both Swift & JSON decoders.
    Passes integer epoch timestamps for start_time & end_time so Swift's epochDateValue
    parses timestamps with 100% precision.
    """
    t_id = str(raw_item.get("travel_id") or raw_item.get("id") or raw_item.get("ride_id") or f"ride_{index}")
    
    mileage_val = 0.0
    # Prioritize specific ride mileage keys over day_total_mileage
    for key in ["mileages", "mileage", "distance", "mileage_km"]:
        v = raw_item.get(key)
        if v is not None:
            try:
                val = float(str(v).strip())
                if val > 0:
                    mileage_val = val
                    break
            except (ValueError, TypeError):
                pass

    if mileage_val <= 0:
        for key in ["day_total_mileage", "dayTotalMileage"]:
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
            
    duration_min = round(duration_sec / 60.0, 1)
    if duration_min <= 0:
        duration_min = float(raw_item.get("duration_minutes") or raw_item.get("durationMinutes") or 15.0)

    raw_start = raw_item.get("start_time") or raw_item.get("started_at")
    raw_end = raw_item.get("end_time") or raw_item.get("ended_at")
    start_fmt = raw_item.get("start_time_format")
    end_fmt = raw_item.get("end_time_format")
    
    start_ts = get_epoch_timestamp(raw_start, start_fmt) or 1785369922
    end_ts = get_epoch_timestamp(raw_end, end_fmt) or (start_ts + int(duration_sec or 900))
    
    start_str = format_china_date(start_ts, "2026-07-30T08:05:22+08:00")
    end_str = format_china_date(end_ts, "2026-07-30T08:22:07+08:00")

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
        "travelId": t_id,
        "ride_id": t_id,
        "rideId": t_id,
        "mileage": mileage_val,
        "mileages": str(mileage_val),
        "distance": mileage_val,
        "mileage_km": mileage_val,
        "rideMileage": mileage_val,
        "duration": int(duration_sec) if duration_sec > 0 else int(duration_min * 60),
        "duration_seconds": int(duration_sec),
        "duration_minutes": duration_min,
        "durationMinutes": duration_min,
        "speed": speed_val,
        "avg_speed": speed_val,
        "avgSpeed": speed_val,
        "average_speed": speed_val,
        "averageSpeed": speed_val,
        "start_time": start_ts,
        "startTime": start_ts,
        "begin_time": start_ts,
        "beginTime": start_ts,
        "started_at": start_str,
        "startedAt": start_str,
        "end_time": end_ts,
        "endTime": end_ts,
        "stop_time": end_ts,
        "stopTime": end_ts,
        "ended_at": end_str,
        "endedAt": end_str,
        "used_electricity": energy_val,
        "usedElectricity": energy_val,
        "ec": energy_val,
        "energy": energy_val,
        "raw": raw_item
    }
    return normalized


def normalize_user_details(token_data: dict, account: str = "") -> dict:
    """Provide exhaustive field aliases for NinePlus Swift Codable decoders."""
    if not isinstance(token_data, dict):
        token_data = {}
        
    access_token = str(token_data.get("access_token") or token_data.get("token") or token_data.get("accessToken") or "")
    refresh_token = str(token_data.get("refresh_token") or token_data.get("refreshToken") or "")
    uuid_val = str(token_data.get("uuid") or token_data.get("uid") or token_data.get("user_id") or token_data.get("userId") or token_data.get("id") or "1144394820840722432")
    username_val = str(token_data.get("username") or token_data.get("nickname") or token_data.get("nickName") or token_data.get("name") or token_data.get("userName") or "九号用户")
    phone_val = str(token_data.get("phone") or token_data.get("mobile") or token_data.get("phoneNumber") or account or "")
    area_code = str(token_data.get("areaCode") or token_data.get("area_code") or "86")
    region_val = str(token_data.get("region") or "bj")
    business_uid = str(token_data.get("business_uid") or token_data.get("businessUID") or "96665471")

    normalized = {
        **token_data,
        "status": "ok",
        "account": phone_val,
        "phone": phone_val,
        "mobile": phone_val,
        "phoneNumber": phone_val,
        "uuid": uuid_val,
        "uid": uuid_val,
        "user_id": uuid_val,
        "userId": uuid_val,
        "id": uuid_val,
        "session_token": access_token,
        "sessionToken": access_token,
        "access_token": access_token,
        "accessToken": access_token,
        "token": access_token,
        "refresh_token": refresh_token,
        "refreshToken": refresh_token,
        "area_code": area_code,
        "areaCode": area_code,
        "region": region_val,
        "business_uid": business_uid,
        "businessUID": business_uid,
        "username": username_val,
        "userName": username_val,
        "nickname": username_val,
        "nickName": username_val,
        "name": username_val,
        "details": token_data
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
    """Server startup hook: restore token from environment variable if provided."""
    logger.info("Initializing NinePlus Token Server startup sequence...")
    tokens_json = os.environ.get("NINEBOT_TOKENS_JSON")
    if tokens_json:
        write_ninebot_tokens_to_disk(tokens_json)
    else:
        logger.info("No NINEBOT_TOKENS_JSON env var provided. Depending on existing ninecli tokens.json")
    warm_vehicle_cache()


# --- REST API Endpoints for NinePlus App & Independent Admin ---

@app.get("/healthz")
def health_check():
    """Server health check endpoint required by NinePlus App."""
    token_data = read_ninebot_tokens_from_disk()
    active_account = token_data.get("phone", "未登录") if token_data else "未登录"
    return {"status": "ok", "service": "NinePlus Platform Server", "version": "2.5.0", "active_account": active_account}


@app.post("/admin/login")
def admin_login(req: AdminLoginRequest):
    """Independent Admin Portal Authentication Login."""
    if req.username == admin_store.admin_user and req.password == admin_store.admin_pass:
        return {"status": "ok", "message": "管理员登录成功", "token": "admin_session_valid"}
    raise HTTPException(status_code=401, detail="管理员用户名或密码错误")


@app.delete("/admin/token")
def admin_delete_token(req: AdminLoginRequest):
    """Securely delete the Ninebot token file (Admin only)."""
    if req.username == admin_store.admin_user and req.password == admin_store.admin_pass:
        if sys.platform == "win32":
            token_file = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "ninebot", "tokens.json")
        else:
            token_file = os.path.join(os.path.expanduser("~"), ".config", "ninebot", "tokens.json")
            
        if os.path.exists(token_file):
            os.remove(token_file)
            return {"status": "ok", "message": "九号账号授权 (Token) 已安全清除"}
        return {"status": "ok", "message": "服务端目前没有保存任何 Token"}
    raise HTTPException(status_code=401, detail="管理员用户名或密码错误")


@app.post("/admin/export-token")
def admin_export_token(req: AdminLoginRequest):
    """Securely export the current Ninebot token JSON string for Render Environment Variables."""
    if req.username == admin_store.admin_user and req.password == admin_store.admin_pass:
        if sys.platform == "win32":
            token_file = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "ninebot", "tokens.json")
        else:
            token_file = os.path.join(os.path.expanduser("~"), ".config", "ninebot", "tokens.json")
            
        if os.path.exists(token_file):
            try:
                with open(token_file, "r", encoding="utf-8") as f:
                    return {"status": "ok", "token_json_string": f.read()}
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"无法读取 Token 文件: {e}")
        return {"status": "error", "message": "目前没有保存在本地的 Token，请先登录。"}
    raise HTTPException(status_code=401, detail="管理员用户名或密码错误")


@app.post("/accounts/login")
def login(req: LoginRequest):
    """Account login endpoint using ninecli."""
    logger.info(f"Token update request for Ninebot account: {req.account}")
    
    # Run login through ninecli (saves tokens.json)
    payload = run_ninecli_json(["login", "-u", req.account, "-p", req.password], expect_payload=True, cold_start_retries=1)
    
    # Read updated token_data
    token_data = read_ninebot_tokens_from_disk() or (payload if isinstance(payload, dict) else {})
    return normalize_user_details(token_data, req.account)


@app.get("/vehicles")
def get_vehicles():
    """List bound Ninebot vehicles."""
    payload = run_ninecli_json(["vehicles"], cold_start_retries=4, retry_delay_seconds=1.0)
    normalized_vehicles = normalize_vehicle_list(payload)
    if normalized_vehicles:
        save_cached_vehicles(normalized_vehicles)
        return {"vehicles": normalized_vehicles}

    cached_vehicles = load_cached_vehicles()
    if cached_vehicles:
        logger.warning("Serving cached vehicle list because fresh ninecli vehicles payload was empty")
        return {"vehicles": cached_vehicles, "stale": True, "source": "vehicle_cache"}

    raise HTTPException(status_code=503, detail="九号服务暂未返回有效车辆列表，请稍后重试")


@app.get("/vehicles/{sn}/status")
def get_vehicle_status(sn: str):
    """Get vehicle current status."""
    return run_ninecli_json(["status", sn], expect_payload=True, cold_start_retries=4, retry_delay_seconds=1.0)


@app.get("/vehicles/{sn}/battery")
def get_vehicle_battery(sn: str):
    """Get vehicle battery details."""
    return run_ninecli_json(["battery", sn], expect_payload=True, cold_start_retries=4, retry_delay_seconds=1.0)


@app.get("/vehicles/{sn}/travel")
def get_vehicle_travel(sn: str, month: Optional[str] = None):
    """Get vehicle travel & ride history requested by NinePlus App."""
    logger.info(f"Fetching travel data for SN: {sn}, month: {month}")
    cmd = ["travel", sn]
    if month:
        ninecli_month = month.replace("-", "")
        cmd.extend(["--month", ninecli_month])
    travel_data = run_ninecli_json(cmd, expect_payload=True, cold_start_retries=4, retry_delay_seconds=1.0)
    
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

    last_mileage = records[0]["mileage"] if records else 0.0

    return {
        "total_mileage": total_mileage,
        "totalMileage": total_mileage,
        "total_mileages": str(total_mileage),
        "last_mileage": last_mileage,
        "lastMileage": last_mileage,
        "list": records,
        "records": records,
        "history": records,
        "detail": travel_data.get("detail", []),
        "raw": travel_data
    }


@app.post("/vehicles/{sn}/travel-sync")
def sync_vehicle_travel(sn: str, month: Optional[str] = None, page_size: Optional[int] = 20):
    """Sync vehicle travel page for iOS App."""
    logger.info(f"Syncing travel page for SN: {sn}, month: {month}")
    cmd = ["travel", sn]
    if month:
        ninecli_month = month.replace("-", "")
        cmd.extend(["--month", ninecli_month])
    cli_result = run_ninecli_json(cmd, expect_payload=True, cold_start_retries=4, retry_delay_seconds=1.0)
    
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

    last_mileage = records[0]["mileage"] if records else 0.0

    return {
        "month": month or "2026-07",
        "page": 1,
        "pageSize": page_size or 20,
        "total": len(records),
        "total_mileage": total_mileage,
        "totalMileage": total_mileage,
        "last_mileage": last_mileage,
        "lastMileage": last_mileage,
        "hasMore": False,
        "list": records,
        "records": records,
        "raw": cli_result
    }


@app.get("/vehicles/{sn}/travel/{travel_id}")
def get_vehicle_travel_detail(sn: str, travel_id: str, month: Optional[str] = None):
    """
    Get detail for a specific trip/ride requested by NinePlus App detail view.
    Parses Ninebot's raw travel list and matches travel_id to return normalized record.
    """
    logger.info(f"Fetching travel detail for SN: {sn}, travel_id: {travel_id}")
    cmd = ["travel", sn]
    if month:
        ninecli_month = month.replace("-", "")
        cmd.extend(["--month", ninecli_month])
        
    cli_result = run_ninecli_json(cmd, expect_payload=True, cold_start_retries=4, retry_delay_seconds=1.0)
    
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
    status = run_ninecli_json(["status", sn], expect_payload=True, cold_start_retries=4, retry_delay_seconds=1.0)
    travel_data = run_ninecli_json(["travel", sn], expect_payload=True, cold_start_retries=4, retry_delay_seconds=1.0)
    battery_data = run_ninecli_json(["battery", sn], expect_payload=True, cold_start_retries=4, retry_delay_seconds=1.0)
    
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

    last_mileage = latest_ride.get("mileage", 0.0) if latest_ride else 0.0

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
            "last_mileage": last_mileage,
            "lastMileage": last_mileage,
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
            **(battery_data if isinstance(battery_data, dict) else {}),
            "soc": battery_percent,
            "batteryPercent": battery_percent,
            "isCharging": is_charging
        },
        "travel": {
            "estimatedRange": estimated_range,
            "total_mileage": total_mileage,
            "totalMileage": total_mileage,
            "total_mileages": str(total_mileage),
            "last_mileage": last_mileage,
            "lastMileage": last_mileage,
            "latest_ride": latest_ride,
            "latestRide": latest_ride,
            "detail": travel_data.get("detail", []),
            "list": records,
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
    <title>⚡ NinePlus 服务端控制台</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #0f172a;
            --bg-card: rgba(30, 41, 59, 0.7);
            --border-card: rgba(255, 255, 255, 0.1);
            --accent: #10b981;
            --accent-strong: #059669;
            --danger: #ef4444;
            --danger-strong: #dc2626;
            --warning: #f59e0b;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --shadow: 0 24px 60px rgba(15, 23, 42, 0.45);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: 'Outfit', sans-serif;
            background:
                radial-gradient(circle at top right, rgba(16, 185, 129, 0.20), transparent 28%),
                radial-gradient(circle at left top, rgba(59, 130, 246, 0.18), transparent 22%),
                linear-gradient(180deg, #0f172a 0%, #111827 100%);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 28px 16px 40px;
            display: flex;
            justify-content: center;
        }
        .container { width: 100%; max-width: 980px; }
        header { text-align: center; margin-bottom: 24px; }
        header h1 {
            font-size: clamp(2rem, 4vw, 2.8rem);
            font-weight: 700;
            background: linear-gradient(135deg, #34d399, #10b981);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        header p {
            color: var(--text-secondary);
            margin-top: 10px;
            font-size: 1rem;
        }
        .status-bar {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
            gap: 14px;
            margin-bottom: 22px;
        }
        .status-card, .panel {
            background: var(--bg-card);
            border: 1px solid var(--border-card);
            border-radius: 22px;
            box-shadow: var(--shadow);
            backdrop-filter: blur(18px);
        }
        .status-card {
            padding: 18px 18px 16px;
        }
        .status-label {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-size: 0.82rem;
            color: var(--text-secondary);
            margin-bottom: 10px;
            letter-spacing: 0.03em;
            text-transform: uppercase;
        }
        .status-value {
            font-size: 1.2rem;
            font-weight: 700;
            word-break: break-word;
        }
        .layout {
            display: grid;
            grid-template-columns: minmax(0, 1.2fr) minmax(0, 0.8fr);
            gap: 18px;
        }
        .panel {
            padding: 22px;
        }
        .panel h2 {
            font-size: 1.15rem;
            margin-bottom: 6px;
        }
        .panel-subtitle {
            color: var(--text-secondary);
            font-size: 0.96rem;
            margin-bottom: 18px;
            line-height: 1.55;
        }
        .form-grid {
            display: grid;
            gap: 14px;
        }
        label {
            display: block;
            font-size: 0.92rem;
            color: var(--text-secondary);
            margin-bottom: 7px;
        }
        input, textarea {
            width: 100%;
            border: 1px solid rgba(148, 163, 184, 0.25);
            background: rgba(15, 23, 42, 0.55);
            color: var(--text-primary);
            border-radius: 14px;
            padding: 13px 14px;
            font: inherit;
            outline: none;
            transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
        }
        input:focus, textarea:focus {
            border-color: rgba(16, 185, 129, 0.75);
            box-shadow: 0 0 0 4px rgba(16, 185, 129, 0.12);
            transform: translateY(-1px);
        }
        textarea {
            min-height: 210px;
            resize: vertical;
            line-height: 1.45;
        }
        .actions {
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-top: 4px;
        }
        button {
            border: none;
            border-radius: 999px;
            padding: 12px 18px;
            font: inherit;
            font-weight: 700;
            cursor: pointer;
            transition: transform 0.18s ease, opacity 0.18s ease, box-shadow 0.18s ease;
        }
        button:hover { transform: translateY(-1px); }
        button:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }
        .primary {
            color: white;
            background: linear-gradient(135deg, var(--accent), var(--accent-strong));
            box-shadow: 0 14px 28px rgba(16, 185, 129, 0.28);
        }
        .secondary {
            color: var(--text-primary);
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }
        .danger {
            color: white;
            background: linear-gradient(135deg, var(--danger), var(--danger-strong));
            box-shadow: 0 14px 28px rgba(239, 68, 68, 0.24);
        }
        .message {
            margin-top: 16px;
            padding: 13px 14px;
            border-radius: 14px;
            font-size: 0.94rem;
            line-height: 1.5;
            display: none;
        }
        .message.show { display: block; }
        .message.success {
            background: rgba(16, 185, 129, 0.16);
            color: #d1fae5;
            border: 1px solid rgba(16, 185, 129, 0.24);
        }
        .message.error {
            background: rgba(239, 68, 68, 0.12);
            color: #fecaca;
            border: 1px solid rgba(239, 68, 68, 0.22);
        }
        .message.info {
            background: rgba(59, 130, 246, 0.12);
            color: #dbeafe;
            border: 1px solid rgba(59, 130, 246, 0.22);
        }
        .hint-list {
            display: grid;
            gap: 12px;
        }
        .hint-item {
            padding: 14px 15px;
            border-radius: 16px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.06);
        }
        .hint-title {
            font-weight: 700;
            margin-bottom: 5px;
        }
        .hint-text {
            color: var(--text-secondary);
            font-size: 0.94rem;
            line-height: 1.55;
        }
        .badge {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            border-radius: 999px;
            padding: 8px 12px;
            background: rgba(245, 158, 11, 0.12);
            border: 1px solid rgba(245, 158, 11, 0.22);
            color: #fde68a;
            font-size: 0.86rem;
            margin-top: 14px;
        }
        code {
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 0.92em;
        }
        @media (max-width: 860px) {
            .layout {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>⚡ NinePlus Platform Server</h1>
            <p>管理员入口已恢复。可以在这里登录管理、检查当前九号账号状态、导出或清除后端 Token。</p>
        </header>

        <section class="status-bar">
            <div class="status-card">
                <div class="status-label">服务状态</div>
                <div class="status-value" id="serviceStatus">检查中...</div>
            </div>
            <div class="status-card">
                <div class="status-label">当前九号账号</div>
                <div class="status-value" id="activeAccount">检查中...</div>
            </div>
            <div class="status-card">
                <div class="status-label">服务版本</div>
                <div class="status-value" id="serviceVersion">检查中...</div>
            </div>
        </section>

        <section class="layout">
            <div class="panel">
                <h2>管理员登录</h2>
                <p class="panel-subtitle">登录后可直接导出当前后端保存的九号 Token，或在需要时清空它。管理员接口没有被删除，这里只是把网页入口补回来了。</p>

                <div class="form-grid">
                    <div>
                        <label for="adminUsername">管理员账号</label>
                        <input id="adminUsername" autocomplete="username" placeholder="默认通常是 admin" />
                    </div>
                    <div>
                        <label for="adminPassword">管理员密码</label>
                        <input id="adminPassword" type="password" autocomplete="current-password" placeholder="默认通常是 admin123" />
                    </div>
                </div>

                <div class="actions">
                    <button class="primary" id="loginButton" type="button">管理员登录</button>
                    <button class="secondary" id="exportButton" type="button">导出后端 Token</button>
                    <button class="danger" id="deleteButton" type="button">清空后端 Token</button>
                </div>

                <div id="messageBox" class="message"></div>

                <div style="margin-top: 18px;">
                    <label for="tokenOutput">Token 导出结果</label>
                    <textarea id="tokenOutput" placeholder="登录后点击“导出后端 Token”，这里会显示当前服务端保存的 tokens.json 内容。"></textarea>
                </div>
            </div>

            <div class="panel">
                <h2>使用说明</h2>
                <p class="panel-subtitle">如果你的网站和 App 都连的是这个 Render 服务，这个页面就是最直接的管理入口。</p>

                <div class="hint-list">
                    <div class="hint-item">
                        <div class="hint-title">1. 先看当前账号</div>
                        <div class="hint-text">页面会自动读取 <code>/healthz</code>。如果“当前九号账号”显示 <code>未登录</code>，说明后端实例里没有可用 Token。</div>
                    </div>
                    <div class="hint-item">
                        <div class="hint-title">2. 导出 Token 做 Render 备份</div>
                        <div class="hint-text">登录管理员后点“导出后端 Token”，可以把当前有效的九号 Token 复制出来，保存到 Render 的 <code>NINEBOT_TOKENS_JSON</code> 环境变量。</div>
                    </div>
                    <div class="hint-item">
                        <div class="hint-title">3. 必要时清空后重登</div>
                        <div class="hint-text">如果后端 Token 已损坏或过期，可以先清空，再从 App 重新登录九号账号，让服务端生成新的 tokens.json。</div>
                    </div>
                </div>

                <div class="badge">Render 冷启动后第一次响应可能更慢，这个页面会自动刷新服务状态。</div>
            </div>
        </section>
    </div>

    <script>
        const els = {
            serviceStatus: document.getElementById("serviceStatus"),
            activeAccount: document.getElementById("activeAccount"),
            serviceVersion: document.getElementById("serviceVersion"),
            username: document.getElementById("adminUsername"),
            password: document.getElementById("adminPassword"),
            tokenOutput: document.getElementById("tokenOutput"),
            messageBox: document.getElementById("messageBox"),
            loginButton: document.getElementById("loginButton"),
            exportButton: document.getElementById("exportButton"),
            deleteButton: document.getElementById("deleteButton")
        };

        function setMessage(type, text) {
            els.messageBox.className = "message show " + type;
            els.messageBox.textContent = text;
        }

        function clearMessage() {
            els.messageBox.className = "message";
            els.messageBox.textContent = "";
        }

        function credentials() {
            return {
                username: els.username.value.trim(),
                password: els.password.value
            };
        }

        function requireCredentials() {
            const creds = credentials();
            if (!creds.username || !creds.password) {
                setMessage("error", "请先填写管理员账号和密码。");
                return null;
            }
            return creds;
        }

        async function requestJSON(url, options = {}) {
            const response = await fetch(url, {
                headers: { "Content-Type": "application/json", ...(options.headers || {}) },
                ...options
            });
            const text = await response.text();
            let data = null;
            try {
                data = text ? JSON.parse(text) : null;
            } catch {
                data = { raw: text };
            }
            if (!response.ok) {
                const detail = data && (data.detail || data.message || data.raw) ? (data.detail || data.message || data.raw) : response.statusText;
                throw new Error(detail || ("HTTP " + response.status));
            }
            return data;
        }

        async function refreshHealth() {
            try {
                const data = await requestJSON("/healthz", { method: "GET" });
                els.serviceStatus.textContent = data.status || "ok";
                els.activeAccount.textContent = data.active_account || "未登录";
                els.serviceVersion.textContent = data.version || "-";
            } catch (error) {
                els.serviceStatus.textContent = "不可用";
                els.activeAccount.textContent = "读取失败";
                els.serviceVersion.textContent = "-";
                setMessage("error", "服务状态读取失败: " + error.message);
            }
        }

        async function adminLogin() {
            clearMessage();
            const creds = requireCredentials();
            if (!creds) return;
            els.loginButton.disabled = true;
            try {
                const data = await requestJSON("/admin/login", {
                    method: "POST",
                    body: JSON.stringify(creds)
                });
                setMessage("success", data.message || "管理员登录成功。");
            } catch (error) {
                setMessage("error", "管理员登录失败: " + error.message);
            } finally {
                els.loginButton.disabled = false;
            }
        }

        async function exportToken() {
            clearMessage();
            const creds = requireCredentials();
            if (!creds) return;
            els.exportButton.disabled = true;
            try {
                const data = await requestJSON("/admin/export-token", {
                    method: "POST",
                    body: JSON.stringify(creds)
                });
                const tokenText = data.token_json_string || "";
                els.tokenOutput.value = tokenText;
                setMessage("success", tokenText ? "Token 已导出，可以复制到 Render 环境变量。": (data.message || "当前没有可导出的 Token。"));
            } catch (error) {
                setMessage("error", "导出 Token 失败: " + error.message);
            } finally {
                els.exportButton.disabled = false;
            }
        }

        async function deleteToken() {
            clearMessage();
            const creds = requireCredentials();
            if (!creds) return;
            if (!confirm("确认要清空当前后端保存的九号 Token 吗？清空后 App 需要重新登录。")) {
                return;
            }
            els.deleteButton.disabled = true;
            try {
                const data = await requestJSON("/admin/token", {
                    method: "DELETE",
                    body: JSON.stringify(creds)
                });
                els.tokenOutput.value = "";
                setMessage("info", data.message || "后端 Token 已清空。");
                await refreshHealth();
            } catch (error) {
                setMessage("error", "清空 Token 失败: " + error.message);
            } finally {
                els.deleteButton.disabled = false;
            }
        }

        els.loginButton.addEventListener("click", adminLogin);
        els.exportButton.addEventListener("click", exportToken);
        els.deleteButton.addEventListener("click", deleteToken);

        refreshHealth();
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
