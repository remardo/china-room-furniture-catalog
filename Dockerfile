FROM python:3.13-alpine
WORKDIR /app
COPY app.py *.html ./
RUN mkdir -p /data
EXPOSE 8000
CMD ["python", "app.py"]
