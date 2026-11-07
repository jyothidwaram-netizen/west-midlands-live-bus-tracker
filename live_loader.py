import os
import time
import xml.etree.ElementTree as ET
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATA_FOLDER = os.path.expanduser("~/Desktop/bus_collector/bus_data")
NS = {"s": "http://www.siri.org.uk/siri"}

conn = psycopg2.connect(
    host="localhost", port=5432, dbname="westmidlands_bus",
    user="postgres", password=os.getenv("DB_PASSWORD")
)
cur = conn.cursor()

# Files older than 15 minutes count as "already handled", so we only load fresh ones.
done = set()
cutoff = time.time() - 15 * 60
for f in os.listdir(DATA_FOLDER):
    if f.endswith(".xml") and os.path.getmtime(os.path.join(DATA_FOLDER, f)) < cutoff:
        done.add(f)

def load_file(path):
    root = ET.parse(path).getroot()
    rows = []
    for va in root.findall(".//s:VehicleActivity", NS):
        recorded_at = va.find("s:RecordedAtTime", NS)
        mvj = va.find("s:MonitoredVehicleJourney", NS)
        if mvj is None or recorded_at is None:
            continue
        loc = mvj.find("s:VehicleLocation", NS)
        if loc is None:
            continue
        lon_el = loc.find("s:Longitude", NS)
        lat_el = loc.find("s:Latitude", NS)
        if lon_el is None or lat_el is None:
            continue
        line = mvj.find("s:PublishedLineName", NS)
        veh = mvj.find("s:VehicleRef", NS)
        origin = mvj.find("s:OriginName", NS)
        dest = mvj.find("s:DestinationName", NS)
        rows.append((
            line.text if line is not None else None,
            veh.text if veh is not None else None,
            float(lat_el.text), float(lon_el.text), recorded_at.text,
            origin.text if origin is not None else None,
            dest.text if dest is not None else None,
        ))
    return rows

print("Live loader started. Press Ctrl+C to stop.")
while True:
    added = 0
    for filename in sorted(os.listdir(DATA_FOLDER)):
        if not filename.endswith(".xml") or filename in done:
            continue
        try:
            rows = load_file(os.path.join(DATA_FOLDER, filename))
        except ET.ParseError:
            continue  # file may still be being written; try again next round
        if rows:
            cur.executemany("""
                INSERT INTO vehicle_positions
                (line_name, vehicle_ref, latitude, longitude, recorded_at, origin_name, destination_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, rows)
        conn.commit()
        done.add(filename)
        added += len(rows)
    # keep the table small: drop positions older than one day
    cur.execute("DELETE FROM vehicle_positions WHERE recorded_at < now() - interval '1 day'")
    conn.commit()
    print(f"Loaded {added} new positions")
    time.sleep(15)