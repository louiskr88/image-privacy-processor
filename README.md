# Image Privacy Processor

A Windows desktop batch tool that detects IPv4, IPv6, and MAC addresses with Tesseract OCR and blurs the matched areas with ImageMagick. It also supports a Fixed Area mode that blurs the same selected region in every image without OCR.

## Requirements

- Python 3.10+
- Tesseract OCR with English language data
- ImageMagick 7 available on PATH

Install Python packages:

```powershell
python -m pip install Pillow pytesseract
```

Double-click `setup_and_run.bat` to check/install the Python packages and launch the GUI. The GUI starts in English;

See [README_th.md](README_th.md) for Thai setup and usage instructions.

Original images are left unchanged. Output images are written into a timestamped `output_YYYYMMDD_HHMMSS` folder.
