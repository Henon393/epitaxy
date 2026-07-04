# Rendu PDF/A via WeasyPrint dans un Linux où Pango s'installe trivialement
# (l'hôte de dev Windows n'a pas les DLL GTK). Polices DejaVu embarquées par
# WeasyPrint dans le PDF, exigence PDF/A.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "weasyprint>=63"

COPY render.py /render.py

ENTRYPOINT ["python", "/render.py"]
