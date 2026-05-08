# Bourbon Hunter

A personal bourbon collection tracker and watchdog agent.

## What it does
- Tracks current secondary market value of bottles in my collection
- Calculates appreciation/depreciation per bottle and overall
- Sends weekly email summary
- Identifies bottles via photo (drop image in Drive folder, agent IDs it)
- (Planned) Watchdog mode: alerts on undervalued bottles at retail

## Stack
- Python 3.12
- Anthropic Claude API (vision + normalization)
- requests + BeautifulSoup (scraping)
- pandas (data wrangling)
- CSV storage to start

## Status
In development — first agent build.