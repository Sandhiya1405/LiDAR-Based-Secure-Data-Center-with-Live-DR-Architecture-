from rplidar import RPLidar
import matplotlib.pyplot as plt
import numpy as np

PORT = 'COM3'

lidar = RPLidar(PORT, baudrate=115200)   # change here

plt.ion()
fig = plt.figure()
ax = fig.add_subplot(111, projection='polar')

try:
    print("Starting LiDAR...")

    for scan in lidar.iter_scans():
        angles = []
        distances = []

        for (_, angle, distance) in scan:
            angles.append(np.deg2rad(angle))
            distances.append(distance)

        ax.clear()
        ax.scatter(angles, distances, s=5)
        plt.pause(0.01)

except KeyboardInterrupt:
    print("Stopping")

finally:
    lidar.stop()
    lidar.disconnect()

