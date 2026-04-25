# ETL Training Data

This directory should contain the current cleaned ETL exports used for downstream dataset builders:

- `issues_clean.jsonl`
- `prs_clean.jsonl`
- `cleaning_report.json`

Source folder:
- [Final Training Data (Google Drive)](https://drive.google.com/drive/folders/1xDUB_NqxSs9ODl4sX_mordbIo2s2vHuh?usp=share_link)

## Refresh steps

1. Download the three files from the Drive folder.
2. Move them into this directory (`etl/training_data/`) with the same filenames.
3. Verify expected files exist before running builders:
   - `issues_clean.jsonl`
   - `prs_clean.jsonl`
   - `cleaning_report.json`
