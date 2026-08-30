FROM python:3.13-alpine
WORKDIR /app
COPY app.py *.html ./
RUN mkdir -p /data
EXPOSE 80
CMD ["python", "app.py"]
