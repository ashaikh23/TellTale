import time
import json
import logging
import os
import requests
from fastapi import FastAPI, Query, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from google.transit import gtfs_realtime_pb2
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("mta_app")

SCHEDULE_PATH = Path("schedule_store.json")

# Cache settings
MTA_CACHE_TTL = 30  # seconds - GTFS-RT best practice
WEATHER_CACHE_TTL = 600  # seconds (10 minutes)

# Cache storage
mta_cache = {}  # {feed_url: (timestamp, feed_data)}
weather_cache = {}  # {(lat, lon): (timestamp, weather_data)}

FEEDS = {
    "ACE":  "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-ace",
    "BDFM": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-bdfm",
    "G":    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-g",
    "JZ":   "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-jz",
    "NQRW": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-nqrw",
    "L":    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-l",
    "123456": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs",
    "7":    "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-7",
    "SIR":  "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-si",
}

app = FastAPI(title="MTA GTFS-RT Local Proxy")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def startup_event():
    logger.info("Startup: app initializing")
    logger.info("Dashboard exists: %s", Path("dashboard.html").exists())
    logger.info("Stops file exists: %s", Path("stops.txt").exists())
    logger.info("Schedule file exists: %s", SCHEDULE_PATH.exists())

@app.get("/", response_class=HTMLResponse)
def serve_dashboard():
    """Serve the dashboard HTML so it can call the API without file:// issues."""
    dashboard_path = Path("dashboard.html")
    if not dashboard_path.exists():
        logger.error("dashboard.html not found")
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    logger.info("Serving dashboard.html")
    return dashboard_path.read_text(encoding="utf-8")

@app.get("/stops")
def get_stops():
    """
    Load stop coordinates from GTFS stops.txt file.
    Returns: {stop_id: {lat, lon, name}}
    """
    stops = {}
    stops_file = Path("stops.txt")
    
    if not stops_file.exists():
        logger.warning("stops.txt not found")
        return {"error": "stops.txt not found"}
    
    try:
        with open(stops_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            # First line is header: stop_id,stop_code,stop_name,stop_desc,stop_lat,stop_lon,...
            header = lines[0].strip().split(',')
            
            # Find column indices
            id_idx = header.index('stop_id')
            name_idx = header.index('stop_name')
            lat_idx = header.index('stop_lat')
            lon_idx = header.index('stop_lon')
            
            for line in lines[1:]:
                parts = line.strip().split(',')
                if len(parts) > max(id_idx, name_idx, lat_idx, lon_idx):
                    stop_id = parts[id_idx]
                    stop_name = parts[name_idx]
                    lat = float(parts[lat_idx])
                    lon = float(parts[lon_idx])
                    
                    stops[stop_id] = {
                        "lat": lat,
                        "lon": lon,
                        "name": stop_name
                    }
        
        logger.info("Loaded %s stops", len(stops))
        return {"stops": stops}
    except Exception as e:
        logger.exception("Failed loading stops.txt")
        return {"error": str(e)}

def fetch_feed(feed_url: str):
    # Check cache first
    now = time.time()
    if feed_url in mta_cache:
        cached_time, cached_feed = mta_cache[feed_url]
        if now - cached_time < MTA_CACHE_TTL:
            logger.debug("MTA cache hit for %s", feed_url)
            return cached_feed
    
    # Cache miss or expired - fetch fresh data
    logger.info("Fetching MTA feed: %s", feed_url)
    r = requests.get(feed_url, timeout=20)
    r.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(r.content)
    
    # Store in cache
    mta_cache[feed_url] = (now, feed)
    
    return feed

DAY_MAP = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}

def load_schedule():
    if not SCHEDULE_PATH.exists():
        logger.warning("Schedule file missing: %s", SCHEDULE_PATH)
        return None
    try:
        data = json.loads(SCHEDULE_PATH.read_text())
        logger.info("Loaded schedule with %s classes", len(data.get("classes", [])))
        return data
    except Exception:
        logger.exception("Failed to load schedule.json")
        return None

