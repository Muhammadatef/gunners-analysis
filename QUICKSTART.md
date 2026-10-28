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

# ⚡ Quick Start - 3 Commands to Analytics

## 1️⃣ Start Services (30 seconds)
```bash
./run.sh
```

## 2️⃣ Test with Arsenal Match (3-5 minutes)
```bash
python3 test_arsenal_match.py
```

## 3️⃣ View Results
```bash
ls -lh data/visualizations/*/
```

---

## 📊 That's it! You now have:

✅ Match data scraped from Whoscored
✅ Coordinates transformed (100x100 → 105x68)
✅ Expected Threat (xT) calculated
✅ Progressive passes identified
✅ Data quality validated
✅ Visualizations generated

---

## 🔗 Quick Links

- **API Docs**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health
- **Logs**: `docker-compose logs -f`

---

## 📚 More Help

- **Detailed Usage**: See [USAGE.md](USAGE.md)
- **Architecture**: See [README.md](README.md)
- **Deployment**: See [DEPLOYMENT.md](DEPLOYMENT.md)

---

## 🛠️ Common Commands

```bash
# Stop services
docker-compose down

# Restart services
docker-compose restart

# View logs
docker-compose logs -f orchestrator

# Clean everything
make clean
```

---

## 🎯 Next Steps

1. Edit `.env` with your match details
2. Get xG values from Fotmob
3. Run `python3 test_arsenal_match.py`
4. Share the dashboards! 📊⚽

---

**Built with ❤️ for Arsenal match analysis**
