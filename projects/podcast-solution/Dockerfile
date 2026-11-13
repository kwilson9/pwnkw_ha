FROM python:3.13-slim
RUN pip install --no-cache-dir mutagen==1.47.0 tzdata==2025.2
WORKDIR /app
COPY app.py .
ENV PYTHONUNBUFFERED=1
EXPOSE 8095
CMD ["python", "/app/app.py"]
