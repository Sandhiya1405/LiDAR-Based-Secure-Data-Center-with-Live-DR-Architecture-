<script>

let currentCity = "London";

let temps = [];
let labels = [];

const ctx = document.getElementById("chart").getContext("2d");

const chart = new Chart(ctx, {
    type: "line",
    data: {
        labels: labels,
        datasets: [{
            label: "Temperature",
            data: temps,
            borderColor: "white",
            fill: false
        }]
    }
});

async function fetchWeather(city) {

    console.log("Fetching weather for:", city); // DEBUG LINE

    const res = await fetch(`/api/weather?city=${city}`);
    const data = await res.json();

    document.getElementById("cityName").innerText = data.city;
    document.getElementById("temp").innerText = data.temp + " °C";
    document.getElementById("condition").innerText = data.condition;

    temps.push(data.temp);
    labels.push(new Date().toLocaleTimeString());

    if (temps.length > 10) {
        temps.shift();
        labels.shift();
    }

    chart.update();
}

// First load
fetchWeather(currentCity);

// Repeat every 2 seconds
setInterval(() => {
    fetchWeather(currentCity);
}, 2000);

// City change
document.getElementById("cityInput").addEventListener("change", e => {
    currentCity = e.target.value;
    fetchWeather(currentCity);
});

</script>
