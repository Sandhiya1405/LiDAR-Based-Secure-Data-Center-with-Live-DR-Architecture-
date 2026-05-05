let temps = [];
let labels = [];

const ctx = document.getElementById('weatherChart').getContext('2d');

const chart = new Chart(ctx, {
    type: 'line',
    data: {
        labels: labels,
        datasets: [{
            label: 'Temperature Trend',
            data: temps
        }]
    }
});

async function updateDashboard() {
    const res = await fetch("/api/weather");
    const data = await res.json();

    document.getElementById("tempVal").innerText = data.temperature;
    document.getElementById("humVal").innerText = data.humidity;
    document.getElementById("pressVal").innerText = data.pressure;
    document.getElementById("windVal").innerText = data.wind;

    
    // Add data to chart
    temps.push(data.temperature);
    labels.push(new Date().toLocaleTimeString());

    if (temps.length > 10) {
        temps.shift();
        labels.shift();
    }

    chart.update();
}

setInterval(updateDashboard, 2000);
