FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .

EXPOSE 8201

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8201"]
