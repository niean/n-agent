FROM python:3.11-slim

ENV TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app
#RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple .

EXPOSE 8201

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8201"]
