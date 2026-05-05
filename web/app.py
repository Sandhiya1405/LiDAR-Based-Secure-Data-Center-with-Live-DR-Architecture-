from flask import Flask, jsonify, request, render_template
import requests
from requests.exceptions import RequestException
import psycopg2
from psycopg2 import OperationalError

# ML imports
import pandas as pd
from prophet import Prophet
from datetime import datetime, timedelta
import time

# Supported capitals and their coordinates
capitals = {
    "Malaysia": (3.1390, 101.6869),
    "Vietnam": (10.8231, 106.6297),
    "Thailand": (13.7563, 100.5018),
    "India": (28.6139, 77.2090),
    "Italy": (41.9028, 12.4964),
    "Singapore": (1.3521, 103.8198),
    "Cambodia": (11.5564, 104.9282),
    "Cyprus": (35.1856, 33.3823),
    "Qatar": (25.276987, 51.520008),
    "Tanzania": (-6.7924, 39.2083),
    "Malta": (35.8989, 14.5146),
    "Spain": (41.3874, 2.1686),
    "Greece": (37.9838, 23.7275),
    "Indonesia": (-6.2088, 106.8456),
    "Oman": (23.5880, 58.3829),
    "Brunei": (4.9031, 114.9398),
    "Peru": (-12.0464, -77.0428),
    "Chile": (-33.4489, -70.6693),
    "London": (51.5074, -0.1278),
    "New York": (40.7128, -74.0060),
    "Tokyo": (35.6895, 139.6917),
    "Mumbai": (19.0760, 72.8777),
    "Paris": (48.8566, 2.3522),
    "Berlin": (52.5200, 13.4050),
    "Sydney": (-33.8688, 151.2093),
    "Moscow": (55.7558, 37.6173)
}

# Helper functions for ML
def fetch_weather(lat, lon):
    end = datetime.today()
    start = end - timedelta(days=730)

    url = (
        f"https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={lat}&longitude={lon}"
        f"&start_date={start.date()}&end_date={end.date()}"
        f"&daily=temperature_2m_max,precipitation_sum,windspeed_10m_max"
        f"&timezone=auto"
    )

    data = requests.get(url, timeout=10).json()

    df = pd.DataFrame({
        "ds": pd.to_datetime(data['daily']['time']),
        "y": data['daily']['temperature_2m_max'],
        "rainfall": data['daily']['precipitation_sum'],
        "wind_speed": data['daily']['windspeed_10m_max']
    })

    return df


def preprocess(df):
    df['y'] = df['y'].ffill()
    df['rainfall'] = df['rainfall'].fillna(0)
    df['wind_speed'] = df['wind_speed'].fillna(df['wind_speed'].mean())

    df['rolling_mean_7'] = df['y'].rolling(7).mean().bfill()
    df['lag_1'] = df['y'].shift(1).bfill()
    df['lag_2'] = df['y'].shift(2).bfill()
    df['lag_3'] = df['y'].shift(3).bfill()

    return df.dropna()


app = Flask(__name__)

API_KEY = "ac9518e6565290318151181a101b0fb3"

# --- UPDATED FAILOVER LOGIC ---
def get_db_connection():
    hosts = ["10.110.173.246", "10.110.173.162"]

    for attempt in range(3):  # retry quickly if failover is happening
        for host in hosts:
            try:
                conn = psycopg2.connect(
                    dbname="weather_db",
                    user="postgres",
                    password="newpassword123",
                    host=host,
                    port="5432",
                    connect_timeout=2
                )

                cursor = conn.cursor()
                cursor.execute("SELECT pg_is_in_recovery();")
                is_standby = cursor.fetchone()[0]
                cursor.close()

                if not is_standby:
                    print(f"Connected to PRIMARY node: {host}")
                    return conn
                else:
                    print(f"{host} is STANDBY, skipping...")
                    conn.close()

            except OperationalError:
                print(f"Node {host} unreachable")

        # wait briefly before retrying (failover window)
        time.sleep(1)

    return None

