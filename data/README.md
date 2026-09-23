# data/

Not tracked in git (see `.gitignore`) — re-derive it:

1. Download the service status message log from TfL's published performance
   data: https://tfl.gov.uk/corporate/publications-and-reports/underground-services-performance
   Save as `data/raw-service-status-messages.csv`.
2. `python3 historical/build_historical.py`

That produces `events_clean.csv` and `hourly.csv`.
