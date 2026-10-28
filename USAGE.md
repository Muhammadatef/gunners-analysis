> [!WARNING]
> **This document is out of date.** It describes the pre-Airflow, pre-warehouse
> workflow and references files that no longer exist (`run.sh`,
> `test_arsenal_match.py`, `visualizer/visualize.py`). Commands here may fail.
>
> Current documentation: [README.md](README.md) ·
> [docs/AUTOMATION.md](docs/AUTOMATION.md) ·
> [docs/DATABASE.md](docs/DATABASE.md) ·
> [docs/VISUALS.md](docs/VISUALS.md) ·
> [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md)

# 📖 Usage Guide - Football Analytics Pipeline

## Quick Start (5 minutes)

### 1. Start the System

```bash
./run.sh
```

This will:
- Build all Docker images
- Start all services
- Check health status
- Display API URLs

### 2. Test with Arsenal Match

```bash
python3 test_arsenal_match.py
```

This will:
- Scrape Crystal Palace vs Arsenal match from Whoscored
- Transform the data
- Validate quality
- Generate visualizations

### 3. View Results

Check the generated files:
```bash
ls -lh data/visualizations/*/
```

## Detailed Usage

### Method 1: Using the Test Script (Easiest)

1. **Edit the test script** with your match details:
```python
# Edit test_arsenal_match.py
MATCH_CONFIG = {
    "match_url": "https://www.whoscored.com/matches/YOUR_MATCH_ID/...",
    "date": "2026-08-15",
    "league": "Premier League",
    "gameweek": 1,
    "home_xg": 1.2,  # Get from Fotmob
    "away_xg": 2.5,
    "home_xgot": 1.0,
    "away_xgot": 2.1
}
```

2. **Run the script**:
```bash
python3 test_arsenal_match.py
```

### Method 2: Using cURL

```bash
curl -X POST http://localhost:8000/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{
    "match_url": "https://www.whoscored.com/matches/1903353/live/england-premier-league-2025-2026-crystal-palace-arsenal",
    "date": "2026-08-15",
    "league": "Premier League",
    "gameweek": 1,
    "home_xg": 1.2,
    "away_xg": 2.5,
    "home_xgot": 1.0,
    "away_xgot": 2.1,
    "home_color": "#1B458F",
    "away_color": "#EF0107"
  }'
```

Save the `job_id` from the response, then monitor:
```bash
# Replace JOB_ID with actual ID from response
curl http://localhost:8000/pipeline/status/JOB_ID
```

### Method 3: Using Python Requests

```python
import requests
import time

# Start pipeline
response = requests.post(
    "http://localhost:8000/pipeline/run",
    json={
        "match_url": "https://www.whoscored.com/matches/...",
        "date": "2026-08-15",
        "league": "Premier League",
        "gameweek": 1,
        "home_xg": 1.2,
        "away_xg": 2.5,
        "home_xgot": 1.0,
        "away_xgot": 2.1
    }
)

job_id = response.json()["job_id"]
print(f"Job started: {job_id}")

# Monitor status
while True:
    status_response = requests.get(
        f"http://localhost:8000/pipeline/status/{job_id}"
    )
    status = status_response.json()

    print(f"Status: {status['status']} - Stage: {status['stage']}")

    if status["status"] in ["completed", "failed"]:
        break

    time.sleep(5)

# List visualizations
viz_response = requests.get(
    f"http://localhost:8000/visualizations/{job_id}"
)
visualizations = viz_response.json()["visualizations"]
print(f"Generated {len(visualizations)} visualizations")
```

### Method 4: Interactive API (Swagger UI)

1. Open browser: http://localhost:8000/docs
2. Click "POST /pipeline/run"
3. Click "Try it out"
4. Fill in the request body
5. Click "Execute"
6. Copy the `job_id`
7. Use "GET /pipeline/status/{job_id}" to monitor

## Finding Match URLs

### Whoscored URLs
Go to Whoscored.com and find your match:
1. Navigate to match page
2. URL format: `https://www.whoscored.com/matches/{MATCH_ID}/live/{competition}-{season}-{home}-{away}`
3. Copy the full URL

Example matches:
- **Crystal Palace vs Arsenal**:
  ```
  https://www.whoscored.com/matches/1903353/live/england-premier-league-2025-2026-crystal-palace-arsenal
  ```

## Getting xG Values

### From Fotmob (Recommended)
1. Go to Fotmob.com
2. Search for the match
3. Look for "Expected goals" section
4. Note: **xG** and **xGOT** (xG on target) for both teams

### From Other Sources
- Understat.com
- FBref.com
- Statsbomb (if available)