# Database initialization
def init_db():
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS weather_data (
                    id SERIAL PRIMARY KEY,
                    city TEXT,
                    temperature FLOAT,
                    humidity FLOAT,
                    pressure FLOAT,
                    wind FLOAT,
                    condition TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            cursor.close()
            conn.close()
            print("Database structure verified.")
        except Exception as e:
            print("Init DB error:", e)

init_db()

@app.route("/")
def home():
    return render_template("index.html")

CURRENT_CITY = "London"

@app.route("/api/weather")
def get_weather():
    global CURRENT_CITY
    print("Requested city:", request.args.get("city"))
    
    conn = get_db_connection()
    if not conn:
        return jsonify({
            "city": CURRENT_CITY,
            "temp": None,
            "humidity": None,
            "pressure": None,
            "wind": None,
            "condition": "Failover in progress"
        })

    try:
        city_param = request.args.get("city")
        if city_param and city_param != CURRENT_CITY:
            CURRENT_CITY = city_param
        city = CURRENT_CITY
        
        # URL is correct; adding a timeout to prevent hanging on network failures
        url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={API_KEY}&units=metric"
        response = requests.get(url, timeout=5).json()

        if response.get("cod") != 200:
            return jsonify({"error": "City not found"})
            
        temp = response["main"]["temp"]
        humidity = response["main"]["humidity"]
        pressure = response["main"]["pressure"]
        wind = response["wind"]["speed"]
        condition = response["weather"][0]["main"]

        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO weather_data 
            (city, temperature, humidity, pressure, wind, condition)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (city, temp, humidity, pressure, wind, condition))

        conn.commit()
        cursor.close()
        conn.close()

        return jsonify({
            "city": city,
            "temp": temp,
            "humidity": humidity,
            "pressure": pressure,
            "wind": wind,
            "condition": condition
        })
        
    except RequestException as e:
        if conn: conn.close()
        print("Network Error while fetching weather:", e)
        return jsonify({"error": "Network connection to API failed"}), 503
    except Exception as e:
        if conn: conn.close()
        print("Weather API error:", e)
        return jsonify({"error": "Server error"}), 500


@app.route("/api/hourly-forecast")
def get_hourly_forecast():
    try:
        city = request.args.get("city", "London")

        url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={API_KEY}&units=metric"
        response = requests.get(url, timeout=5).json()

        if str(response.get("cod")) != "200":
            return jsonify({"error": "City not found"})

        forecasts = response.get("list", [])[:8]

        hourly_data = []
        for f in forecasts:
            hour = datetime.fromtimestamp(f["dt"]).strftime("%H:%M")

            hourly_data.append({
                "time": hour,
                "temp": f["main"]["temp"],
                "condition": f["weather"][0]["main"]
            })

        return jsonify({"city": city, "hourly": hourly_data})
        
    except RequestException as e:
        print("Network Error while fetching hourly forecast:", e)
        return jsonify({"error": "Network connection to API failed"}), 503
    except Exception as e:
        print("Hourly forecast error:", e)
        return jsonify({"error": "Server error"}), 500


@app.route("/api/daily-forecast")
def get_daily_forecast():
    try:
        city = request.args.get("city", "London")

        url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={API_KEY}&units=metric"
        response = requests.get(url, timeout=5).json()

        if str(response.get("cod")) != "200":
            return jsonify({"error": "City not found"})

        from collections import Counter
        daily = {}

        for f in response["list"]:
            date = datetime.fromtimestamp(f["dt"]).strftime("%Y-%m-%d")

            if date not in daily:
                daily[date] = {"temps": [], "conditions": []}

            daily[date]["temps"].append(f["main"]["temp"])
            daily[date]["conditions"].append(f["weather"][0]["main"])

        formatted = []
        for date, data in sorted(daily.items())[:10]:
            formatted.append({
                "date": date,
                "day": datetime.strptime(date, "%Y-%m-%d").strftime("%a"),
                "high": round(max(data["temps"]), 1),
                "low": round(min(data["temps"]), 1),
                "condition": Counter(data["conditions"]).most_common(1)[0][0]
            })

        return jsonify({"city": city, "forecast": formatted})

    except RequestException as e:
        print("Network Error while fetching daily forecast:", e)
        return jsonify({"error": "Network connection to API failed"}), 503
    except Exception as e:
        print("Daily forecast error:", e)
        return jsonify({"error": "Server error"}), 500


# ML forecast endpoint (PURE ML — NO FUTURE WEATHER API)
@app.route("/api/ml-forecast")
def ml_forecast():
    country = request.args.get("country")

    if not country or country not in capitals:
        return jsonify({"error": "Location not supported"}), 400

    lat, lon = capitals[country]

    try:
        raw = fetch_weather(lat, lon)
        clean = preprocess(raw)

        model = Prophet(
            yearly_seasonality=True,
            weekly_seasonality=True,
            daily_seasonality=False,
            changepoint_prior_scale=0.2
        )

        model.add_regressor('rainfall')
        model.add_regressor('wind_speed')
        model.add_regressor('rolling_mean_7')
        model.add_regressor('lag_1')
        model.add_regressor('lag_2')
        model.add_regressor('lag_3')

        model.fit(clean)

        future = model.make_future_dataframe(periods=5)

        future['rainfall'] = clean['rainfall'].iloc[-1]
        future['wind_speed'] = clean['wind_speed'].iloc[-1]
        future['rolling_mean_7'] = clean['rolling_mean_7'].iloc[-1]
        future['lag_1'] = clean['y'].iloc[-1]
        future['lag_2'] = clean['y'].iloc[-2]
        future['lag_3'] = clean['y'].iloc[-3]

        forecast = model.predict(future)

        result = forecast[['ds', 'yhat']].tail(5)
        result.columns = ['date', 'predicted_temp']

        return jsonify({
            "country": country,
            "forecast": result.to_dict(orient='records')
        })

    except Exception as e:
        print("ML forecast error:", e)
        return jsonify({"error": "ML forecast failed"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
    
