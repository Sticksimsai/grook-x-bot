FROM python:3.12-slim AS tested
WORKDIR /app
COPY bot.py voice.txt test_bot.py ./
RUN python -m unittest -q

FROM python:3.12-slim
WORKDIR /app
COPY --from=tested /app/bot.py /app/voice.txt ./
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["python", "bot.py"]
