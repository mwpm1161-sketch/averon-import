from averon_import.services.ocr_engine import TesseractOcrEngine

health = TesseractOcrEngine.health()
print("OCR:", health)
if not health.get("available") or not health.get("russian"):
    raise SystemExit(1)
print("Окружение готово.")