def next_class_event(schedule):
    tz = schedule.get("timezone", "America/New_York")
    now = datetime.now(ZoneInfo(tz))
    today = now.weekday()

    upcoming = []
    for c in schedule["classes"]:
        if today not in [DAY_MAP[d] for d in c["days"]]:
            continue
        hh, mm = map(int, c["start"].split(":"))
        start_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if start_dt <= now:
            continue
        upcoming.append((start_dt, c))

    if not upcoming:
        return None, now

    upcoming.sort(key=lambda x: x[0])
    return upcoming[0], now

def fetch_weather(lat: float, lon: float):
    """
    Fetch hourly weather forecast from Open-Meteo with caching.
    Returns current hour weather data or None on error.
    """
    # Check cache first
    cache_key = (lat, lon)
    now_time = time.time()
    if cache_key in weather_cache:
        cached_time, cached_data = weather_cache[cache_key]
        if now_time - cached_time < WEATHER_CACHE_TTL:
            logger.debug("Weather cache hit for %s", cache_key)
            return cached_data
    
    # Cache miss or expired - fetch fresh data
    try:
        logger.info("Fetching weather for lat=%s lon=%s", lat, lon)
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,precipitation_probability,snowfall",
            "temperature_unit": "fahrenheit",
            "timezone": "America/New_York",
            "forecast_days": 1
        }
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # Get current hour index (0 = midnight, 1 = 1am, etc.)
        now = datetime.now(ZoneInfo("America/New_York"))
        current_hour = now.hour
        
        hourly = data.get("hourly", {})
        temps = hourly.get("temperature_2m", [])
        precip_probs = hourly.get("precipitation_probability", [])
        snowfalls = hourly.get("snowfall", [])
        
        if current_hour < len(temps):
            weather_data = {
                "temperature": temps[current_hour],
                "precipitation_probability": precip_probs[current_hour] if current_hour < len(precip_probs) else 0,
                "snowfall": snowfalls[current_hour] if current_hour < len(snowfalls) else 0
            }
            # Store in cache
            weather_cache[cache_key] = (now_time, weather_data)
            return weather_data
        logger.warning("Weather data unavailable for current hour")
        return None
    except Exception as e:
        logger.exception("Weather fetch failed")
        return None

def get_weather_badges(weather_data):
    """
    Convert weather data to simple badges.
    """
    if not weather_data:
        return ["SUN"]
    
    badges = []
    
    # Snowfall check
    if weather_data.get("snowfall", 0) > 0:
        badges.append("SNOW")
    
    # Rain check (40% threshold)
    if weather_data.get("precipitation_probability", 0) >= 40:
        badges.append("RAIN")
    
    # Cold check (35°F threshold)
    if weather_data.get("temperature", 100) <= 35:
        badges.append("COLD")
    
    # Default to SUN if no weather alerts
    if not badges:
        badges.append("SUN")
    
    return badges

def get_weather_summary(weather_data):
    """
    Generate a short weather summary string.
    """
    if not weather_data:
        return "Weather unavailable"
    
    temp = weather_data.get("temperature")
    precip = weather_data.get("precipitation_probability", 0)
    snow = weather_data.get("snowfall", 0)
    
    parts = []
    if temp is not None:
        parts.append(f"{int(temp)}°F")
    
    if snow > 0:
        parts.append(f"snow likely")
    elif precip >= 40:
        parts.append(f"{int(precip)}% rain chance")
    
    return ", ".join(parts) if parts else "Clear"