**Important**: xG values MUST be provided manually via the API - they are not scraped from Whoscored.

## Team Colors

### Premier League Teams
```python
ARSENAL = "#EF0107"
CHELSEA = "#034694"
CRYSTAL_PALACE = "#1B458F"
LIVERPOOL = "#C8102E"
MAN_CITY = "#6CABDD"
MAN_UNITED = "#DA291C"
TOTTENHAM = "#132257"
WEST_HAM = "#7A263A"
```

### Finding Team Colors
1. Google: "{team name} official colors hex"
2. Or use: https://teamcolorcodes.com/soccer/

## Output Files

After successful pipeline run, find files in:

```
data/
├── raw/{job_id}/
│   └── match_{job_id}.html          # Raw HTML from Whoscored
├── processed/{job_id}/
│   ├── EventData.csv                # Transformed events
│   ├── PlayerData.csv               # Player information
│   └── metadata.json                # Match metadata
├── quality/{job_id}/
│   └── validation_report.json       # Quality check results
└── visualizations/{job_id}/
    ├── match_dashboard.png          # Main 6x3 dashboard
    ├── player_dashboard.png         # Player analysis
    └── *.png                        # Individual plots
```

## Troubleshooting

### "Cannot connect to API"
```bash
# Check if services are running
docker-compose ps

# If not running, start them
./run.sh

# Or manually
docker-compose up -d
```

### "Scraping failed"
Common causes:
1. **Invalid URL**: Double-check Whoscored URL
2. **Page not loaded**: Increase wait time in `scraper/scraper.py`
3. **Whoscored blocking**: Try different user agent or IP

Fix:
```python
# In scraper/scraper.py, increase wait_time
def scrape_match(self, match_url: str, output_path: str, wait_time: int = 20):  # Changed from 10 to 20
```

### "Validation failed"
Check the validation report:
```bash
cat data/quality/{job_id}_report.json | jq
```

Common issues:
- Missing players
- Invalid coordinates
- Duplicate event IDs

### "No visualizations generated"
Check orchestrator logs:
```bash
docker-compose logs orchestrator | grep -i error
```

### "Memory issues"
Increase Docker memory:
1. Open Docker Desktop
2. Settings → Resources
3. Memory → 8GB minimum
4. Apply & Restart

## Advanced Usage

### Custom Colors
```json
{
  "bg_color": "#1A1A1A",      # Dark background
  "line_color": "#FFFFFF",     # White lines
  "home_color": "#FF5722",     # Custom orange
  "away_color": "#00BCD4"      # Custom cyan
}
```

### Processing Multiple Matches
```python
import requests

matches = [
    {"url": "...", "home_xg": 1.2, "away_xg": 2.1},
    {"url": "...", "home_xg": 0.8, "away_xg": 1.5},
    {"url": "...", "home_xg": 2.3, "away_xg": 0.9},
]

job_ids = []
for match in matches:
    response = requests.post(
        "http://localhost:8000/pipeline/run",
        json={
            "match_url": match["url"],
            "date": "2026-08-15",
            "league": "Premier League",
            "gameweek": 1,
            "home_xg": match["home_xg"],
            "away_xg": match["away_xg"],
            "home_xgot": match["home_xg"] * 0.8,  # Estimate
            "away_xgot": match["away_xg"] * 0.8
        }
    )
    job_ids.append(response.json()["job_id"])

print(f"Started {len(job_ids)} jobs")
```

### Viewing Logs
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f orchestrator
docker-compose logs -f scraper

# Last 100 lines
docker-compose logs --tail=100
```

### Restarting Services
```bash
# Restart all
docker-compose restart

# Restart specific service
docker-compose restart orchestrator
```

### Stopping Services
```bash
# Stop but keep data
docker-compose down

# Stop and remove data
docker-compose down -v
```

## Performance Tips

1. **Pipeline Duration**: Typically 3-5 minutes per match
2. **Concurrent Jobs**: Can run multiple matches in parallel
3. **Scraping Time**: 10-20 seconds (increase if needed)
4. **Transformation**: 5-10 seconds
5. **Validation**: 2-5 seconds
6. **Visualization**: 1-2 minutes

## Next Steps

After successful run:
1. **View visualizations**: Open PNG files
2. **Analyze data**: Examine CSV files with pandas
3. **Check quality**: Review validation report
4. **Share results**: Dashboard images are ready to share

## Getting Help

- **Documentation**: README.md
- **API Docs**: http://localhost:8000/docs
- **Logs**: `docker-compose logs`
- **Status**: http://localhost:8000/health

---

**Ready to analyze your matches! ⚽📊**
