name: Melbet Monitor

on:
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch: {}

# Prevent overlapping runs if one poll takes longer than the 5-minute interval
concurrency:
  group: melbet-monitor
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  poll:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Cache Playwright browsers
        uses: actions/cache@v4
        with:
          path: ~/.cache/ms-playwright
          key: playwright-chromium-${{ runner.os }}

      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          playwright install --with-deps chromium

      - name: Run monitor
        env:
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
          ALERT_TO: ${{ secrets.ALERT_TO }}
        run: python monitor.py

      - name: Commit updated alerts.json if changed
        run: |
          git config user.name "melbet-monitor-bot"
          git config user.email "actions@github.com"
          git add alerts.json
          git diff --cached --quiet || git commit -m "Update alerts state [skip ci]"
          git push
