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

# 🔧 Troubleshooting Guide

## Docker Build Failures

### Issue: `pip install` failing during build

**Symptoms:**
```
failed to solve: process "/bin/sh -c pip install --no-cache-dir -r requirements.txt" did not complete successfully: exit code: 1
```

**Solution 1: Updated dependency versions** ✅
All requirements.txt files have been updated with compatible versions.

Try building again:
```bash
docker compose build
```

**Solution 2: If still failing, use unpinned versions**

Edit `orchestrator/requirements.txt`:
```txt
fastapi
uvicorn[standard]
redis
pydantic
requests
pandas
numpy
selenium
webdriver-manager
pandera
matplotlib
seaborn
mplsoccer
highlight-text
pillow
scipy
scikit-learn
```

Then rebuild:
```bash
docker compose build --no-cache
```

**Solution 3: Build without cache**
```bash
docker compose build --no-cache
```

**Solution 4: Update Docker**
```bash
# Make sure you have latest Docker
docker --version  # Should be 20.10+
```

---

## Scraper Issues

### Issue: "matchCentreData not found in HTML"

**Solution:**
Increase wait time in `scraper/scraper.py`:

```python
def scrape_match(self, match_url: str, output_path: str, wait_time: int = 20):  # Increased from 10
```

---

## Service Startup Issues

### Issue: Services not starting

**Check status:**
```bash
docker compose ps
docker compose logs
```

**Restart:**
```bash
docker compose down
docker compose up -d
```

**Clean restart:**
```bash
docker compose down -v
docker system prune -f
./run.sh
```

---

## Redis Connection Issues

### Issue: "Cannot connect to Redis"

**Check Redis:**
```bash
docker compose ps redis
docker compose logs redis
```

**Restart Redis:**
```bash
docker compose restart redis
```

---

## API Not Responding

### Issue: Cannot connect to http://localhost:8000

**Check orchestrator:**
```bash
docker compose logs orchestrator
docker compose ps orchestrator
```

**Restart:**
```bash
docker compose restart orchestrator
```

**Check port:**
```bash
# Make sure port 8000 is not in use
lsof -i :8000
```

---

## Memory Issues

### Issue: Container killed (OOM)

**Solution:**
Increase Docker memory to 8GB:
1. Docker Desktop → Settings → Resources
2. Memory → 8GB
3. Apply & Restart

---

## Permission Issues

### Issue: Cannot write to /data

**Solution:**
```bash
chmod -R 777 data/
# Or
sudo chown -R $USER:$USER data/
```

---

## Python Import Errors

### Issue: ModuleNotFoundError

**Solution:**
Make sure you're running inside Docker:
```bash
# Don't run services directly
# ❌ python visualizer/visualize.py

# Use Docker
# ✅ docker compose up
```

---

## Quick Fixes

### Reset Everything
```bash
docker compose down -v
rm -rf data/*
docker system prune -af
./run.sh
```

### View All Logs
```bash
docker compose logs -f | grep -i error
```

### Test Individual Service
```bash
# Build just orchestrator
docker compose build orchestrator

# Run just orchestrator
docker compose up orchestrator
```

---

## Common Error Messages

### "apt-key is deprecated"
**Safe to ignore** - it's a warning, not an error

### "Chrome not found"
**Solution:** Dockerfile installs Chrome automatically. If failing:
```bash
docker compose build --no-cache orchestrator
```

### "Cannot find module 'contracts'"
**Solution:** Volumes not mounted correctly
```bash
docker compose down
docker compose up -d
```

---

## Get More Help

### Check Logs
```bash
# All services
docker compose logs

# Specific service
docker compose logs orchestrator
docker compose logs scraper
docker compose logs transformer
```

### Check Container Status
```bash
docker compose ps
docker stats
```

### Check Disk Space
```bash
df -h
docker system df
```

### Clean Docker
```bash
docker system prune -a
docker volume prune
```

---

## Still Having Issues?

1. **Check Docker version:** `docker --version` (need 20.10+)
2. **Check Docker Compose version:** `docker compose version` (need v2+)
3. **Check available memory:** At least 4GB free
4. **Check internet connection:** Needed for scraping
5. **Try clean rebuild:**
   ```bash
   docker compose down -v
   docker system prune -af
   docker compose build --no-cache
   docker compose up -d
   ```

---

**Most issues are solved by rebuilding:**
```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```
