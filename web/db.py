import psycopg2
from psycopg2 import OperationalError

def get_db_connection():
    # Replace these with your actual Laptop IPs
    # Laptop 1 (LIVE): 10.110.173.246
    # Laptop 2 (DR):   10.110.173.162
    db_nodes = ["10.110.173.246", "10.110.173.162"]
    
    conn = None
    for ip in db_nodes:
        try:
            print(f"Attempting to connect to Data Center at {ip}...")
            conn = psycopg2.connect(
                host=ip,
                database="weather_db",
                user="postgres",
                password="newpassword123",
                connect_timeout=3  # Stop waiting after 3 seconds to trigger failover faster
            )
            print(f"✅ Connected successfully to {ip}!")
            return conn
        except OperationalError:
            print(f"❌ Data Center at {ip} is DOWN. Moving to next node...")
            continue
    
    if conn is None:
        print("🚨 CRITICAL: All Data Centers (LIVE & DR) are offline!")
        return None

# Initial setup (only runs once)
connection = get_db_connection()
if connection:
    cur = connection.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS weather_data (
        id SERIAL PRIMARY KEY,
        city VARCHAR(50),
        temperature FLOAT,
        description TEXT
    )
    """)
    connection.commit()
    print("Database ready!")
    connection.close()