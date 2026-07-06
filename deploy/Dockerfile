FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# runs the release-time macro engine; keys come from the environment
CMD ["python", "release_runner.py"]
