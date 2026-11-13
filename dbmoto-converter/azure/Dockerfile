# Azure mirror of the dbmoto-xml-converter Lambda (python3.11): the same files the deploy_lambda job zips,
# hosted by azure/runner.py. Build context: dbmoto-converter/  (az acr build -f azure/Dockerfile dbmoto-converter)
FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 APP_DIR=/app
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd -u 1000 -m app
COPY lambda_function.py parse_dbmoto_metadata_xml.py ./
COPY azure/runner.py /runner/runner.py
USER app
EXPOSE 8080
CMD ["python", "/runner/runner.py"]