@app.get("/arrivals")
def arrivals(feed: str = Query("BDFM"), stop: str = Query(...), route: str | None = Query(None), limit: int = Query(6, ge=1, le=20)):
    logger.info("Arrivals request feed=%s stop=%s route=%s limit=%s", feed, stop, route, limit)
    feed_url = FEEDS.get(feed)
    if not feed_url:
        logger.warning("Unknown feed requested: %s", feed)
        return {"error": f"Unknown feed {feed}", "feeds": sorted(FEEDS.keys())}
    
    now = int(time.time())
    
    try:
        fm = fetch_feed(feed_url)
    except Exception as e:
        logger.exception("Feed fetch error")
        return {"status": "feed_error", "error": str(e), "feed": feed, "stop_id": stop}
    
    # Check feed freshness
    feed_timestamp = fm.header.timestamp if fm.header.HasField("timestamp") else None
    if not feed_timestamp:
        logger.warning("Feed missing timestamp for %s", feed)
        return {"status": "no_timestamp", "feed": feed, "stop_id": stop, "warning": "Feed has no timestamp"}
    
    feed_age_sec = now - feed_timestamp
    is_stale = feed_age_sec > 120
    
    hits = []
    for ent in fm.entity:
        if not ent.HasField("trip_update"): continue
        tu = ent.trip_update
        # Don't hardcode expected routes - use whatever route_id is present
        route_id = tu.trip.route_id if (tu.trip and tu.trip.route_id) else None
        trip_id = tu.trip.trip_id if (tu.trip and tu.trip.trip_id) else None
        if route and route_id and route_id != route: continue
        for stu in tu.stop_time_update:
            if stu.stop_id != stop: continue
            t = None
            if stu.HasField("arrival") and stu.arrival.time: t = int(stu.arrival.time)
            elif stu.HasField("departure") and stu.departure.time: t = int(stu.departure.time)
            if t and t >= now:
                hits.append({"t_epoch": t, "in_min": (t - now) // 60, "route_id": route_id, "trip_id": trip_id})
    
    hits.sort(key=lambda x: x["t_epoch"])
    logger.info("Arrivals found: %s", len(hits))
    
    result = {
        "status": "stale_feed" if is_stale else "ok",
        "feed": feed,
        "stop_id": stop,
        "filter_route": route,
        "updated_epoch": now,
        "feed_timestamp": feed_timestamp,
        "feed_age_sec": feed_age_sec,
        "arrivals": hits[:limit]
    }
    
    if is_stale:
        result["warning"] = f"Feed data is {feed_age_sec}s old (threshold: 120s). Data may be outdated."
    
    return result

@app.post("/schedule/upload")
async def upload_schedule(file: UploadFile = File(...)):
    if not file.filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="Upload a .json file")

    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if "classes" not in data or "home" not in data:
        raise HTTPException(status_code=400, detail="JSON must include 'home' and 'classes'")

    SCHEDULE_PATH.write_text(json.dumps(data, indent=2))
    return {"status": "ok", "saved_as": str(SCHEDULE_PATH), "classes": len(data["classes"])}

@app.get("/schedule")
def get_schedule():
    if not SCHEDULE_PATH.exists():
        return {"status": "empty"}
    return json.loads(SCHEDULE_PATH.read_text())

@app.get("/next_class")
def next_class():
    schedule = load_schedule()
    if not schedule:
        return {"status": "no_schedule"}

    nxt, now = next_class_event(schedule)
    if not nxt:
        return {"status": "all_done", "message": "You're all done with classes for today. You're now free! 🎉"}

    start_dt, c = nxt
    arrive_early = c.get("arrive_early_min", schedule.get("defaults", {}).get("arrive_early_min", 10))
    arrive_by = start_dt - timedelta(minutes=arrive_early)

    return {
        "status": "ok",
        "now_local": now.isoformat(),
        "class": {
            "name": c["name"],
            "start_local": start_dt.isoformat(),
            "arrive_by_local": arrive_by.isoformat(),
            "location_name": c.get("location_name"),
            "destination": c.get("destination"),
        }
    }

@app.get("/leave_for_next_class")
def leave_for_next_class():
    schedule = load_schedule()
    if not schedule:
        return {"status": "no_schedule"}

    nxt, now = next_class_event(schedule)
    if not nxt:
        return {"status": "all_done", "message": "You're all done with classes for today. You're now free! 🎉"}

    start_dt, c = nxt
    arrive_early = c.get("arrive_early_min", schedule.get("defaults", {}).get("arrive_early_min", 10))
    arrive_by = start_dt - timedelta(minutes=arrive_early)

    home = schedule["home"]["origin"]
    
    data = arrivals(feed=home["feed"], stop=home["stop_id"], route=None, limit=6)

    # Handle feed errors or stale data
    if data.get("status") == "feed_error":
        return {
            "status": "feed_error",
            "error": data.get("error"),
            "next_class": c["name"],
            "message": "Cannot fetch real-time data. Check MTA feed status."
        }
    
    if data.get("status") == "stale_feed":
        return {
            "status": "stale_feed",
            "warning": data.get("warning"),
            "feed_age_sec": data.get("feed_age_sec"),
            "next_class": c["name"],
            "message": "Real-time data is outdated. Arrival predictions may be inaccurate."
        }

    if not data.get("arrivals"):
        return {
            "status": "no_arrivals",
            "next_class": c["name"],
            "feed_age_sec": data.get("feed_age_sec", 0),
            "message": "No upcoming trains found at your stop."
        }

    next_train = data["arrivals"][0]
    walk_to = schedule["home"]["origin"].get("walk_to_stop_min", 7)
    buffer = schedule.get("defaults", {}).get("platform_buffer_min", 2)

    leave_in = int(next_train["in_min"]) - walk_to - buffer

    return {
        "status": "ok",
        "class": c["name"],
        "class_start_local": start_dt.isoformat(),
        "arrive_by_local": arrive_by.isoformat(),
        "origin_stop": home["stop_id"],
        "next_train": next_train,
        "leave_in_min": leave_in,
        "feed_age_sec": data.get("feed_age_sec", 0)
    }

@app.get("/decision")
def decision():
    """
    Unified endpoint for the Feather display.
    Returns everything needed: next class, next train, when to leave, weather.
    """
    now = int(time.time())
    
    # Load schedule
    logger.info("Decision request")
    schedule = load_schedule()
    if not schedule:
        logger.warning("No schedule available")
        return {"status": "no_schedule", "now_epoch": now}

    # Find next class
    nxt, now_dt = next_class_event(schedule)
    if not nxt:
        logger.info("All done for today")
        # Fetch weather even when all done
        weather_data = fetch_weather(40.7614, -73.9509)
        weather_badges = get_weather_badges(weather_data)
        weather_summary = get_weather_summary(weather_data)
        
        return {
            "status": "all_done",
            "now_epoch": now,
            "message": "You're all done with classes for today. You're now free! 🎉",
            "weather_badges": weather_badges,
            "weather_summary": weather_summary,
            "feed_age_sec": 0
        }

    start_dt, c = nxt
    arrive_early = c.get("arrive_early_min", schedule.get("defaults", {}).get("arrive_early_min", 10))
    arrive_by = start_dt - timedelta(minutes=arrive_early)

    # Get origin and destination info
    home = schedule["home"]["origin"]
    dest = c.get("destination", {})
    
    # Fetch weather (Roosevelt Island coordinates: 40.7614, -73.9509)
    weather_data = fetch_weather(40.7614, -73.9509)
    weather_badges = get_weather_badges(weather_data)
    weather_summary = get_weather_summary(weather_data)
    
    # Get transit data for DESTINATION stop (where you're going)
    if not dest or not dest.get("feed") or not dest.get("stop_id"):
        return {
            "status": "error",
            "now_epoch": now,
            "next_class": {
                "name": c["name"],
                "start_local": start_dt.isoformat(),
                "arrive_by_local": arrive_by.isoformat(),
                "location_name": c.get("location_name")
            },
            "error": "Class missing destination info",
            "weather_badges": weather_badges,
            "weather_summary": weather_summary
        }
    
    transit_data = arrivals(feed=dest["feed"], stop=dest["stop_id"], route=None, limit=6)

    # Handle stale or error feeds
    feed_status = transit_data.get("status", "ok")
    feed_age = transit_data.get("feed_age_sec", 0)
    logger.info("Transit status=%s feed_age=%s", feed_status, feed_age)
    
    if feed_status == "feed_error":
        return {
            "status": "feed_error",
            "now_epoch": now,
            "next_class": {
                "name": c["name"],
                "start_local": start_dt.isoformat(),
                "arrive_by_local": arrive_by.isoformat(),
                "location_name": c.get("location_name")
            },
            "error": transit_data.get("error", "Cannot fetch real-time data"),
            "weather_badges": weather_badges,
            "weather_summary": weather_summary
        }

    # Calculate leave time with FULL journey
    next_train = None
    leave_in_min = None
    
    if transit_data.get("arrivals"):
        # Find first train that gets you there on time
        walk_to_origin = home.get("walk_to_stop_min", 7)
        walk_from_dest = dest.get("walk_from_stop_min", 5)
        buffer = schedule.get("defaults", {}).get("platform_buffer_min", 2)
        
        arrive_by_epoch = int(arrive_by.timestamp())
        
        for arrival in transit_data["arrivals"]:
            train_arrives_at_dest = arrival["t_epoch"]
            # Add walk time from subway to classroom
            arrival_at_class = train_arrives_at_dest + (walk_from_dest * 60)
            
            # Check if this train gets you there on time
            if arrival_at_class <= arrive_by_epoch:
                next_train = arrival
                # Calculate when to leave home
                # Leave time = when train arrives at destination - walk from dest - train ride - walk to origin - buffer
                # Simpler: Leave time = now + (train arrival in minutes - walk to origin - buffer)
                leave_in_min = int(arrival["in_min"]) - walk_to_origin - buffer
                break
        
        # If no train gets you there on time, show the next train anyway with warning
        if not next_train and transit_data.get("arrivals"):
            next_train = transit_data["arrivals"][0]
            leave_in_min = int(next_train["in_min"]) - walk_to_origin - buffer

    # Build response
    response = {
        "status": "stale_feed" if feed_status == "stale_feed" else "ok",
        "now_epoch": now,
        "next_class": {
            "name": c["name"],
            "start_local": start_dt.isoformat(),
            "arrive_by_local": arrive_by.isoformat(),
            "location_name": c.get("location_name")
        },
        "origin": {
            "feed": home["feed"],
            "stop_id": home["stop_id"],
            "walk_min": home.get("walk_to_stop_min", 7)
        },
        "destination": {
            "feed": dest["feed"],
            "stop_id": dest["stop_id"],
            "walk_min": dest.get("walk_from_stop_min", 5)
        },
        "feed_age_sec": feed_age,
        "weather_badges": weather_badges,
        "weather_summary": weather_summary
    }

    if next_train:
        response["next_train"] = {
            "route_id": next_train.get("route_id"),
            "in_min": next_train["in_min"],
            "t_epoch": next_train["t_epoch"],
            "arrives_at_dest_epoch": next_train["t_epoch"]  # When train arrives at destination
        }
        response["leave_in_min"] = leave_in_min
        
        # Calculate total journey time
        walk_from_dest = dest.get("walk_from_stop_min", 5)
        walk_to_origin = home.get("walk_to_stop_min", 7)
        
        # Time from now until train arrives at destination
        now_epoch = int(time.time())
        train_arrives_in_min = (next_train["t_epoch"] - now_epoch) // 60
        
        # Total time to get to class = walk to origin + wait for train + ride + walk to class
        # But we can simplify: time until train arrives at dest + walk from dest
        total_journey_min = train_arrives_in_min + walk_from_dest
        
        response["journey_breakdown"] = {
            "walk_to_station_min": walk_to_origin,
            "train_arrives_at_dest_in_min": train_arrives_in_min,
            "walk_to_class_min": walk_from_dest,
            "total_journey_min": total_journey_min
        }
        
        # Check if train gets you there on time
        arrive_by_epoch = int(arrive_by.timestamp())
        arrival_at_class = next_train["t_epoch"] + (walk_from_dest * 60)
        
        if arrival_at_class > arrive_by_epoch:
            mins_late = (arrival_at_class - arrive_by_epoch) // 60
            response["warning"] = f"This train will make you {mins_late} min late! Leave NOW or skip."
    else:
        response["next_train"] = None
        response["leave_in_min"] = None
        response["journey_breakdown"] = None
        response["warning"] = "No upcoming trains found"

    if feed_status == "stale_feed" and "warning" not in response:
        response["warning"] = transit_data.get("warning", "Feed data may be outdated")

    return response
