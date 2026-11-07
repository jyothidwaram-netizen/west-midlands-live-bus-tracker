import requests
import time
import os
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BODS_API_KEY")
BOUNDING_BOX = "-2.25,52.30,-1.45,52.70"

URL = f"https://data.bus-data.dft.gov.uk/api/v1/datafeed?boundingBox={BOUNDING_BOX}&api_key={API_KEY}"

SAVE_FOLDER = "bus_data"
os.makedirs(SAVE_FOLDER, exist_ok=True)

while True:
    try:
        response = requests.get(URL, timeout=15)
        if response.status_code == 200:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            filename = os.path.join(SAVE_FOLDER, f"positions_{timestamp}.xml")
            with open(filename, "w") as f:
                f.write(response.text)
            print(f"Saved: {filename}")
        else:
            print(f"Error: got status code {response.status_code}")
    except Exception as e:
        print(f"Something went wrong: {e}")

    time.sleep(25)
