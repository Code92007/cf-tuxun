FROM node:22-bookworm-slim AS math-runtime
WORKDIR /math
COPY package.json .
RUN npm install --omit=dev --ignore-scripts

FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
COPY --from=math-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=math-runtime /math/node_modules /app/node_modules

ENV PORT=8000
ENV DATA_DIR=/data
EXPOSE 8000

CMD ["python", "app.py"]
