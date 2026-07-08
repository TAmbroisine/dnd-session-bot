FROM python:3.12-slim

# Fuseau horaire du conteneur (les datetimes du bot sont de toute façon
# explicitement en Europe/Paris via zoneinfo, ceci aligne juste les logs)
ENV TZ=Europe/Paris \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# Exécution sans privilèges ; /app/data est le point de montage du volume
RUN useradd --create-home botuser && mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "bot.main"]
