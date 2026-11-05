# Validation — 2026.11.05.15

- Core synthetic regression: 72 tests passed.
- Desktop/package checks: 11 tests passed.
- Progress and popup placement: 7 tests passed, including nested dialog centering and preserving manual movement. Set expansion and duplicate-white validation-type rejection are included in the 72 core tests.
- Ed import: 7 tests passed, including real GUI worker conversion, current-date auto-connection, persisted CSV path, wrong-date rejection and modified-CSV rejection.
- Synthetic end-to-end: real DN/radiometry/gain/Ed/ELC processing, 20 TIFFs, pilot approval before full processing, unchanged source hashes. No science processing stages mocked.
- Archived partial-result continuation: GUI report → explicit approval → full output tested with 3 synthetic shots, 8 bands and 24 TIFFs; original coefficients reused and archived outputs/ROI preserved.
- Launcher/bootstrap smoke passed. Installed stage 01 rendered and inspected with the red folder-import button and editable Ed timezone.
- Package manifest and SHA256 checked; research imagery, observation-specific Ed, ROI, outputs and credentials excluded. Explicitly requested built-in sensor/tarp calibration tables included.

These checks do not establish accuracy for every field flight or compatibility with every PC. Users still review tarp proposals, identity of calibration data, validation errors and full-flight approval.
